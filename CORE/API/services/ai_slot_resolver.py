"""ai-config-stack-binding 2.1 — the typed slot resolver (design D2).

One entry point, one truth: :func:`resolve_slot(db, slot, tenant_id)` answers
"what serves this AI role right now, and if nothing does, WHY in a sentence an
operator can act on". The old shape — :func:`routers.ai_config._resolved_effective_env`
degrading every failure to ``{}`` and a side-channel
(:func:`routers.ai_config._stack_provider_error`) bolted on beside it — cannot
tell "nobody ever set this slot up" from "the stack you picked is asleep", and
that difference is exactly what a human needs (start it vs pick one).

The outcome is a discriminated union (design D2):

  ready          — env dict + the resolved target (stack id/name, base_url, model)
  no_binding     — nobody picked anything for this slot (or this tenant turned it off)
  stack_stopped  — names the stack and its status
  stack_missing  — the bound stack row is gone / soft-deleted (FK SET NULL left a NULL)
  unreachable    — names host:port, from the BOUNDED probe only (never a guess)
  waking         — a sleeping bound stack is being started (carries progress)
  stack_waking_timeout — 4.1 (``wake=True`` only): the bound stack was woken and
                   is still starting after the bounded wait — retry the SAME
                   slot, never a switch to another model
  stack_wake_held      — 4.1 (``wake=True`` only): it is asleep and no card can
                   take it now (D7 hold), so the caller retries

Every non-``ready`` carries a human sentence naming the cause and the remedy
("an admin picks one in Settings -> AI"), plus ``attempts`` — one entry per row
of the slot's ordered fallback chain (owner BUG.B56: ``priority`` 0 = primary,
1..15 = fallbacks tried in order) so a chain that ends in failure reports each
failure's cause, not just the last one.

Owner decisions honoured here
-----------------------------
Q-AI1 = B  no hidden fallback: only the user-configured chain is walked, and
           every failure is reported with its cause. Nothing here reaches
           another slot, another tenant's stack, or a Vault alias.
Q-AI2 = A  a tenant binding overrides the platform binding for that slot. A
           tenant that has rows for the slot — even all-disabled ones — has
           SPOKEN: it never silently inherits the platform chain (5.1).
Q-AI5 = B  ``target_kind='auto'`` was resolved ONCE at save. Reading it here
           re-runs the existing managed-AI placement (``resolve_role``) and
           reports which stack that placement lands on — it is never a
           binding to a stack that does not exist.
Q-AI6 = A  a tenant may bind to a PLATFORM stack, so stack targets are looked
           up unscoped (``get_stack_by_id``), not tenant-scoped.

Never a guessed host: reachability is the bounded probe
(:func:`routers.ai_config._host_resolves_bounded`, BUG.B45) — an unbounded
``getaddrinfo`` walks a ~3.6s retry ladder on the request path. Never key
material: an external target contributes ``key_ref`` (a Vault PATH) only, and
``LLM_API_KEY`` stays empty so no Authorization header is sent from here.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import case
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

CHAT_SLOTS = ("wizard", "adapter", "assistant", "support", "repair", "critic")
EMBEDDING_SLOTS = ("wizard_learning_embed", "assistant_user_rag_embed",
                   "assistant_knowledge_embed")
SLOTS = CHAT_SLOTS + EMBEDDING_SLOTS

REMEDY = "Ask an admin to pick one in Settings -> AI Configuration."
# A status that means "someone already started it" — the retryable case (D4).
WAKING_STATUSES = ("deploying", "starting", "waking", "provisioning")


def resolve_slot(db: Session, slot: str,
                 tenant_id: Optional[int] = None, *,
                 wake: bool = False,
                 on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Resolve one AI slot to a typed outcome. Never raises.

    Returns ``{"outcome", "message", "attempts", "target", "env",
    "progress"}``:

    * ``outcome`` — ``ready | no_binding | stack_stopped | stack_missing |
      unreachable | waking``. On a chain that fails, it is the PRIMARY's
      (``priority`` 0) outcome — the thing the operator picked is the thing
      to report; the fallbacks are listed in ``attempts``.
    * ``message`` — the human sentence for that outcome, naming the cause
      (stack name + status, or ``host:port``) and the remedy.
    * ``attempts`` — ``[{priority, outcome, message}, ...]`` for the whole
      chain, in priority order, so "the primary is stopped, the fallback is
      missing" is one read.
    * ``target`` — ``{slot, capability, source, priority, stack_id, stack_name,
      base_url, model, key_ref}`` for the row that answered (``ready``) or the
      primary (failure). ``key_ref`` is a Vault PATH, never a secret.
    * ``env`` — the ``LLM_*`` dict, ONLY on ``ready`` (never a diagnostic key
      leaking into a task container — that is why this is a second entry point
      beside :func:`routers.ai_config._resolved_effective_env`).
    * ``progress`` — ``waking`` only: what the wake has already done, so a
      feature can show "Starting the AI model..." and retry.
    """
    r = _Resolver(db, tenant_id, wake=wake, on_progress=on_progress)
    return r.resolve(slot)


