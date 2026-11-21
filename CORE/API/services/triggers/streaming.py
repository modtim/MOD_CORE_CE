"""add-workflow-triggers §8 — streaming (message-bus) + polling trigger sources.

Two shapes:
- POLL (pull): a scheduler-tick evaluator fetches an HTTP endpoint (conditional
  GET via ETag/If-Modified-Since) or an RSS feed (last-seen marker) at the
  trigger's min interval and fires on new/changed content. SSRF-guarded.
- MESSAGE_BUS (push): a background subscriber per active trigger. Redis-channel
  works on any deployment; kafka/nats/rabbitmq/mqtt need their client lib + a
  broker — adapters lazy-import and are reported DEGRADED (not faked) when absent.

Honest gating: `streaming_status` reports which providers are actually available.
"""
from __future__ import annotations

import hashlib
import inspect
import ipaddress
import os
import random
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

# Message-bus client libs we can drive if installed. Detected lazily.
_BUS_CLIENT_MODULE = {
    "redis": "redis",
    "kafka": "kafka",        # kafka-python
    "nats": "nats",          # nats-py
    "rabbitmq": "pika",
    "mqtt": "paho.mqtt.client",
}

# Providers with an ACTUAL subscriber implementation (see
# start_message_bus_subscribers below). Single source of truth for the
# trigger-create gate in routers/workflow_triggers.py — a message_bus
# trigger for a provider not in this set is rejected, so users can't
# create a trigger that silently never fires. nats/rabbitmq are declared
# in the enum + _BUS_CLIENT_MODULE for honest DEGRADED reporting but have
# no consumer loop yet; mqtt's landed (see MqttSubscriber below). Add a
# provider here the moment its subscriber lands.
IMPLEMENTED_MESSAGE_BUS_PROVIDERS = frozenset({"redis", "kafka", "mqtt"})


# --- SSRF guard ------------------------------------------------------------

