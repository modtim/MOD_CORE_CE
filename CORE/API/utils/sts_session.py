"""GSEC Q21.2 — scoped, short-lived S3 STS session credentials for BYO-S3
worker tasks.

When a tenant's BYO-S3 ``StorageConnection`` has ``sts_enabled`` and the
claiming worker runs the Q21 agent, the credentials endpoint mints an
AssumeRole session instead of shipping the tenant's static key: the session
is scoped by a session policy to the tenant's own bucket with minimal
actions, and lives for a TTL read from the platform setting
``worker.sts_session_ttl_s`` (default 2h). The worker refresher
(Q21.7) re-calls the endpoint at ~TTL/2 for the lifetime of the task.

Layout note: MOD task object keys are content-addressed (the content hash
IS the flat object key; inputs may reference blobs from earlier runs), so
the runtime scope is the whole tenant bucket. ``build_session_policy``
still takes a ``prefix`` so a caller that CAN express a prefix (e.g. a
task whose keys all sit under one directory) can tighten it; see
``worker_tasks.get_task_credentials`` for the runtime choice.

The builder functions here are pure and unit-testable without network;
``assume_role`` is the thin SigV4-signed AssumeRole POST.
"""

import datetime
import hashlib
import hmac
import json
import logging
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TTL_S = 7200
MIN_SESSION_TTL_S = 900
MAX_SESSION_TTL_S = 43200

# Minimal task-container actions: fetch inputs, write outputs, list the
# bucket (mc ls/stat resolution). No Delete*, no bucket management.
MINIMAL_TASK_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
)

DEFAULT_ROLE_ARN = "arn:aws:iam::000000000000/root"


def clamp_ttl(ttl_s) -> int:
    """Clamp a requested session TTL into the sane STS window [15m, 12h]."""
    try:
        v = int(ttl_s)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_TTL_S
    if v <= 0:
        return DEFAULT_SESSION_TTL_S
    return max(MIN_SESSION_TTL_S, min(MAX_SESSION_TTL_S, v))


