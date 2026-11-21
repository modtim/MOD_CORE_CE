"""AI Config service layer — managed-AI fleet placement + tier-ladder policy.

Gate 2b (per-file service/CRUD migration) for `routers/ai_config.py`.

LAYERING (top calls down):

    routers/ai_config.py         HTTP only: auth check, thin call, response
        -> HERE (services/ai_config_service.py)   policy / orchestration
            -> crud/ai_config.py                   the ONLY db.query for
                                                    this domain
                -> models.py

Every function here was extracted verbatim from `routers/ai_config.py`,
where it lived as a private module-level helper with `db.query(...)` calls
inline — the audit's core complaint (routers bypassing the service/CRUD
layer). Behaviour is unchanged; the query lines now go through
`crud.ai_config`.

`routers/ai_config.py` still owns every Pydantic response schema, every
`LOCAL_PRESETS`/`MODEL_CATALOG`/`PRESET_LADDER`/`TOOLS` table, and a handful
of small pure helpers (`tier_capabilities`, `_default_accelerator_kind`,
`_serving_matches`, `_served_model_ref`, `_weight_total_bytes`,
`_role_base_url`, vault read/write, …) that dozens of OTHER modules import
directly by name (`node_builder/`, `utils/llm_client.py`,
`services/managed_ai_stacks.py`, `routers/persistent_stacks/`, plus ~30 test
files). Those are intentionally left in place rather than relocated here —
moving a name that many call sites bind to by `from routers.ai_config import
X` would multiply this migration's blast radius far past this one file for
no functional gain. This module reaches back into `routers.ai_config` for
those names with LAZY (function-local) imports, exactly the convention
`routers/ai_config.py` already uses everywhere for the same reason
(`routers.ai_config` imports THIS module at load time, so a module-level
import back here would be circular).

`routers/ai_config.py` re-exports every function below under its original
name (`from services.ai_config_service import fleet_accelerator_kind`, …) so
every existing importer — direct unit-test calls
(`from routers.ai_config import fleet_accelerator_kind`), and
`routers/persistent_stacks/_shared.py`'s own lazy import of
`fleet_accelerator_kind` — keeps working unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fleet introspection
# ---------------------------------------------------------------------------

def fleet_accelerator_kind(db) -> Optional[str]:
    """The accelerator kind this deployment's ladder figures should describe.

    multi-vendor §1b.2/§1b.4. Demand forks per vendor — `balanced` needs 45 GB
    on CUDA and 42 on ROCm, because a different engine serves it — so reporting
    the CUDA number to an AMD operator is telling them to buy hardware they do
    not need.

    Mixed fleet: pick the kind with the most total VRAM. That is the hardware
    most likely to host the managed stacks, and reporting SOME fleet's real
    figure beats reporting a vendor nobody owns. The chosen kind is returned so
    the caller can say which hardware the numbers describe rather than leaving
    the operator to guess.

    None when the fleet reports no accelerator at all, which keeps the CUDA
    defaults every existing caller already gets.
    """
    try:
        from crud.ai_config import accelerator_vram_by_kind
        rows = accelerator_vram_by_kind(db)
    except Exception:  # noqa: BLE001
        logger.debug("fleet accelerator kind unavailable", exc_info=True)
        return None
    usable = [(k, float(v or 0)) for k, v in rows if k]
    if not usable:
        return None
    return max(usable, key=lambda kv: kv[1])[0].strip().lower()


def managed_ai_wanted_models(db) -> Dict[str, str]:
    """{repo: full model ref} the managed-AI stacks are configured to serve.

    Read off the stack specs rather than inferred from a preset's display name.
    `LOCAL_PRESETS[...]["served_name"]` is a LABEL — `minimal` carries the
    Ollama-style "granite3.3:2b" while the stack serves
    "ibm-granite/granite-3.3-2b-instruct-GGUF:Q4_K_M" — and substring-matching
    one against the other only ever worked for the rungs whose label happened to
    be a literal substring. The spec is the authority on what is being served.
    """
    wanted: Dict[str, str] = {}
    try:
        from services import managed_ai_stacks as _mas
        from crud.ai_config import list_stacks_by_names
        for st in list_stacks_by_names(db, _mas.ALL_STACK_NAMES):
            for svc in ((st.compose_spec or {}).get("services") or {}).values():
                ref = ((svc.get("x-mod") or {}).get("served") or {}).get("model")
                if ref:
                    wanted[ref.split(":", 1)[0]] = ref
    except Exception:  # noqa: BLE001
        logger.debug("managed-AI stack scan failed", exc_info=True)
    return wanted


def managed_ai_download_progress(db) -> List[Dict[str, Any]]:
    """In-flight weight downloads across the fleet, with a denominator.

    The worker reports bytes on disk; the total is only knowable from
    HuggingFace, so the two halves are joined here. Returns [] when nothing is
    downloading — which is what lets the UI take a progress bar away again.
    """
    from routers.ai_config import _weight_total_bytes

    # What each managed-AI stack is trying to serve, so a downloading repo can
    # be matched to the rung that asked for it.
    wanted = managed_ai_wanted_models(db)

    out: List[Dict[str, Any]] = []
    try:
        from crud.ai_config import list_licensed_workers
        workers = list_licensed_workers(db)
    except Exception:  # noqa: BLE001
        return out

    seen_repos = set()
    cached_repos = set()
    for w in workers:
        cached_repos.update((w.hardware_specs or {}).get("cached_models") or [])
        for dl in ((w.hardware_specs or {}).get("model_downloads") or []):
            repo = dl.get("repo")
            done = dl.get("bytes")
            if not repo or not isinstance(done, int):
                continue
            # Only report downloads for models a managed-AI stack actually
            # wants. A tenant pulling their own weights into the same cache is
            # not this page's business.
            ref = wanted.get(repo)
            if not ref:
                continue
            total = _weight_total_bytes(ref)
            seen_repos.add(repo)
            out.append({
                "repo": repo,
                "model": ref,
                "worker": w.worker_name,
                "state": "downloading",
                "downloaded_bytes": done,
                "total_bytes": total,
                "percent": (round(done / total * 100, 1)
                            if total and total > 0 else None),
            })

    # A rung the fleet is switching TO whose weights are not here yet, and whose
    # download has not begun. It takes roughly half a minute to get from Apply
    # to the first byte — the container is recreated, the server starts, the
    # repo is resolved — and reporting nothing across that gap leaves a blank
    # panel at the exact moment someone is deciding whether the button worked.
    # Announced as "pending" rather than faked as 0% of a download that is not
    # running yet, so the UI can say "starting" and mean it.
    for repo, ref in wanted.items():
        if repo in seen_repos or repo in cached_repos:
            continue
        out.append({
            "repo": repo,
            "model": ref,
            "worker": None,
            "state": "pending",
            "downloaded_bytes": 0,
            "total_bytes": _weight_total_bytes(ref),
            "percent": 0,
        })
    return out


# ---------------------------------------------------------------------------
# Tier ladder + placement
# ---------------------------------------------------------------------------

def _tier_with_placement(db, preset: str, kind: Optional[str] = None) -> Dict[str, Any]:
    """A rung, plus whether this fleet can actually host it.

    per-rung-reasoner §5.3/§5.4/§5.5 — the ladder previously described what each
    rung WOULD serve without ever saying whether the operator's hardware could
    run it. The cost of learning that after selection is a stack that
    provisions, waits, and never runs, with the reason buried in placement
    arithmetic nobody reads.

    The verdict comes from `_managed_ai_placement`, the SAME mechanism that
    already reports why a managed-AI stack could not be placed. A second
    calculation here would eventually disagree with it, and the disagreement
    surfaces as a rung that reads selectable and then fails — which is the exact
    failure being fixed, wearing different clothes.

    Best-effort: this enriches a display. A fleet-capacity hiccup must not take
    out the ladder the operator needs in order to choose anything at all.
    """
    from routers.ai_config import tier_capabilities, _default_accelerator_kind

    tier = tier_capabilities(preset, kind)
    try:
        from routers.persistent_stacks import (_fleet_worker_devices,
                                               _managed_ai_placement)
        from utils.paid_import import optional_paid

        # managed-ai-self-deploy is a Paid feature: `services/managed_ai_stacks.py`
        # is denied by open-core/manifest.yaml, so the CE carve strips it and this
        # module is absent at call time. Guarded (optional_paid) rather than raw:
        # the placement verdict is a DISPLAY enrichment (see the docstring above),
        # so on CE it DEGRADES to `placement: None` — the exact shape the existing
        # `except Exception` path below already ships — rather than ImportError-500
        # taking the whole ladder out. On the paid tree `mas` is the module itself
        # and every path below is byte-for-byte the existing one.
        mas = optional_paid("services.managed_ai_stacks")
        if mas is None:
            tier["placement"] = None  # CE: no managed stacks to place
            return tier

        cards_required = mas.cards_required
        stack_roles = mas.stack_roles
        choose_role_kinds = mas.choose_role_kinds
        normalize_kind = mas.normalize_kind
        role_floors = mas.role_floors

        # §5.9 — a rung must not be measured against capacity IT is holding.
        # Once `compact` was deployed it reported itself unplaceable, because
        # its own two stacks had taken 14.5 of the card's 16 GB and the largest
        # free device read 1.5. "This tier will not fit" is a bad way to say
        # "this tier is currently running".
        GENERATOR_STACK_NAME = mas.GENERATOR_STACK_NAME
        REASONER_STACK_NAME = mas.REASONER_STACK_NAME
        from crud.ai_config import list_stack_ids_by_names
        own = list_stack_ids_by_names(db, [GENERATOR_STACK_NAME, REASONER_STACK_NAME])

        # THE KIND COMES FROM THE FLEET, and it is chosen PER ROLE.
        #
        # This line used to read
        #     kind = normalize_kind(os.getenv(...) or DEFAULT_ACCELERATOR_KIND)
        # which threw away the `kind` argument the caller had just worked out
        # from the fleet. The ladder therefore announced `accelerator_kind:
        # rocm` in its header and then priced every rung in CUDA — quoting
        # "needs 45 GB of cuda" for a rung that costs 42 on this hardware, and
        # testing it against CUDA devices only. On a fleet whose sole CUDA card
        # is the manager's laptop GPU, that reads exactly like "the UI only
        # looks at the manager": the 32 GB AMD card and the 24 GB Arc were
        # invisible to the check because nothing was asking about their kind.
        #
        # §1.4 — when the fleet reports no kind at all, the fallback now goes
        # through the resolver (stored setting -> env -> hardcoded default)
        # rather than reading MANAGED_AI_ACCELERATOR_KIND directly, so an
        # operator's stored accelerator preference applies even before any
        # hardware has been detected.
        base_kind = normalize_kind(kind or _default_accelerator_kind())
        role_kinds = choose_role_kinds(
            preset, _fleet_worker_devices(db, exclude_stack_ids=own),
            default_kind=base_kind,
        ) or {"generator": base_kind, "reasoner": base_kind}

        # Floors fork by vendor, so each role is priced on the card it will
        # actually land on: {role: (kind, floor)}.
        priced = {}
        role_rows = []
        for r in stack_roles(preset):
            rk = role_kinds.get(r["role"], base_kind)
            # Re-read the row on the role's OWN kind: the engine, and with it
            # the served model's floor, forks by vendor.
            own_row = next((x for x in stack_roles(preset, rk)
                            if x["role"] == r["role"]), r)
            floor = float(own_row.get("vram_floor_gb") or 0.0)
            role_rows.append({"role": r["role"],
                              "model": own_row.get("served_name"),
                              "vram_floor_gb": floor,
                              "serving_stack": own_row.get("serving_stack"),
                              "accelerator_kind": rk})
            if floor > 0:
                priced[r["role"]] = (rk, floor)

        if not priced:
            tier["placement"] = {"placeable": True, "shares_one_card": False,
                                 "per_role": {}, "fits_instead": None,
                                 "reason": None}
            return tier

        mixed = len({k for k, _f in priced.values()}) > 1
        # A mixed pair is on two different cards by definition, so it cannot
        # also be co-tenanting one.
        shares = cards_required(preset) == 1 and len(priced) > 1 and not mixed

        # Demand shape is {kind: [descending per-device minimums]} — the same
        # thing `_aggregate_accelerator_demand` produces, so the fit test here
        # is bit-for-bit the one placement will apply.
        if shares:
            # One device must hold both models. Asked as a SINGLE demand of the
            # combined size, not two demands: two entries would be read as
            # needing two devices by the domination rule, when the whole point
            # of this rung is that the pair co-tenants one card.
            one_kind = next(iter(priced.values()))[0]
            combined = round(sum(f for _k, f in priced.values()), 2)
            verdicts = [("combined", _managed_ai_placement(
                db, {one_kind: [combined]}, preset, exclude_stack_ids=own))]
        else:
            verdicts = [
                (role, _managed_ai_placement(
                    db, {rk: [floor]}, preset, exclude_stack_ids=own))
                for role, (rk, floor) in priced.items()
            ]

        blocking = [(n, v) for n, v in verdicts if not v.get("placeable")]

        # Two failures with opposite fixes: hardware that can NEVER host this
        # rung, versus hardware that could but is busy. Reported as one
        # "unplaceable" they send an operator to buy a card they already own.
        capacity_now = None
        if blocking:
            biggest_total = 0.0
            try:
                from utils.accelerator_fit import device_map
                from crud.ai_config import list_licensed_workers
                for w in list_licensed_workers(db):
                    for sizes in (device_map(db, w) or {}).values():
                        biggest_total = max([biggest_total] + [float(s) for s in sizes])
            except Exception:  # noqa: BLE001
                biggest_total = 0.0
            need = max(float(v.get("required_gb") or 0) for _n, v in blocking)
            capacity_now = biggest_total >= need > 0
        tier["placement"] = {
            "placeable": not blocking,
            "shares_one_card": shares,
            "per_role": {n: v for n, v in verdicts},
            # §5.5 — when this rung will not fit, name one that would. Silence
            # here invites the conclusion that managed AI does not work on this
            # hardware, when the honest answer is usually "not at this rung".
            "fits_instead": next(
                (v.get("fits_instead") for _, v in blocking if v.get("fits_instead")),
                None),
            "reason": "; ".join(
                f"{n}: {v.get('reason')}" for n, v in blocking if v.get("reason")
            ) or None,
            # True  -> the hardware is big enough, it is just occupied now.
            # False -> no card here is large enough, ever.
            "capacity_is_temporary": capacity_now,
            # §5.8 — what this rung actually provisions, so the active tier can
            # be shown as stacks rather than as a single verdict. Each role
            # carries the kind it will land on, so a mixed pair can be rendered
            # as "generator on AMD, reasoner on Intel" rather than the UI
            # having to assume one vendor for both.
            "roles": role_rows,
            "accelerator_kinds": dict(role_kinds),
            "mixed_vendor": mixed,
        }
    except Exception:  # noqa: BLE001
        logger.debug("tier placement unavailable for %s", preset, exc_info=True)
        tier["placement"] = None
    return tier


def _annotate_newly_available(ladder: List[Dict[str, Any]]) -> None:
    """§7.4 — mark a rung placeable now but not the last time the ladder was
    viewed, so new hardware surfaces as a newly-available option without being
    applied. A display hint only: it never changes the selection, and it clears
    once seen.

    The first-ever view establishes the baseline — everything currently
    placeable counts as already seen — so the initial fleet does not light up
    every rung as new. A rung that leaves the placeable set (a worker went
    away) updates the baseline too, so it can legitimately light up again if it
    returns.
    """
    from routers.ai_config import _read_vault_path, _write_vault_path, _SEEN_PLACEABLE_PATH

    current = {r.get("preset") for r in ladder
               if (r.get("placement") or {}).get("placeable")}
    try:
        stored = _read_vault_path(_SEEN_PLACEABLE_PATH) or {}
    except Exception:  # noqa: BLE001 — a hint must never take out the ladder
        stored = {}
    has_baseline = "presets" in stored
    seen = set(stored.get("presets") or [])

    for r in ladder:
        placeable = bool((r.get("placement") or {}).get("placeable"))
        r["newly_available"] = bool(has_baseline and placeable
                                    and r.get("preset") not in seen)

    if not has_baseline or current != seen:
        try:
            _write_vault_path(_SEEN_PLACEABLE_PATH, {"presets": sorted(p for p in current if p)})
        except Exception:  # noqa: BLE001
            logger.debug("could not persist newly-available baseline", exc_info=True)


def build_tier_ladder(db: Session) -> Dict[str, Any]:
    """The full preset ladder, with what each rung can actually serve.

    The policy behind `GET /platform/ai-config/tiers` — see that handler in
    `routers/ai_config.py` for the HTTP-facing docstring (auth, response
    shape). Split out here so the handler is auth-check + shape only.
    """
    from routers.ai_config import (resolve_managed_ai_setting, PRESET_LADDER,
                                   WIZARD_MIN_CONTEXT_TOKENS, TOOLS)

    _fleet_kind = fleet_accelerator_kind(db)
    _setting = resolve_managed_ai_setting()
    _ladder = [_tier_with_placement(db, p, _fleet_kind) for p in PRESET_LADDER]
    _annotate_newly_available(_ladder)
    return {
        "default": _setting.tier,
        # §1.2 — which source decided `default`, so a settings surface can say
        # "you chose this" versus "nobody has touched this yet".
        "default_source": _setting.tier_source,
        "wizard_min_context_tokens": WIZARD_MIN_CONTEXT_TOKENS,
        "tools": list(TOOLS),
        # multi-vendor §1b.2 — the ladder describes THIS fleet's hardware, not
        # NVIDIA's. `accelerator_kind` is echoed so the UI can say which
        # hardware the figures are for instead of the operator assuming.
        "accelerator_kind": _fleet_kind,
        "ladder": _ladder,
    }


# ---------------------------------------------------------------------------
# managed-ai-self-deploy §3 — the plan, computed by the code that will
# execute it.
# ---------------------------------------------------------------------------

def compute_managed_ai_plan(db: Session, target: str,
                            kind: Optional[str] = None):
    """§3.1/§3.2/§3.3 — diff declared stacks for `target` against what is
    actually deployed, and return the actions applying it would take.

    There are exactly two possible managed-AI stacks
    (`services.managed_ai_stacks.ALL_STACK_NAMES`: one generator, one
    reasoner) — the tier changes what each SERVES, never their names — so the
    diff is by name, then by compose-spec equality for "already correct".

    This reads from the same authorities `apply_managed_ai_core`
    (routers/persistent_stacks.py) executes against — `mas.definitions()`,
    `mas.choose_role_kinds()`, the `PersistentStack` rows named in
    `ALL_STACK_NAMES` — rather than a parallel notion of what "declared" or
    "deployed" means, which is what keeps a preview from being able to
    disagree with what Apply then does. It does not call the generic
    `stack_reconciler.plan_for_gap()` split apply's sibling features use:
    that function reconciles arbitrary stacks found by `find_gaps()`, and
    managed-AI's two fixed, by-name stacks are a narrower question with a
    cheaper direct answer — the invariant `plan_for_gap()` protects is upheld
    here by reading the same source functions, not by sharing its call.
    """
    from fastapi import HTTPException
    from routers.ai_config import (LOCAL_PRESETS, _served_model_ref, _weight_total_bytes,
                                   _default_accelerator_kind, ManagedAIPlanAction,
                                   ManagedAIDownloadEstimate, ManagedAIPlan)
    from crud.ai_config import list_stacks_by_names
    from utils.paid_import import optional_paid

    # managed-ai-self-deploy is a Paid feature: `services.managed_ai_stacks` is
    # denied by open-core/manifest.yaml, so the CE carve strips it and this
    # module is absent at call time. Guarded (optional_paid) rather than raw:
    # the plan surface must DEGRADE (no managed stacks in the ladder/plan,
    # nothing to apply) rather than ImportError-500, and must never pretend a
    # stack is serving. On the paid tree `mas` is the module itself and every
    # path below is byte-for-byte the existing one.
    mas = optional_paid("services.managed_ai_stacks")

    if target not in LOCAL_PRESETS:
        raise HTTPException(status_code=422, detail=f"unknown tier {target!r}")

    if mas is None:
        # Same ManagedAIPlan shape, empty managed fields: no actions, nothing
        # already correct, no bytes, no placement, stack path off.
        return ManagedAIPlan(
            tier=target,
            accelerator_kind=kind or fleet_accelerator_kind(db) or _default_accelerator_kind(),
            actions=[],
            already_correct=False,
            download_estimate=ManagedAIDownloadEstimate(
                upper_bound_bytes=0, unknown=False,
                note=("Managed AI stacks are not part of this edition — this "
                      "plan has no managed-AI actions.")),
            placement=None,
            stack_path_enabled=False,
            message="Managed AI stacks are not available in this edition.",
        )

    stack_path_enabled = mas.enabled()

    existing_rows = list_stacks_by_names(db, mas.ALL_STACK_NAMES)
    existing_by_name = {s.name: s for s in existing_rows}
    existing_ids = [s.id for s in existing_rows]

    # Same kind-resolution chain apply_managed_ai_core uses (fleet auto-detect
    # first — a rung's demand forks by vendor), falling through the §1
    # resolver rather than a raw env read when the fleet reports nothing.
    resolved_kind = kind or fleet_accelerator_kind(db) or _default_accelerator_kind()

    role_kinds = {"generator": resolved_kind, "reasoner": resolved_kind}
    try:
        from routers.persistent_stacks import _fleet_worker_devices
        role_kinds = mas.choose_role_kinds(
            preset=target,
            worker_devices=_fleet_worker_devices(db, exclude_stack_ids=existing_ids),
            default_kind=resolved_kind,
        ) or role_kinds
    except Exception:  # noqa: BLE001
        logger.debug("plan: role-kind selection unavailable for %s", target, exc_info=True)

    try:
        declared = mas.definitions(target, role_kinds["generator"],
                                   reasoner_kind=role_kinds["reasoner"])
    except Exception as e:  # noqa: BLE001
        raise internal_http_error(422, "Cannot plan stack preset", e) from e
    declared_by_name = {d["name"]: d for d in declared}

    role_of = {mas.GENERATOR_STACK_NAME: "generator",
              mas.REASONER_STACK_NAME: "reasoner"}

    actions: List[Any] = []
    download_bytes = 0
    download_unknown = False

    for name, decl in declared_by_name.items():
        target_model = _served_model_ref(decl.get("compose_spec"))
        existing = existing_by_name.get(name)
        role = role_of.get(name, name)
        if existing is None:
            actions.append(ManagedAIPlanAction(
                stack=name, role=role, action="deploy",
                reason=f"the {target} tier serves a {role} this deployment does not have yet",
                current_model=None, target_model=target_model))
            need_download = True
        else:
            current_model = _served_model_ref(existing.compose_spec)
            same_spec = (existing.compose_spec or {}) == (decl.get("compose_spec") or {})
            if same_spec:
                actions.append(ManagedAIPlanAction(
                    stack=name, role=role, action="leave",
                    reason=f"already deployed at the {target} tier",
                    current_model=current_model, target_model=target_model))
                need_download = False
            else:
                reason = (f"model change within {target}: {current_model or '?'} -> {target_model or '?'}"
                          if current_model != target_model
                          else f"the {target} tier's {role} configuration has changed")
                actions.append(ManagedAIPlanAction(
                    stack=name, role=role, action="deploy", reason=reason,
                    current_model=current_model, target_model=target_model))
                need_download = True
        if need_download:
            total = _weight_total_bytes(target_model) if target_model else None
            if total is None:
                download_unknown = True
            else:
                download_bytes += total

    for name, existing in existing_by_name.items():
        if name in declared_by_name:
            continue
        role = role_of.get(name, name)
        actions.append(ManagedAIPlanAction(
            stack=name, role=role, action="stop",
            reason=f"the {target} tier does not serve a {role}",
            current_model=_served_model_ref(existing.compose_spec), target_model=None))

    already_correct = bool(actions) and all(a.action == "leave" for a in actions)

    placement = None
    try:
        placement = _tier_with_placement(db, target, resolved_kind).get("placement")
    except Exception:  # noqa: BLE001
        logger.debug("plan: placement unavailable for %s", target, exc_info=True)

    note = ("Upper bound only — the API cannot see what a worker's local "
            "weight cache already holds, so a stack marked to deploy may in "
            "fact start in seconds if the weights are already there.")
    if download_unknown:
        note += (" At least one model's size could not be looked up, so the "
                 "real figure may be larger than shown.")

    if not stack_path_enabled:
        message = (f"{mas.FLAG_ENV} is not set — managed AI runs as a compose "
                   f"service on this deployment, and this plan cannot be applied "
                   f"through the API. Set {mas.FLAG_ENV}=true to enable stack-based apply.")
    elif already_correct:
        message = f"Already at {target} — applying would change nothing."
    else:
        n_deploy = sum(1 for a in actions if a.action == "deploy")
        n_stop = sum(1 for a in actions if a.action == "stop")
        parts = []
        if n_deploy:
            parts.append(f"{n_deploy} stack(s) would deploy")
        if n_stop:
            parts.append(f"{n_stop} stack(s) would stop")
        message = ("; ".join(parts) or "No managed-AI stacks are declared at this tier.") + "."

    return ManagedAIPlan(
        tier=target, accelerator_kind=resolved_kind, actions=actions,
        already_correct=already_correct,
        download_estimate=ManagedAIDownloadEstimate(
            upper_bound_bytes=download_bytes, unknown=download_unknown, note=note),
        placement=placement, stack_path_enabled=stack_path_enabled, message=message,
    )


# ---------------------------------------------------------------------------
# Handler bodies — moved out of the @router-decorated functions so those
# stay thin (auth check + call + return). See the matching handler in
# routers/ai_config.py for the HTTP-facing docstring.
# ---------------------------------------------------------------------------

def build_managed_llm_health(db: Session):
    """Ping the compose-level vLLM server's `/v1/models` endpoint.

    Policy for `GET /platform/llm-health` — see that handler for the
    HTTP-facing docstring.
    """
    from routers.ai_config import (_role_base_url, _read_vault_path, _MANAGED_STATE_PATH,
                                   LOCAL_PRESETS, _serving_matches, ManagedLLMHealthResponse)

    # Resolved, not hardcoded — see the note on the /health probe in main.py.
    # The alias is right only while the model is on this network, and a probe
    # that always fails made a working deployment read as a stalled download.
    base_url = _role_base_url("generator")

    # Same guard as compute_managed_ai_plan: services.managed_ai_stacks is
    # denied by open-core/manifest.yaml, so on the CE carve this module is
    # absent at call time. Absent means "managed AI does not run as stacks
    # here" (as_stacks False): the health surface still serves the compose
    # probe, with no stack-served catalogue enrichment and no managed
    # downloads. On the paid tree this is the identical existing path.
    from utils.paid_import import optional_paid
    _mas = optional_paid("services.managed_ai_stacks")
    as_stacks = _mas.enabled() if _mas is not None else False
    _llm_keys = optional_paid("services.llm_api_keys")
    # Gathered before the probe: the probe FAILING is exactly when a download
    # is most likely to be the reason, so it must be present on that path too.
    downloads = managed_ai_download_progress(db) if as_stacks else []

    managed_state = _read_vault_path(_MANAGED_STATE_PATH) or {}
    saved_preset = managed_state.get("preset")
    saved_served_name = (
        LOCAL_PRESETS.get(saved_preset, {}).get("served_name") if saved_preset else None
    )

    try:
        import httpx
        # FX.25 p1a: send the managed-LLM bearer token when one has been
        # provisioned — a no-op `{}` today (nothing turns the server's
        # `--api-key` flag on yet, that's p1b), so this is zero behaviour
        # change until then.
        # services.llm_api_keys is denied by open-core/manifest.yaml (CE has no
        # managed-LLM key provisioning), so it is absent on the CE carve: no
        # key means no auth header, never an import error in the health payload.
        auth_headers = (
            _llm_keys.llm_auth_headers("api_key") if _llm_keys is not None else {}
        )
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{base_url}/v1/models", headers=auth_headers)
        if resp.status_code != 200:
            return ManagedLLMHealthResponse(
                healthy=False, base_url=base_url, served_models=[],
                saved_preset=saved_preset, running_matches_saved=True,
                as_stacks=as_stacks, downloads=downloads,
                error=f"vLLM returned HTTP {resp.status_code}",
            )
        data = resp.json()
        served_models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        if as_stacks:
            # On the stack path the SPEC says what should be serving, so compare
            # against that rather than against a preset's display label. The
            # label is not the model id — `minimal` reads "granite3.3:2b" while
            # the stack serves "ibm-granite/granite-3.3-2b-instruct-GGUF:Q4_K_M"
            # — and matching one to the other only ever worked for rungs whose
            # label happened to be a literal substring. It did not for minimal,
            # which reported a redeploy pending forever while serving exactly
            # what was asked for, and no amount of pressing Apply could clear it.
            expected = set(managed_ai_wanted_models(db).values())
            matches = (not expected) or any(
                m in expected or any(e.split(":", 1)[0] in m for e in expected)
                for m in served_models)
        else:
            matches = _serving_matches(saved_served_name, served_models)
        return ManagedLLMHealthResponse(
            healthy=True, base_url=base_url, served_models=served_models,
            saved_preset=saved_preset, running_matches_saved=matches,
            as_stacks=as_stacks, downloads=downloads,
        )
    except Exception as e:
        return ManagedLLMHealthResponse(
            healthy=False, base_url=base_url, served_models=[],
            saved_preset=saved_preset, running_matches_saved=True,
            as_stacks=as_stacks, downloads=downloads,
            error=(str(e)[:200] if _llm_keys is not None
                   else "LLM endpoint not reachable"),
        )


def _model_to_preset(served_name: str) -> str | None:
    """Return the lowest rung preset that serves *served_name*, or None.

    Checks both the preset's top-level generator and its reasoner sub-dict.
    Returns the first (lowest) match in PRESET_LADDER order.
    """
    from routers.ai_config import LOCAL_PRESETS, PRESET_LADDER
    for preset_name in PRESET_LADDER:
        preset = LOCAL_PRESETS.get(preset_name)
        if not preset:
            continue
        # Check top-level generator first
        if preset.get("served_name") == served_name:
            return preset_name
        # Check reasoner sub-dict
        reasoner = preset.get("reasoner")
        if isinstance(reasoner, dict) and reasoner.get("served_name") == served_name:
            return preset_name
    return None


def build_model_catalog(db: Session) -> Dict[str, Any]:
    """List the local models a tool can be routed at.

    Policy for `GET /platform/ai-config/model-catalog` — see that handler
    for the HTTP-facing docstring.
    """
    from routers.ai_config import (MODEL_CATALOG, model_availability,
                                   resolve_default_model)

    out = []
    listed = set()
    for key, e in MODEL_CATALOG.items():
        v = model_availability(key, db=db)
        out.append({
            "key": key,
            "label": e["label"],
            "served_name": e["served_name"],
            "vendor": e["vendor"],
            "role_hint": e.get("role_hint", ""),
            "vram_est_gb": e.get("vram_est_gb"),
            # The CARD needed, which is not the weight footprint.
            "vram_floor_gb": e.get("vram_floor_gb"),
            # §1.1/§1.3 — computed across the fleet, with the reason when it is
            # not servable. `None` means capacity could not be determined, which
            # is distinct from a definite no.
            "available": v["available"],
            "unavailable_reason": v.get("reason"),
            "unavailable_cause": v.get("cause"),
            "largest_available_gb": v.get("largest_available_gb"),
            "shortfall_gb": v.get("shortfall_gb"),
            # Server-side: which LOCAL_PRESETS rung serves this model.
            # `None` = no rung uses this model (unmapped / future).
            "preset": _model_to_preset(e["served_name"]),
        })
        listed.add(key)

    # mesh-native-llm-routing §4.2/§4.3 — the catalogue is descriptive, not an
    # allow-list: a model a running stack serves must appear here even when no
    # catalog entry exists for it, with the served name as its label. The
    # availability verdict already prefers the serving stack over the fleet
    # estimate when the two disagree.
    try:
        import models
        # Guarded per the CE-carve idiom: services.llm_idle is denied by
        # open-core/manifest.yaml, so on the CE carve the stack-served
        # catalogue enrichment is simply skipped (the catalog keeps every
        # catalogued model; it just gains no "served by a running stack"
        # extras). On the paid tree `llm_idle` is the module itself and the
        # loop below is the identical existing path.
        from utils.paid_import import optional_paid
        llm_idle = optional_paid("services.llm_idle")
        for st in ((db.query(models.PersistentStack)
                    .filter(models.PersistentStack.status == "running")
                    .all()) if llm_idle is not None else []):
            for name in llm_idle.served_models(st):
                if name in listed:
                    continue
                listed.add(name)
                v = model_availability(name, db=db)
                out.append({
                    "key": name,
                    "label": name,
                    "served_name": name,
                    "vendor": "served by stack %s" % st.name,
                    "role_hint": "served by a running stack",
                    "vram_est_gb": None,
                    "vram_floor_gb": None,
                    "available": v["available"],
                    "unavailable_reason": v.get("reason"),
                    "unavailable_cause": v.get("cause"),
                    "largest_available_gb": v.get("largest_available_gb"),
                    "shortfall_gb": v.get("shortfall_gb"),
                    "preset": _model_to_preset(name),
                })
    except Exception:  # noqa: BLE001 — the stack scan must not break the catalog
        logger.debug("uncatalogued served-model scan failed", exc_info=True)
    return {
        "default": resolve_default_model(None, db=db),
        "models": out,
        # Stated plainly so the surface does not have to infer it from an
        # all-unavailable list.
        "any_servable": any(m["available"] is True for m in out),
    }