def _ip_blocked(ip_text: str) -> bool:
    """The IP-level half of the SSRF guard (shared by url_is_blocked and the
    MQTT subscriber, which resolves + pins addresses itself)."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True  # unparseable address -> refuse
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)


def url_is_blocked(url: str) -> Optional[str]:
    """Return a reason string if the URL should NOT be fetched (SSRF guard), else
    None. Private/loopback/link-local targets are refused unless TRIGGER_POLL_
    ALLOW_PRIVATE=true (dev/bed)."""
    if os.getenv("TRIGGER_POLL_ALLOW_PRIVATE", "false").lower() == "true":
        return None
    try:
        host = urlparse(url).hostname
        if not host:
            return "no host"
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            if _ip_blocked(sockaddr[0]):
                return f"{host} resolves to a private/loopback address ({sockaddr[0]})"
    except Exception as e:  # noqa: BLE001 — unresolvable → refuse
        return f"could not resolve host: {e}"
    return None


# --- adapter availability + status ----------------------------------------

def provider_available(provider: str) -> bool:
    mod = _BUS_CLIENT_MODULE.get(provider)
    if not mod:
        return False
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


def streaming_status(db) -> Dict[str, Any]:
    from models import WorkflowTrigger, WorkflowTriggerType
    counts: Dict[str, int] = {}
    for trig in (db.query(WorkflowTrigger).filter(
            WorkflowTrigger.trigger_type == WorkflowTriggerType.MESSAGE_BUS,
            WorkflowTrigger.is_active.is_(True),
            WorkflowTrigger.deleted_at.is_(None)).all()):
        p = (trig.configuration or {}).get("provider", "?")
        counts[p] = counts.get(p, 0) + 1
    providers = {p: {"available": provider_available(p), "active_triggers": counts.get(p, 0)}
                 for p in _BUS_CLIENT_MODULE}
    return {"message_bus": providers,
            "poll_active": db.query(WorkflowTrigger).filter(
                WorkflowTrigger.trigger_type == WorkflowTriggerType.POLL,
                WorkflowTrigger.is_active.is_(True),
                WorkflowTrigger.deleted_at.is_(None)).count()}


# --- POLL evaluator (scheduler tick) --------------------------------------

def _poll_state(db, trig):
    from models import TriggerEventLog
    row = (db.query(TriggerEventLog)
           .filter(TriggerEventLog.trigger_id == trig.id,
                   TriggerEventLog.event_key == "poll-state").first())
    if row is None:
        row = TriggerEventLog(trigger_id=trig.id, tenant_id=trig.tenant_id,
                              event_key="poll-state", state={})
        db.add(row)
        db.commit()
    return row


def _now():
    return datetime.now(timezone.utc)


def _poll_http(db, trig, st: dict) -> bool:
    # GSEC FX.19 R-042 — webhook egress disabled: no outbound poll fetch.
    # Logged (audit-safe, no URL body); the trigger simply does not fire.
    from utils import egress_policy
    _r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_WEBHOOKS, db=db)
    if _r042:
        logger.info("triggers: HTTP poll skipped (trigger=%s) — %s",
                    trig.id, _r042)
        return False
    from utils import safe_http
    cfg = trig.configuration or {}
    headers = {}
    if st.get("etag"):
        headers["If-None-Match"] = st["etag"]
    if st.get("last_modified"):
        headers["If-Modified-Since"] = st["last_modified"]
    try:
        r = safe_http.get(cfg["url"], headers=headers,
                          _ssrf_connect_timeout=15, _ssrf_read_timeout=15)
    except safe_http.SsrfBlocked:
        logger.warning("triggers: HTTP poll refused by SSRF guard (trigger=%s)",
                       trig.id)
        return False
    if r.status_code == 304:
        return False
    if not r.ok:
        return False
    h = hashlib.sha256(r.content).hexdigest()
    changed = h != st.get("content_hash")
    st["content_hash"] = h
    st["etag"] = r.headers.get("ETag")
    st["last_modified"] = r.headers.get("Last-Modified")
    if not changed:
        return False
    from services.triggers import pipeline
    ctx = {"event_key": f"poll:{trig.id}:{h[:16]}",
           "http": {"status": r.status_code},
           "inputs": {"http_body": r.text[:65536]}}
    return bool(pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"))


def _parse_rss(text: str) -> List[Dict[str, str]]:
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(text)
    except Exception:  # noqa: BLE001
        return out
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for ch in item:
            t = ch.tag.split("}")[-1]
            if t in ("guid", "id", "link", "title"):
                d[t] = (ch.text or ch.get("href") or "").strip()
        d["guid"] = d.get("guid") or d.get("id") or d.get("link") or d.get("title")
        if d.get("guid"):
            out.append(d)
    return out


def _poll_rss(db, trig, st: dict) -> int:
    # GSEC FX.19 R-042 — webhook egress disabled: no outbound RSS fetch.
    from utils import egress_policy
    _r042 = egress_policy.refuse_reason(egress_policy.CATEGORY_WEBHOOKS, db=db)
    if _r042:
        logger.info("triggers: RSS poll skipped (trigger=%s) — %s",
                    trig.id, _r042)
        return 0
    from utils import safe_http
    try:
        r = safe_http.get((trig.configuration or {})["url"],
                          _ssrf_connect_timeout=15, _ssrf_read_timeout=15)
    except safe_http.SsrfBlocked:
        logger.warning("triggers: RSS poll refused by SSRF guard (trigger=%s)",
                       trig.id)
        return 0
    if not r.ok:
        return 0
    items = _parse_rss(r.text)
    seen = set(st.get("seen", []))
    fresh = [it for it in items if it["guid"] not in seen]
    from services.triggers import pipeline
    fired = 0
    for it in fresh:
        ctx = {"event_key": f"rss:{trig.id}:{it['guid']}",
               "rss": it, "inputs": {"rss_item": it}}
        if pipeline.evaluate_and_dispatch(db, trig, ctx).get("dispatched"):
            fired += 1
    st["seen"] = list(seen | {it["guid"] for it in items})[-500:]
    return fired


def evaluate_poll_triggers(db) -> int:
    """Poll due POLL triggers; fire on new/changed content. Returns count fired."""
    from models import WorkflowTrigger, WorkflowTriggerType
    fired = 0
    triggers = (db.query(WorkflowTrigger).filter(
        WorkflowTrigger.trigger_type == WorkflowTriggerType.POLL,
        WorkflowTrigger.is_active.is_(True),
        WorkflowTrigger.deleted_at.is_(None)).all())
    for trig in triggers:
        try:
            cfg = trig.configuration or {}
            row = _poll_state(db, trig)
            st = dict(row.state or {})
            interval = int(cfg.get("interval_seconds", 300))
            lp = st.get("last_polled")
            if lp and (_now() - datetime.fromisoformat(lp)).total_seconds() < interval:
                continue
            blocked = url_is_blocked(cfg.get("url", ""))
            if blocked:
                logger.warning("poll trigger %s URL blocked (SSRF): %s", trig.id, blocked)
                continue
            if cfg.get("kind") == "http":
                if _poll_http(db, trig, st):
                    fired += 1
            elif cfg.get("kind") == "rss":
                fired += _poll_rss(db, trig, st)
            st["last_polled"] = _now().isoformat()
            row.state = st
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("poll trigger %s evaluation failed", trig.id)
            try:
                db.rollback()
            except Exception:
                pass
    return fired


# --- MESSAGE_BUS subscribers (opt-in) -------------------------------------

_subscribers_started = False

# Kafka topic naming (same constraint the broker itself enforces):
# alnum + '.'/'_'/'-', 1-249 chars, and never exactly '.' or '..'.
_KAFKA_TOPIC_RE = re.compile(r'\A[A-Za-z0-9._-]{1,249}\Z')


def validate_kafka_topic(topic: Any) -> str:
    """Raise ValueError for anything that isn't a legal Kafka topic name.
    Called both at trigger create-time (workflow_triggers_service.py) and
    again at subscriber start — a trigger's configuration can be edited
    (or come from an older, pre-validation row) so the subscriber never
    trusts the DB blindly."""
    if (not isinstance(topic, str) or topic in (".", "..")
            or not _KAFKA_TOPIC_RE.match(topic)):
        raise ValueError(f"invalid kafka topic name: {topic!r}")
    return topic


def _resolve_message_bus_secret(tenant_id: Optional[int],
                                 secret_ref: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve a message-bus trigger's secret-ref via the tenant secret store
    (same tenant-scoped Vault-backed resolution burst_accounts.py uses for its
    provider credentials: `crud.secret_cud.resolve_decrypted_secret_for_tenant`,
    JSON object of kwargs). Returns None — never raises, never logs the value —
    if nothing is configured or resolution fails; callers then just skip SASL."""
    if not secret_ref or tenant_id is None:
        return None
    try:
        import json
        from database import SessionLocal
        from crud.secret_cud import resolve_decrypted_secret_for_tenant
        with SessionLocal() as db:
            raw = resolve_decrypted_secret_for_tenant(
                db, secret_name=secret_ref, tenant_id=tenant_id)
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — resolution failure must not crash the subscriber
        logger.warning("message-bus(kafka) secret resolution failed for secret_ref=%s "
                       "(tenant=%s) — falling back to no SASL", secret_ref, tenant_id)
        return None