def refresh_interval_s(ttl_s) -> int:
    """Refresh cadence to advertise to the worker: half the TTL, never
    below 30s (a 15m floor TTL would otherwise refresh every ~45s)."""
    return max(30, int(clamp_ttl(ttl_s)) // 2)


def session_name(task_id: int, tenant_id: int) -> str:
    """Audit-traceable session name; STS caps this at 64 chars."""
    name = f"mod-task-{task_id}-t{tenant_id}"
    return name[:64]


def normalize_prefix(prefix: Optional[str]) -> Optional[str]:
    """Normalize a key prefix for ARN use: strip scheme noise and trailing
    slashes; empty/None means 'no prefix restriction'."""
    if prefix is None:
        return None
    p = str(prefix).strip().lstrip("/")
    while p.endswith("/"):
        p = p[:-1]
    return p or None


def build_session_policy(
    bucket: str,
    prefix: Optional[str] = None,
    actions: Optional[tuple] = None,
) -> dict:
    """Build the AssumeRole session policy: the tenant bucket only, minimal
    actions, optional key-prefix scoping on the object resource."""
    if not bucket:
        raise ValueError("build_session_policy: bucket is required")
    acts = tuple(actions) if actions else MINIMAL_TASK_ACTIONS
    obj_prefix = normalize_prefix(prefix)
    obj_resource = f"arn:aws:s3:::{bucket}/{obj_prefix}/*" if obj_prefix else f"arn:aws:s3:::{bucket}/*"
    object_actions = [a for a in acts if not a.startswith("s3:ListBucket") and a != "s3:GetBucketLocation"]
    bucket_actions = [a for a in acts if a in ("s3:ListBucket", "s3:GetBucketLocation")]
    statements = [
        {
            "Sid": "TenantObjects",
            "Effect": "Allow",
            "Action": object_actions,
            "Resource": [obj_resource],
        },
    ]
    if bucket_actions:
        statements.append(
            {
                "Sid": "TenantBucketList",
                "Effect": "Allow",
                "Action": bucket_actions,
                "Resource": [f"arn:aws:s3:::{bucket}"],
            }
        )
    return {
        "Version": "2012-10-17",
        "Statement": statements,
    }


def session_policy_json(bucket: str, prefix: Optional[str] = None,
                        actions: Optional[tuple] = None) -> str:
    """The session policy serialized for the AssumeRole ``Policy`` field."""
    return json.dumps(build_session_policy(bucket, prefix=prefix, actions=actions))


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k = _hmac(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _amz3_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _signed_body(endpoint: str, scheme: str, body: bytes, *, access_key: str,
                 secret_key: str, region: str, service: str) -> tuple:
    """SigV4 Authorization header for a form-POST to ``/`` on endpoint."""
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = endpoint.split("/", 1)[0]
    canonical_uri = "/"
    payload_hash = _amz3_sha256(body)
    headers = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join([
        "POST", canonical_uri, "",
        canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    ), amz_date


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_assume_role_response(root: ET.Element) -> dict:
    """Parse the AWS-style AssumeRole XML response (namespace-agnostic).
    Raises ValueError when the payload is an error response."""
    fields = {}
    for el in root.iter():
        t = _strip_ns(el.tag)
        if t == "Error" or t == "Code":
            code = el.text or t
            raise ValueError(f"AssumeRole failed: {code}")
        if t in ("AccessKeyId", "SecretAccessKey", "SessionToken"):
            fields[t] = (el.text or "").strip()
        elif t == "Expiration" and el.text:
            fields["Expiration"] = el.text.strip()
    missing = [k for k in ("AccessKeyId", "SecretAccessKey", "SessionToken") if k not in fields]
    if missing:
        raise ValueError(f"AssumeRole response missing fields: {missing}")
    expires_at = fields.get("Expiration")
    dt: Optional[datetime.datetime] = None
    if expires_at:
        try:
            dt = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None or dt.tzinfo is None:
        # Never ship a session without a trustworthy expiry — the worker
        # refresher schedules off it.
        raise ValueError("AssumeRole response has no usable Expiration")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return {
        "access_key": fields["AccessKeyId"],
        "secret_key": fields["SecretAccessKey"],
        "session_token": fields["SessionToken"],
        "expires_at": dt.astimezone(datetime.timezone.utc),
    }


def assume_role(
    endpoint: str,
    *,
    access_key: str,
    secret_key: str,
    bucket: str,
    session_name: str,
    ttl_seconds: int,
    session_policy: Optional[dict] = None,
    region: str = "us-east-1",
    role_arn: str = DEFAULT_ROLE_ARN,
    secure: bool = True,
    timeout_s: float = 15.0,
    prefix: Optional[str] = None,
) -> dict:
    """SigV4-signed AssumeRole POST against the tenant's own S3 endpoint
    (self-hosted S3 serves AssumeRole on the same endpoint; an AWS-side
    endpoint would carry its STS host here).

    Returns ``{"access_key", "secret_key", "session_token", "expires_at"}``
    (expires_at: tz-aware UTC datetime). Raises on any failure — the caller
    falls back to the static key bundle and alerts.
    """
    import requests  # local import: keep module import cheap + offline-safe

    scheme = "https" if secure else "http"
    url = f"{scheme}://{endpoint}/"
    policy = session_policy if session_policy is not None else build_session_policy(bucket, prefix=prefix)
    body = "&".join([
        "Action=AssumeRole",
        "Version=2011-06-15",
        requests.utils.quote(f"RoleArn={role_arn}"),
        requests.utils.quote(f"RoleSessionName={session_name}"),
        f"DurationSeconds={int(clamp_ttl(ttl_seconds))}",
        requests.utils.quote(f"Policy={json.dumps(policy)}"),
    ]).encode("utf-8")
    last_err: Optional[Exception] = None
    # AWS signs STS calls with service "sts"; self-hosted S3 (MinIO-style)
    # verifies its endpoint's SigV4 under service "s3". Try both, in order.
    for service in ("sts", "s3"):
        try:
            authorization, amz_date = _signed_body(
                endpoint, scheme, body,
                access_key=access_key, secret_key=secret_key,
                region=region, service=service,
            )
            resp = requests.post(
                url,
                data=body,
                headers={
                    "Authorization": authorization,
                    "x-amz-date": amz_date,
                    "x-amz-content-sha256": _amz3_sha256(body),
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                },
                timeout=timeout_s,
            )
            if resp.status_code != 200:
                last_err = ValueError(
                    f"AssumeRole HTTP {resp.status_code} from {endpoint} (service={service})")
                continue
            root = ET.fromstring(resp.content)
            return _parse_assume_role_response(root)
        except Exception as e:  # noqa: BLE001 — fall through to next service / raise
            last_err = e
    raise ValueError(f"AssumeRole failed against {endpoint}: {last_err}")