class _Resolver:                                    # internal: see resolve_slot
    def __init__(self, db: Session,
                 tenant_id: Optional[int] = None,
                 probe_timeout_s: Optional[float] = None, *,
                 wake: bool = False,
                 on_progress: Optional[Callable[[str], None]] = None):
        self.db, self.tenant_id = db, tenant_id
        # None = the module's own budget (ai_config._HOST_RESOLVE_PROBE_TIMEOUT_S)
        self.probe_timeout_s = probe_timeout_s
        # 4.1 opt-in: a sleeping bound stack is STARTED (and polled) instead of
        # being reported stopped. Default False — every existing caller, and
        # every existing test, keeps the byte-for-byte `stack_stopped` answer.
        self.wake = bool(wake)
        self.on_progress = on_progress
        self.woken: Dict[str, int] = {}   # stack_id -> times woken (once each)
        # 5.1: set by the ONE chain SELECT when it saw a tenant row for this
        # slot (live or retired) — "the tenant spoke", without a second query.
        self.spoken = False

    def resolve(self, slot: str) -> Dict[str, Any]:
        self.slot = slot
        self.capability = ("embedding" if slot in EMBEDDING_SLOTS else "chat")
        rows = self._chain_rows()
        if not rows:
            # 5.1: a tenant that retired every row for this slot has TURNED IT
            # OFF — that is a different sentence from "nobody ever picked".
            if self._tenant_spoken():  # 5.1: read off the chain SELECT
                return self._no_binding(
                    f"This tenant turned the {slot.replace('_', ' ')} slot "
                    f"off (every binding for it is retired). " + REMEDY)
            return self._no_binding(
                f"No AI model is bound to the {slot.replace('_', ' ')} slot. "
                + REMEDY)
        attempts: List[Dict[str, Any]] = []
        extras: List[Dict[str, Any]] = []
        for row in rows:
            outcome, extra = self._attempt(row)
            extras.append(extra)
            attempts.append({"priority": int(row.priority or 0),
                             "outcome": outcome,
                             "message": extra.get("message") or ""})
            if outcome == "ready":
                return self._finish(row, "ready", extra, attempts)
            if self.wake and outcome in ("stack_waking_timeout",
                                         "stack_wake_held"):
                # 4.1: the operator picked THIS stack and it is being started
                # for them — falling through to a fallback would be a silent
                # model switch (Q-AI1 = B). Report the retryable cause.
                return self._finish(row, outcome, extra, attempts)
        # The whole chain failed. Report the PRIMARY's cause — the thing the
        # operator picked is the thing to report — and list every attempt's
        # cause so "primary stopped, fallback missing" is one read (Q-AI1 = B:
        # nothing here silently swaps in another model).
        primary = rows[0]
        return self._finish(primary, attempts[0]["outcome"],
                            extras[0], attempts)

    # ------------------------------------------------------------------
    # The chain: which rows this slot's answer may come from (Q-AI2 = A)
    # ------------------------------------------------------------------
    def _chain_rows(self) -> List[Any]:
        """One SQL step: the tenant's rows for the slot if it has any, else the
        platform's, each ordered by ``priority`` (owner BUG.B56).

        RETIRED rows (``enabled=false`` / ``retired_at`` stamped — ISO 27001
        soft delete, never a DELETE) are filtered out here, so the history of
        "this slot once ran on that stack" stays readable without ever being
        served.

        A tenant that HAS rows for the slot but has disabled all of them has
        spoken: it gets no_binding, not the platform chain (5.1: "this tenant
        turned this slot off").

        No session at all (a caller that has none, e.g. the transitional
        wrapper in :mod:`routers.ai_config`) reads as "no rows" — the clean
        ``no_binding`` the task prompt asks for when the table is not in play,
        never a crash.

        5.1 (Q-AI2 = A) does the whole chain in ONE SELECT — tenant rows
        (``scope='tenant'`` for this tenant) UNION platform rows, one
        ``ORDER BY`` with the scope CASE putting tenant first — so a disabled
        tenant row shadows the platform row without a second query, and no
        env/Vault fallback ever steps in. "Did the tenant speak?" is read off
        the rows this SELECT returned (:meth:`_tenant_spoke`), not from an
        extra EXISTS query.
        """
        if self.db is None:
            return []
        return self._chain_sql()

    def _chain_sql(self) -> List[Any]:
        """The whole chain in one SELECT (5.1: no second query, no fallback).

        The SELECT carries BOTH scopes — a tenant row counts only when it is
        this tenant's — and one ``ORDER BY case(scope=='tenant',0,1),
        priority, id`` puts the tenant's rows first. Back in Python the
        precedence is then a split of THAT result set: any tenant row (live or
        retired) means the tenant spoke, so only its own live rows are the
        chain; with no tenant row at all the platform's live rows are.
        """
        B = models.AiSlotBinding
        self.spoken = False
        # A caller without a tenant_id can still see the platform rows; the
        # tenant_id comparison then renders ``tenant_id IS NULL`` and matches
        # no tenant row (tenant rows always carry one).
        mine_clause = (B.scope == "tenant") & (B.tenant_id == self.tenant_id)
        try:
            found = (self.db.query(B)
                     .filter(B.slot == self.slot,
                             (B.scope == "platform") | mine_clause)
                     .order_by(case((B.scope == "tenant", 0), else_=1),
                               B.priority, B.id)
                     .all())
        except Exception:  # noqa: BLE001 — no table / no such column yet
            logger.debug("ai_slot_binding unavailable for slot %s", self.slot,
                         exc_info=True)
            return []
        tenant_rows = [r for r in found if r.scope == "tenant"]
        self.spoken = bool(tenant_rows)
        if tenant_rows:
            # The tenant spoke: retired rows stay out of the chain.
            return [r for r in tenant_rows if self._live_row(r)]
        return [r for r in found if r.scope == "platform" and self._live_row(r)]

    def _live_row(self, row) -> bool:
        """Live = not retired (``enabled=false`` / ``retired_at`` stamped)."""
        return row.enabled is True and getattr(row, "retired_at", None) is None

    def _tenant_spoken(self) -> bool:
        """Did the tenant speak for this slot? Read off the chain SELECT
        (:meth:`_chain_sql`) — 5.1 forbids a second query for it."""
        return bool(self.spoken)

    def _no_binding(self, message: str) -> Dict[str, Any]:
        return self._finish(None, "no_binding", {"message": message}, [])

    # ------------------------------------------------------------------
    # One row -> one outcome
    # ------------------------------------------------------------------
    def _attempt(self, row) -> tuple:
        """(outcome, extra) for one binding row. Never raises, never guesses."""
        if row.target_kind == "stack":
            return self._try_stack(row)
        if row.target_kind == "external":
            return self._try_external(row)
        if row.target_kind == "auto":
            return self._try_auto(row)
        return ("no_binding", {
            "message": (f"Binding #{row.id} for the {self.slot} slot names an "
                        f"unknown target kind '{row.target_kind}'. "
                        + REMEDY)})

    def _try_stack(self, row) -> tuple:
        """A bound PersistentStack: exists, running, reachable."""
        if not row.stack_id:
            # FK is ON DELETE SET NULL: deleting/soft-deleting the stack leaves
            # the binding record (ISO 27001) but nulls the pointer, and the
            # denormalized name keeps the row readable — that is the cause.
            named = (f"Stack '{row.stack_name}'" if row.stack_name
                     else "The bound stack")
            return ("stack_missing", {
                "message": (f"{named} is no longer in the system "
                            f"(binding #{row.id} still points at it). "
                            + REMEDY)})
        from crud.persistent_stacks_domain import get_stack_by_id
        stack = get_stack_by_id(self.db, int(row.stack_id))
        if stack is None:
            named = (f"Stack '{row.stack_name or '?'}'"
                     f" (#{row.stack_id})")
            return ("stack_missing", {
                "message": (f"{named} is no longer in the system. "
                            + REMEDY)})
        if stack.status != "running":
            if stack.status in WAKING_STATUSES:
                return ("waking", {
                    "message": (f"Stack '{stack.name}' (#{stack.id}) is "
                                f"{stack.status} — it is being started for this "
                                f"slot; try again once it is serving."),
                    "progress": (f"Starting the AI model '{stack.name}': "
                                 f"status={stack.status}."),
                })
            stopped = ("stack_stopped", {
                "message": (f"Stack '{stack.name}' (#{stack.id}) is "
                            f"{stack.status}, not running — it cannot serve "
                            f"the {self.slot} slot until it is started. "
                            + REMEDY)})
            if not self.wake:
                return stopped
            # 4.1 (opt-in): someone already picked this stack, so the slot's
            # job is to start IT and answer — never to swap in another model
            # (Q-AI1 = B). The wake is stack_wake's own; the wait is bounded by
            # the existing idle budget. On a wake that did not finish, that
            # named retryable outcome IS the answer for this row.
            woken, woken_extra = self._wake_for(stack)
            if woken != "ready":
                return (woken, woken_extra)
            # It is serving now: fall through and resolve its endpoint exactly
            # as a running stack does.
        from services.ai_stack_provider import resolve_stack_endpoint
        try:
            base_url, model_name = resolve_stack_endpoint(
                self.db, int(row.stack_id), row.model,
                # Q-AI6 = A: a tenant may bind a PLATFORM stack, so the lookup
                # is unscoped here (ai_stack_provider's own scope check would
                # refuse it).
                None)
        except Exception as e:                     # StackProviderError + friends
            return self._classify_stack_error(row, e)
        host = _host_of(base_url)
        if host and not self._probe(host):
            return ("unreachable", {
                "message": (f"Stack '{stack.name}' (#{stack.id}) is running "
                            f"but nothing answers at {_host_port_of(base_url) or host} "
                            f"— the route to it is not open from here. "
                            + REMEDY)})
        return ("ready", {
            "target": {"stack_id": int(row.stack_id),
                       "stack_name": stack.name,
                       "base_url": base_url, "model": model_name},
            # A stack target is an internal endpoint: no key travels with it
            # (matches `_resolved_effective_env`'s provider=="stack" branch,
            # which sends LLM_API_KEY empty so no Authorization header is sent).
            "env": self._env_for(row, base_url, model_name, managed_key=""),
        })

    def _wake_for(self, stack) -> tuple:
        """4.1 opt-in: wake the sleeping bound stack, poll to serving.

        One wake per stack per resolution (a chain that wakes twice would
        dispatch the same start twice). Never picks another model: whatever
        ``ai_slot_wake`` answers for THIS stack is what this row reports.
        """
        if self.woken.get(int(stack.id)):
            return ("stack_waking_timeout", {
                "message": (f"Stack '{stack.name}' (#{stack.id}) is still "
                            f"starting — try again once it is serving."),
                "progress": "Starting the AI model…"})
        self.woken[int(stack.id)] = self.woken.get(int(stack.id), 0) + 1
        # services.ai_slot_wake is denied by open-core/manifest.yaml, so the
        # CE carve strips it. Guarded: with no wake mechanism the row reports
        # the EXISTING retryable not-serving outcome (the same
        # `stack_waking_timeout` sentence the already-woken case returns), so
        # the slot says "not serving, retry" rather than crashing and rather
        # than pretending the stack is up. On the paid tree nothing changes.
        from utils.paid_import import optional_paid
        _wake = optional_paid("services.ai_slot_wake")
        if _wake is None:
            return ("stack_waking_timeout", {
                "message": (f"Stack '{stack.name}' (#{stack.id}) is still "
                            f"starting — try again once it is serving."),
                "progress": "Starting the AI model…"})
        answered = _wake.wake_for_slot(
            self.db, stack_id=int(stack.id), stack_name=stack.name,
            tenant_id=getattr(stack, "tenant_id", None), slot=self.slot,
            on_progress=self.on_progress)
        outcome = answered.get("outcome") or "stack_waking_timeout"
        extra = {"message": answered.get("message") or "",
                 "progress": answered.get("progress")}
        if outcome == "ready":
            extra["reached_running"] = True
        return (outcome, extra)

    def _try_external(self, row) -> tuple:
        """An external base_url: probe it (bounded), never read its key.

        ``key_ref`` is a Vault PATH and is reported as such — the secret is
        never read or returned here (``ai_config.py:1626``/``:1993`` already
        mask ``api_key`` as a bool on GET, and this module keeps it that way).
        ``LLM_API_KEY`` therefore stays empty: whoever dials this endpoint
        resolves the key from ``key_ref`` at the request boundary.
        """
        url = (row.base_url or "").strip()
        if not url:
            return ("no_binding", {
                "message": (f"Binding #{row.id} for the {self.slot} slot is "
                            f"external but names no base_url. " + REMEDY)})
        host = _host_of(url)
        if host and not self._probe(host):
            return ("unreachable", {
                "message": (f"Nothing answers at {_host_port_of(url) or host} — "
                            f"the external endpoint bound to the {self.slot} "
                            f"slot is not reachable from here. " + REMEDY)})
        return ("ready", {
            "target": {"stack_id": None, "stack_name": None,
                       "base_url": url, "model": row.model},
            "env": self._env_for(row, url, row.model, managed_key=""),
        })

    def _try_auto(self, row) -> tuple:
        """Q-AI5 = B: 'auto' was resolved ONCE at save. Re-running the
        existing managed-AI placement here answers "which stack MOD chose"
        (reported in ``target.stack_name``) and never invents one: if the
        placement serves nothing, that is the named failure.
        """
        placement = self._auto_placement()
        if not placement:
            return ("no_binding", {
                "message": (f"'Let MOD choose' for the {self.slot} slot "
                            f"resolves to no model on the active tier. "
                            + REMEDY)})
        base_url, model_name, stack_name = placement
        host = _host_of(base_url)
        if host and not self._probe(host):
            return ("unreachable", {
                "message": (f"MOD's choice for the {self.slot} slot answers "
                            f"at {_host_port_of(base_url) or host}, which nothing "
                            f"reaches from here. " + REMEDY)})
        return ("ready", {
            "target": {"stack_id": None, "stack_name": stack_name,
                       "base_url": base_url, "model": model_name},
            "env": self._env_for(row, base_url, model_name,
                                 managed_key=self._managed_key()),
        })

    def _auto_placement(self) -> Optional[tuple]:
        """(base_url, model_name, stack_name|None) from the managed-AI role.

        The placement is the existing one (:func:`routers.ai_config.resolve_role`
        over the active tier) — no second policy is invented here. The stack
        name is looked up so the page can say WHICH stack MOD picked.
        """
        from routers.ai_config import resolve_role
        raw: Dict[str, Any] = {}
        try:
            from routers.ai_config import _read_tool_config
            raw = _read_tool_config(self.slot) or {}
        except Exception:  # noqa: BLE001 — placement input only
            pass
        try:
            role = resolve_role(self.slot, raw)
        except Exception:  # noqa: BLE001
            return None
        if not role.get("resolved"):
            return None
        base_url, served = role.get("base_url"), role.get("served_name")
        if not base_url or not served:
            return None
        return (base_url, served, self._stack_serving(served))

    def _stack_serving(self, model_name: str) -> Optional[str]:
        """Name of the running stack that serves this model (reporting only).

        Guarded: services.llm_idle is denied by open-core/manifest.yaml, so on
        the CE carve there is nothing to name and this reports None — the
        placement still answers with its base_url/model, only the "which stack"
        label is absent. Reporting only: never a behaviour change on paid.
        """
        from utils.paid_import import optional_paid
        llm_idle = optional_paid("services.llm_idle")
        if llm_idle is None:
            return None
        for st in llm_idle._managed_llm_stacks(self.db):
            if model_name in llm_idle.served_models(st):
                return st.name
        return None

    def _managed_key(self) -> str:
        try:
            from routers.ai_config import _managed_key
            return _managed_key()
        except Exception:  # noqa: BLE001
            return ""

    def _classify_stack_error(self, row, err) -> tuple:
        """A StackProviderError from the shared resolver -> a typed outcome.

        The message already names the stack, so the cause is kept verbatim;
        the status word decides the outcome (the same fact, phrased for the
        slot's human).
        """
        text = str(err)
        low = text.lower()
        if "not found" in low:
            return ("stack_missing", {"message": text + " " + REMEDY})
        if "no reachable endpoint" in low:
            return ("unreachable", {
                "message": (f"Stack '{row.stack_name or '?'}' is running but "
                            f"has no reachable endpoint right now. "
                            + REMEDY)})
        if ("served model" in low or "serves multiple models" in low
                or "does not serve" in low):
            # Running, reachable, but the binding never named a usable model:
            # the slot is not bound to anything it can serve. The remedy is the
            # same one the message already gives (choose one), so no_binding +
            # the verbatim sentence is the honest answer inside this union.
            return ("no_binding", {"message": text + " " + REMEDY})
        for st in WAKING_STATUSES:
            if f"is {st}," in low:
                return ("waking", {
                    "message": text + " It will serve the slot once it is "
                              "serving; try again then.",
                    "progress": f"Starting the AI model: status={st}."})
        return ("stack_stopped", {"message": text + " " + REMEDY})

    # ------------------------------------------------------------------
    # Reachability: the BOUNDED probe only (BUG.B45)
    # ------------------------------------------------------------------
    def _probe(self, host: str) -> bool:
        """Never an unbounded getaddrinfo: reuse ai_config's bounded probe.

        A cold miss pays at most ``_HOST_RESOLVE_PROBE_TIMEOUT_S`` and answers
        optimistically while the real verdict caches itself for the next
        request — the exact shape that kept ``/ai-config/all`` at 0.05s warm
        after an API restart instead of 10.6s cold.
        """
        from routers.ai_config import _host_resolves_bounded
        if self.probe_timeout_s is None:
            return _host_resolves_bounded(host)
        return _host_resolves_bounded(host, self.probe_timeout_s)

    def _env_for(self, row, base_url: Optional[str],
                 model_name: Optional[str], managed_key: str = "") -> Dict[str, str]:
        """The LLM_* env a task container spreads — same shape as
        :func:`routers.ai_config._resolved_effective_env`'s dict, so every
        existing spread site keeps working; only ``ready`` ever carries it."""
        raw: Dict[str, Any] = {}
        try:
            from routers.ai_config import _read_tool_config
            raw = _read_tool_config(self.slot) or {}
        except Exception:  # noqa: BLE001 — knobs are optional
            pass
        try:
            from routers.ai_config import _model_is_reasoning
            reasoning = _model_is_reasoning(model_name or "")
        except Exception:  # noqa: BLE001
            reasoning = False
        provider = ("openai_compatible" if row.target_kind in ("stack", "auto")
                    else (raw.get("provider") or "openai_compatible"))
        env = {
            "LLM_PROVIDER": provider,
            "LLM_MODEL": model_name or "",
            "LLM_BASE_URL": base_url or "",
            # External targets: the key lives at `key_ref` (a Vault path) and
            # is resolved at the request boundary, never carried in a task env.
            "LLM_API_KEY": managed_key or "",
            "LLM_TEMPERATURE": raw.get("temperature", "0.3"),
            "LLM_MAX_TOKENS": raw.get("max_tokens", "4096"),
            "LLM_REASONING": "1" if reasoning else "",
        }
        return {k: v for k, v in env.items() if v}

    def _finish(self, row, outcome: str, extra: Dict[str, Any],
                attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
        target = dict(extra.get("target") or {})
        if row is not None:
            target.setdefault("stack_id", row.stack_id)
            target.setdefault("stack_name", row.stack_name)
            target.setdefault("base_url", row.base_url)
            target.setdefault("model", row.model)
            target["slot"] = self.slot
            target["capability"] = self.capability
            target["source"] = row.scope
            target["priority"] = int(row.priority or 0)
            # A Vault PATH, never a secret (design D1/D7).
            target["key_ref"] = row.key_ref
        message = extra.get("message") or ""
        # 4.1: a wake that is merely still starting is retryable — telling the
        # operator to go pick a different model there would contradict Q-AI1 = B
        # (the binding is the truth), so the remedy stays off these two.
        no_remedy = outcome in ("stack_waking_timeout", "stack_wake_held")
        if outcome != "ready" and not no_remedy and message and REMEDY not in message:
            message = f"{message} {REMEDY}"
        return {"outcome": outcome, "message": message,
                "attempts": attempts, "target": target,
                "env": (extra.get("env") or {}) if outcome == "ready" else {},
                "progress": extra.get("progress")}


def _host_of(url: str) -> Optional[str]:
    """The host a URL dials, or None when it names none (never a guess)."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname
    except Exception:  # noqa: BLE001
        return None
    return host or None


def _host_port_of(url: str) -> Optional[str]:
    """``host[:port]`` for the human sentence (the contract says host:port).

    The probe still dials the bare host — ``getaddrinfo`` wants no port — but
    the sentence an operator acts on names the exact endpoint that went dark.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    return f"{host}:{parsed.port}" if parsed.port else host