def _redis_subscriber(trig_id: int, channel: str):
    import redis as redis_lib
    from database import SessionLocal
    from models import WorkflowTrigger
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    ps = r.pubsub()
    ps.subscribe(channel)
    logger.info("message-bus(redis) subscriber on %s for trigger %s", channel, trig_id)
    for msg in ps.listen():
        if msg.get("type") != "message":
            continue
        try:
            payload = msg.get("data")
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", "replace")
            from services.triggers import pipeline
            with SessionLocal() as db:
                trig = db.query(WorkflowTrigger).filter(WorkflowTrigger.id == trig_id).first()
                if trig and trig.is_active:
                    pipeline.evaluate_and_dispatch(
                        db, trig, {"message": payload, "inputs": {"message": payload}})
        except Exception:  # noqa: BLE001
            logger.exception("message-bus(redis) handler failed for trigger %s", trig_id)


class KafkaSubscriber:
    """Message-bus subscriber for a Kafka topic — same contract as the redis
    subscriber in _redis_subscriber: constructed with (trigger_id, topic),
    `.start()` blocks in the producer->consumer loop and dispatches each
    message through the trigger pipeline.

    kafka-python is imported LAZILY inside start() so this module keeps
    importing (and the create-gate keeps working) on deployments without the
    client installed; when it's missing, start() raises ImportError and the
    caller logs DEGRADED — never a silently-dead trigger."""

    def __init__(self, trig_id: int, topic: str, *, tenant_id: Optional[int] = None,
                 url: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.trig_id = trig_id
        self.topic = validate_kafka_topic(topic)
        self.tenant_id = tenant_id
        self.url = url or os.getenv("KAFKA_URL", "kafka://localhost:9092")
        self.config = config or {}
        # Scoped per tenant+trigger so consumer-group offsets never collide
        # across tenants (or across two triggers on the same topic).
        self.group_id = f"mod-trigger-{tenant_id}-{trig_id}"
        self.client = None
        self._stop_event = threading.Event()
        # SASL INFO is logged at most once per subscriber instance (the kwargs
        # builder re-runs on every connect retry).
        self._sasl_logged = False

    def stop(self) -> None:
        """Request a clean shutdown of the consume loop."""
        self._stop_event.set()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        """Connect and consume until stopped (daemon-thread target).
        Connection/consume errors back off exponentially (cap 60s) and retry;
        stop() returns without retry.  An IDLE topic (kafka-python's iterator
        raising StopIteration after consumer_timeout_ms) is NOT exhaustion:
        we re-enter the same consumer and keep listening, like redis.  A real
        kafka-python iterator always blocks up to consumer_timeout_ms per
        next() call, so a pass that returns without blocking is unreachable
        against a live broker; if it ever happens without a stop() request we
        log a WARNING (not a silent exit) before returning."""
        from kafka import KafkaConsumer  # lazy: absent lib -> ImportError, caller logs DEGRADED
        from database import SessionLocal
        from models import WorkflowTrigger
        from services.triggers import pipeline

        host = urlparse(self.url).hostname
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                kwargs = self._consumer_kwargs()
                consumer = KafkaConsumer(self.topic, **kwargs)
                self.client = consumer
                logger.info("message-bus(kafka) subscriber on topic=%s host=%s for trigger %s",
                           self.topic, host, self.trig_id)
                # kafka-python's iterator honours consumer_timeout_ms: on an IDLE
                # topic it BLOCKS for that long and then raises StopIteration.
                # That is not exhaustion of the subscriber — re-enter the SAME
                # consumer and keep listening (the redis subscriber never stops
                # either).  A pass that comes back WITHOUT blocking that long
                # means the iterator is spent/closed, and only then is a clean
                # exit honest (stop() remains the guaranteed exit signal).
                idle_floor = max(0.05,
                                 int(kwargs.get("consumer_timeout_ms") or 10000) / 2000.0)
                while not self._stop_event.is_set():
                    pass_started = time.monotonic()
                    for msg in consumer:
                        if self._stop_event.is_set():
                            break
                        try:
                            if getattr(msg, "data", msg) is None:
                                continue  # tombstone (Kafka null value): nothing to dispatch
                            payload = self._decode(msg)
                            with SessionLocal() as db:
                                trig = (db.query(WorkflowTrigger)
                                        .filter(WorkflowTrigger.id == self.trig_id).first())
                                if trig and trig.is_active:
                                    pipeline.evaluate_and_dispatch(
                                        db, trig, {"message": payload, "inputs": {"message": payload}})
                        except Exception:  # noqa: BLE001 — one bad message must not kill the loop
                            logger.exception("message-bus(kafka) handler failed for trigger %s", self.trig_id)
                    if self._stop_event.is_set():
                        break
                    if time.monotonic() - pass_started < idle_floor:
                        break  # iterator returned without an idle-timeout block: spent
                if self._stop_event.is_set():
                    return  # stop() -> clean, expected exit
                # A real kafka-python iterator always blocks up to
                # consumer_timeout_ms per next() call, so this path is not
                # reachable against a live broker — it only fires if the
                # consumer was closed/invalidated out from under us without a
                # stop() request.  Log loudly rather than exiting the daemon
                # thread silently (B101).
                logger.warning(
                    "message-bus(kafka) subscriber for trigger %s topic=%s host=%s "
                    "exiting: consumer iterator returned spent without a stop() request",
                    self.trig_id, self.topic, host)
                return
            except Exception:  # noqa: BLE001 — connection/consume error: back off and retry
                logger.exception(
                    "message-bus(kafka) subscriber error (trigger=%s topic=%s host=%s); "
                    "retrying in %.0fs", self.trig_id, self.topic, host, backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2.0, 60.0)

    def _consumer_kwargs(self) -> Dict[str, Any]:
        """Real kafka-python KafkaConsumer kwargs: bootstrap_servers (list of
        "host:port"), group_id, enable_auto_commit, auto_offset_reset,
        consumer_timeout_ms. SASL kwargs are added only when the trigger's
        secret-ref resolves to credentials (see _sasl_kwargs)."""
        u = urlparse(self.url)
        host = u.hostname or "localhost"
        port = u.port or 9092
        kwargs: Dict[str, Any] = {
            "bootstrap_servers": [f"{host}:{port}"],
            "group_id": self.group_id,
            "enable_auto_commit": True,
            "auto_offset_reset": "latest",
            "consumer_timeout_ms": int(self.config.get("consumer_timeout_ms", 10000)),
        }
        try:
            from kafka import KafkaConsumer as _KafkaConsumer
            if "allow_auto_create_topics" in inspect.signature(_KafkaConsumer.__init__).parameters:
                kwargs["allow_auto_create_topics"] = False
        except Exception:  # noqa: BLE001 — optional hardening only
            pass
        kwargs.update(self._sasl_kwargs())
        return kwargs

    def _sasl_kwargs(self) -> Dict[str, Any]:
        """SASL kwargs when the trigger's secret-ref carries username/password,
        else {} (plaintext/no-auth broker). Never logs credentials — only
        host + topic are ever logged."""
        creds = _resolve_message_bus_secret(self.tenant_id, self.config.get("secret_ref"))
        if not creds or not creds.get("username") or not creds.get("password"):
            return {}
        tls = self.config.get("tls", True)
        if not self._sasl_logged:
            self._sasl_logged = True
            logger.info("message-bus(kafka) SASL enabled for trigger %s topic=%s host=%s",
                        self.trig_id, self.topic, urlparse(self.url).hostname)
        return {
            "security_protocol": "SASL_SSL" if tls else "SASL_PLAINTEXT",
            "sasl_mechanism": self.config.get("sasl_mechanism", "SCRAM-SHA-512"),
            "sasl_plain_username": creds["username"],
            "sasl_plain_password": creds["password"],
        }

    def _decode(self, msg: Any) -> str:
        """Normalise a kafka message (bytes/str/Message object) to text."""
        data = getattr(msg, "data", msg)
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return str(data)


# --- MQTT (paho-mqtt) ------------------------------------------------------
# Same contract as KafkaSubscriber: lazy client import, DEGRADED when absent,
# never a silently-dead trigger. Credentials come ONLY from the trigger's
# secret_ref (never from the URL) and are never logged.

# Payload cap identical to the kafka/http poll path (r.text[:65536]).
_MQTT_MAX_PAYLOAD = 65536
_MQTT_MAX_TOPIC_LEN = 256


def validate_mqtt_topic(topic: Any) -> str:
    """Raise ValueError for anything that isn't a legal MQTT subscribe topic.

    Rules: 1..256 chars, UTF-8, no NUL; '+' must occupy a whole topic level
    ('a/+/c' ok, 'a+/b' not); '#' only as the final whole level ('a/#' ok,
    'a/#/b' not); topics starting with '$' (e.g. '$SYS/#') are broker-internal
    and refused. Called at create-time and again at subscriber start — the
    subscriber never trusts the DB blindly."""
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"invalid mqtt topic: {topic!r}")
    if len(topic) > _MQTT_MAX_TOPIC_LEN:
        raise ValueError(f"mqtt topic too long ({len(topic)} > 256 chars): "
                         f"{topic[:32]!r}...")
    if "\x00" in topic:
        raise ValueError("mqtt topic contains NUL")
    try:
        topic.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("mqtt topic is not valid UTF-8")
    if topic.startswith("$"):
        raise ValueError(f"mqtt topic is broker-internal ($-prefixed): {topic!r}")
    levels = topic.split("/")
    for i, level in enumerate(levels):
        if "#" in level:
            if level != "#" or i != len(levels) - 1:
                raise ValueError(f"mqtt topic has '#' in an invalid position: {topic!r}")
        if "+" in level and level != "+":
            raise ValueError(f"mqtt topic has '+' in an invalid position: {topic!r}")
    return topic


def _mqtt_client_kwargs(secret: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Broker auth kwargs from the resolved secret object (username/password,
    optional client cert paths). Never logs values; only key names are ever
    mentioned."""
    kwargs: Dict[str, Any] = {}
    if secret:
        if secret.get("username"):
            kwargs["username"] = secret["username"]
        if secret.get("password"):
            kwargs["password"] = secret["password"]
    return kwargs


class MqttSubscriber:
    """Message-bus subscriber for an MQTT topic — same contract as
    KafkaSubscriber: constructed with (trigger_id, topic), `.start()` blocks in
    paho's forever-loop and dispatches each message through the trigger
    pipeline.

    paho-mqtt is imported LAZILY inside start() so this module keeps importing
    (and the create-gate keeps working) on deployments without the client
    installed; when it's missing, start() raises ImportError and the caller
    logs DEGRADED. QoS 1 (never re-send on reconnect); reconnect backoff is
    ours (1s -> 60s with jitter), NOT paho's — every reconnect re-resolves and
    re-vets the broker address (see start())."""

    def __init__(self, trig_id: int, topic: str, *, tenant_id: Optional[int] = None,
                 url: Optional[str] = None, secret_ref: Optional[str] = None):
        self.trig_id = trig_id
        self.topic = validate_mqtt_topic(topic)
        self.tenant_id = tenant_id
        self.secret_ref = secret_ref
        self.url = url or os.getenv("MQTT_URL", "mqtt://localhost:1883")
        self.client_id = f"mod-trig-{trig_id}-{os.urandom(3).hex()}"
        self.client = None
        self._stop_event = threading.Event()
        self._torn_down = False
        self._peer_refused = False
        self._vetted = False
        self._blocked_reason = self._url_problem()

    def _url_problem(self) -> Optional[str]:
        """Return a refusal reason for the broker URL (SSRF / userinfo / bad
        scheme / bad port), else None. Credentials must come from secret_ref,
        never the URL — a URL with userinfo is refused outright."""
        u = urlparse(self.url)
        if u.scheme not in ("mqtt", "mqtts"):
            return f"scheme must be mqtt:// or mqtts:// (got {u.scheme or '?'})"
        if u.username or u.password:
            return ("URL carries userinfo credentials — broker credentials "
                    "come only from the trigger's secret_ref")
        if not u.hostname:
            return "no host"
        try:
            port = u.port
        except ValueError as e:  # urlparse raises on an out-of-range port
            return f"broker port invalid: {e}"
        port = port or (8883 if u.scheme == "mqtts" else 1883)
        if not 1 <= port <= 65535:
            return f"broker port out of range: {port}"
        return url_is_blocked(self.url)

    def stop(self) -> None:
        """Request a clean shutdown of the subscribe loop (idempotent: a second
        call never re-tears down an already-torn-down client)."""
        self._stop_event.set()
        if self.client is None or self._torn_down:
            return
        self._torn_down = True
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> None:
        """Connect + subscribe until stopped (daemon-thread target).

        Every connection attempt re-resolves the broker host and re-runs the
        SSRF guard, so a host that rebinds to a private/loopback address
        between attempts is refused before paho ever opens a socket. paho's own
        reconnect machinery is deliberately NOT used (no loop_forever retry
        loop, no reconnect_delay_set): reconnects are ours, so each one goes
        through the same resolve+vet step. Plain mqtt connects to the pinned,
        vetted IP; mqtts connects to the ORIGINAL hostname so TLS SNI +
        certificate hostname verification stay ON, and the peer address is
        re-checked in on_connect (disconnect + refuse if it is blocked).
        """
        from database import SessionLocal
        from models import WorkflowTrigger
        from services.triggers import pipeline

        if self._blocked_reason:
            # URL policy is checked BEFORE the client import so a refused
            # broker never even needs the lib present.
            logger.warning("message-bus(mqtt) trigger %s NOT STARTED: broker URL "
                           "refused: %s", self.trig_id, self._blocked_reason)
            return
        import paho.mqtt.client as mqtt_lib  # lazy: absent lib -> ImportError
        host = urlparse(self.url).hostname
        tls = urlparse(self.url).scheme == "mqtts"
        client = mqtt_lib.Client()
        self._apply_identity(client)
        if tls:
            # System CA bundle, CERT_REQUIRED, and hostname verification left
            # enabled (tls_insecure_set(False)) — there is no insecure path.
            client.tls_set()
            insecure = getattr(client, "tls_insecure_set", None)
            if insecure:
                insecure(False)
            logger.info("message-bus(mqtt) TLS enabled (verify hostname + chain) "
                        "for trigger %s host=%s", self.trig_id, host)
        secret = _resolve_message_bus_secret(self.tenant_id, self.secret_ref)
        kwargs = _mqtt_client_kwargs(secret)
        if kwargs:
            logger.info("message-bus(mqtt) broker auth applied for trigger %s host=%s "
                        "(keys: %s)", self.trig_id, host, sorted(kwargs))
        client.username = kwargs.get("username", client.username)
        client.password = kwargs.get("password", client.password)
        self.client = client
        client.on_message = self._on_message
        client.on_connect = self._on_connect
        backoff = 1.0
        while not self._stop_event.is_set():
            target = self._vetted_target(host)
            if target is None:
                return  # policy refusal: not a transient error, do not retry
            self._vetted = False
            self._peer_refused = False
            try:
                client.connect(host=target, port=self._broker_port())
                # SUBSCRIBE happens from on_connect — only after the broker's
                # CONNACK arrives (and, for mqtts, the real peer re-check
                # passes) — never right after connect() returns, since
                # connect() does not wait for the network loop/CONNACK.
                logger.info("message-bus(mqtt) subscriber connecting topic=%s host=%s "
                            "target=%s for trigger %s", self.topic, host, target,
                            self.trig_id)
                loop = getattr(client, "start_loop", None)
                if loop:
                    loop()
                else:
                    client.loop_forever(retry_first_connection=False)
                if self._peer_refused:
                    logger.warning("message-bus(mqtt) trigger %s REFUSED: broker "
                                   "peer address is private/loopback", self.trig_id)
                    return
                return  # loop ended cleanly (stop() or socket closed)
            except Exception:  # noqa: BLE001 — connection/loop error: back off
                if self._stop_event.is_set():
                    return
                wait = backoff + random.uniform(0, backoff * 0.25)
                logger.exception(
                    "message-bus(mqtt) subscriber error (trigger=%s topic=%s host=%s); "
                    "retrying in %.1fs", self.trig_id, self.topic, host, wait)
                self._stop_event.wait(wait)  # honours stop() during the backoff
                backoff = min(backoff * 2.0, 60.0)

    def _apply_identity(self, client: Any) -> None:
        """client_id + QoS 1 (never re-send a message on reconnect). paho's
        reconnect delay is NOT set — reconnects are ours (see start()), so
        every one of them re-resolves + re-vets the broker address."""
        setter = getattr(client, "set_client_id", None)
        if setter:
            setter(self.client_id)
        else:
            client.client_id = self.client_id
        qos = getattr(client, "set_qos", None)
        if qos:
            qos(1)
        # paho's own reconnect machinery is OFF: reconnects are ours (see
        # start()), so each one re-resolves + re-vets the broker address.
        if hasattr(client, "reconnect_on_disconnect"):
            client.reconnect_on_disconnect = False

    def _broker_port(self) -> int:
        """Broker port: explicit, else the scheme's standard one (1883 plain /
        8883 TLS). Raises ValueError for an out-of-range port."""
        port = urlparse(self.url).port or (8883 if urlparse(self.url).scheme
                                           == "mqtts" else 1883)
        if not 1 <= port <= 65535:
            raise ValueError(f"mqtt broker port out of range: {port}")
        return port

    def _vetted_target(self, host: str) -> Optional[str]:
        """Resolve the broker host (AF_UNSPEC/TCP) and return the address paho
        should connect to, or None when the attempt must be refused.

        Every resolved address must pass the SSRF guard — one blocked address in
        the list is enough to refuse (a broker that rebinds between attempts is
        caught here, since this runs on every reconnect). Plain mqtt connects to
        the pinned vetted IP; mqtts connects to the original hostname so the
        TLS peer/hostname check still applies, with the peer re-checked in
        on_connect."""
        try:
            infos = socket.getaddrinfo(host, self._broker_port(),
                                       proto=socket.IPPROTO_TCP)
        except Exception as e:  # noqa: BLE001 — unresolvable: retry, don't connect
            logger.warning("message-bus(mqtt) trigger %s host %s did not resolve: %s",
                           self.trig_id, host, e)
            return None
        addrs = []
        for fam, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if ip not in addrs:
                addrs.append(ip)
        if not addrs:
            logger.warning("message-bus(mqtt) trigger %s host %s resolved to nothing",
                           self.trig_id, host)
            return None
        blocked = [a for a in addrs if _ip_blocked(a)]
        if blocked:
            logger.warning("message-bus(mqtt) trigger %s REFUSED: host %s resolves to "
                           "a private/loopback address (%s)", self.trig_id, host,
                           blocked[0])
            return None
        return host if urlparse(self.url).scheme == "mqtts" else addrs[0]

    def _on_connect(self, client: Any, _userdata, _flags, rc, *_rest) -> None:
        """Fires only from inside the network loop (after the broker's
        CONNACK), never inline from connect(). SUBSCRIBE is issued ONLY from
        here — not right after connect() — so nothing goes out on the wire
        before the broker accepted us and (for mqtts) the real peer has been
        re-checked. In paho-mqtt 2.x `client.socket` is a METHOD, not an
        attribute — it must be called. If the peer can't even be determined,
        fail closed (treat it the same as a blocked peer)."""
        rc_val = getattr(rc, "value", rc)
        if rc_val != 0:
            logger.warning("message-bus(mqtt) trigger %s connect refused (rc=%s)",
                           self.trig_id, rc_val)
            return
        if urlparse(self.url).scheme == "mqtts":
            peer = None
            try:
                sock = client.socket()
                peer = sock.getpeername()[0] if sock else None
            except Exception:  # noqa: BLE001 — can't determine peer: fail closed
                peer = None
            if peer is None or _ip_blocked(peer):
                self._peer_refused = True
                logger.warning("message-bus(mqtt) trigger %s REFUSED: broker peer "
                               "address could not be verified safe", self.trig_id)
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                return
        self._vetted = True
        try:
            client.subscribe(topic=self.topic, qos=1)
            logger.info("message-bus(mqtt) subscriber on topic=%s host=%s subscribed "
                        "(post-CONNACK) for trigger %s", self.topic,
                        urlparse(self.url).hostname, self.trig_id)
        except Exception:  # noqa: BLE001 — subscribe failure: let the loop retry
            logger.exception("message-bus(mqtt) subscribe failed for trigger %s",
                             self.trig_id)

    def _on_message(self, _client, _msg, msg) -> None:  # type: ignore
        """paho on_message callback -> same fire path as the kafka subscriber.
        Payloads over the cap are dropped + logged (never truncated into a run)."""
        from database import SessionLocal
        from models import WorkflowTrigger
        from services.triggers import pipeline
        if not self._vetted:
            # Belt-and-braces: a message arriving before on_connect has
            # finished vetting (rc==0 and, for mqtts, the peer check) is
            # ignored outright — it should never happen since subscribe()
            # itself only runs post-vetting, but this is a second guard.
            return
        if self._stop_event.is_set():
            return
        try:
            payload = msg.payload
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", "replace")
            payload = str(payload)
            if len(payload) > _MQTT_MAX_PAYLOAD:
                logger.warning("message-bus(mqtt) trigger %s dropped oversize payload "
                               "(%d > %d chars) on topic=%s",
                               self.trig_id, len(payload), _MQTT_MAX_PAYLOAD, self.topic)
                return
            with SessionLocal() as db:
                trig = (db.query(WorkflowTrigger)
                        .filter(WorkflowTrigger.id == self.trig_id).first())
                if trig and trig.is_active:
                    pipeline.evaluate_and_dispatch(
                        db, trig, {"message": payload,
                                   "inputs": {"message": payload}})
        except Exception:  # noqa: BLE001 — one bad message must not kill the loop
            logger.exception("message-bus(mqtt) handler failed for trigger %s",
                             self.trig_id)

    def _decode(self, msg: Any) -> str:
        """Normalise an mqtt payload (bytes/str) to text (mirrors kafka's)."""
        data = getattr(msg, "payload", msg)
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return str(data)


def _mqtt_subscriber(sub: MqttSubscriber) -> None:
    """Daemon-thread target: start the MQTT subscriber, logging DEGRADED (not
    crashing the API) when the paho client lib isn't installed."""
    try:
        sub.start()
    except ImportError:
        logger.warning("message-bus(mqtt) trigger %s DEGRADED: paho-mqtt client "
                       "not installed (pip install paho-mqtt + a reachable broker)",
                       sub.trig_id)
    except Exception:  # noqa: BLE001
        logger.exception("message-bus(mqtt) subscriber died for trigger %s", sub.trig_id)


def _kafka_subscriber(sub: KafkaSubscriber) -> None:
    """Daemon-thread target: start the subscriber, logging DEGRADED (not
    crashing the API) when the kafka client lib isn't installed."""
    try:
        sub.start()
    except ImportError:
        logger.warning("message-bus(kafka) trigger %s DEGRADED: kafka client "
                       "not installed (pip install kafka-python + a reachable broker)",
                       sub.trig_id)
    except Exception:  # noqa: BLE001
        logger.exception("message-bus(kafka) subscriber died for trigger %s", sub.trig_id)


def start_message_bus_subscribers() -> None:
    """Start subscribers for active MESSAGE_BUS triggers (opt-in TRIGGER_STREAMING).
    Redis works anywhere; other providers are logged DEGRADED when their client lib
    isn't installed — never silently pretended-working."""
    global _subscribers_started
    if _subscribers_started or os.getenv("TRIGGER_STREAMING", "false").lower() != "true":
        return
    _subscribers_started = True
    try:
        from database import SessionLocal
        from models import WorkflowTrigger, WorkflowTriggerType
        with SessionLocal() as db:
            triggers = (db.query(WorkflowTrigger).filter(
                WorkflowTrigger.trigger_type == WorkflowTriggerType.MESSAGE_BUS,
                WorkflowTrigger.is_active.is_(True),
                WorkflowTrigger.deleted_at.is_(None)).all())
            for trig in triggers:
                cfg = trig.configuration or {}
                provider, topic = cfg.get("provider"), cfg.get("topic")
                if not provider_available(provider):
                    logger.warning("message-bus trigger %s DEGRADED: provider %r client "
                                   "not installed/available", trig.id, provider)
                    continue
                if provider == "redis":
                    threading.Thread(target=_redis_subscriber, args=(trig.id, topic),
                                     daemon=True, name=f"MsgBus-{trig.id}").start()
                elif provider == "kafka":
                    try:
                        sub = KafkaSubscriber(trig.id, topic, tenant_id=trig.tenant_id, config=cfg)
                    except ValueError as e:
                        logger.warning("message-bus(kafka) trigger %s DEGRADED: %s", trig.id, e)
                        continue
                    threading.Thread(target=_kafka_subscriber, args=(sub,),
                                     daemon=True,
                                     name=f"MsgBus-kafka-{trig.id}").start()
                elif provider == "mqtt":
                    try:
                        sub = MqttSubscriber(trig.id, topic, tenant_id=trig.tenant_id,
                                             url=cfg.get("url"),
                                             secret_ref=cfg.get("secret_ref"))
                    except ValueError as e:
                        logger.warning("message-bus(mqtt) trigger %s DEGRADED: %s",
                                       trig.id, e)
                        continue
                    threading.Thread(target=_mqtt_subscriber, args=(sub,),
                                     daemon=True,
                                     name=f"MsgBus-mqtt-{trig.id}").start()
                else:
                    # nats/rabbitmq: client importable but driving them needs
                    # a reachable broker + per-provider loop — wired per deployment.
                    logger.info("message-bus trigger %s (%s) client available; broker "
                                "subscriber is deployment-wired", trig.id, provider)
    except Exception as e:  # noqa: BLE001
        logger.warning("start_message_bus_subscribers failed: %s", e)
