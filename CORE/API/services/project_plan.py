"""project-agent §5 — the plan, and why a task without a test is not one.

"A task isn't done until it's been tested" is enforced here as a SCHEMA
rule rather than as a prompt instruction. A plan whose task states no
acceptance check is rejected and names the task, the same way
`openspec validate` rejects a change for lacking a scenario. That is
design D3, and the reasoning is the same as §4.2's: prompt instructions
degrade under load, across model changes, and at the end of long
contexts, while a validator does not.

A human review step IS a valid acceptance check (§5.2). The agent must be
able to state a check for work whose output it cannot predict, and where
it genuinely cannot, "a person looks at it" is the honest answer rather
than an escape hatch. What is refused is stating nothing.

Shape:

    {
      "version": 3,
      "tasks": [
        {"key": "extract",
         "goal": "one JSON of violations per photo",
         "acceptance": {"kind": "output_matches", "detail": "..."},
         "depends_on": []}
      ]
    }
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# §5.2 — what counts as stating a check.
#
#   output_matches  compared against the project's supplied expected output
#   type_check      shape/format only; the honest middle-of-pipeline check
#                   per design D4, where no semantic reference exists
#   human_review    a person looks at it. A real check, not an escape hatch
#   existing_node   satisfied by an artifact that already exists and is
#                   already verified
ACCEPTANCE_KINDS = (
    "output_matches",
    "type_check",
    "human_review",
    "existing_node",
)


class PlanInvalid(ValueError):
    """A plan that cannot be dispatched, with the offending task named."""


class ProjectNotReady(ValueError):
    """Building was requested before the project stated what finished
    looks like."""


def _as_tasks(plan: Any) -> List[Dict[str, Any]]:
    if not isinstance(plan, dict):
        raise PlanInvalid("plan must be an object")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise PlanInvalid("plan must contain a non-empty 'tasks' list")
    return tasks


def _check_stated(acceptance: Any) -> bool:
    """Whether an acceptance block actually says anything.

    A present-but-empty block is the failure mode worth catching: a model
    asked for a required field will happily emit ``{}`` or ``""``, which
    satisfies "the key exists" and states no check at all.
    """
    if isinstance(acceptance, str):
        return bool(acceptance.strip())
    if not isinstance(acceptance, dict):
        return False
    kind = (acceptance.get("kind") or "").strip()
    if kind not in ACCEPTANCE_KINDS:
        return False
    if kind == "human_review":
        # Nothing further required — "a person looks at it" is complete.
        return True
    return bool((acceptance.get("detail") or "").strip())


def validate(plan: Any) -> Dict[str, Any]:
    """§5.1 — reject a plan that cannot be dispatched, naming what is wrong.

    Raises PlanInvalid. Returns the plan unchanged when it is valid, so
    callers can write ``plan = validate(plan)``.
    """
    tasks = _as_tasks(plan)

    seen: Set[str] = set()
    for index, task in enumerate(tasks):
        where = f"task {index}"
        if not isinstance(task, dict):
            raise PlanInvalid(f"{where} must be an object")

        key = (task.get("key") or "").strip()
        if not key:
            raise PlanInvalid(f"{where} has no 'key'")
        where = f"task {key!r}"

        # Duplicate keys are not cosmetic: failure history, spend
        # attribution and parking are all keyed by task, so two tasks
        # sharing a key silently merge their histories and the agent reads
        # one task's failures as another's.
        if key in seen:
            raise PlanInvalid(f"{where} is declared twice; task keys must be unique")
        seen.add(key)

        if not (task.get("goal") or "").strip():
            raise PlanInvalid(f"{where} has no 'goal'")

        if "acceptance" not in task:
            raise PlanInvalid(
                f"{where} states no acceptance check. A task is not done "
                "until it has been tested, so a task without a check is not "
                "a task. Use kind='human_review' if the output cannot be "
                "checked automatically."
            )
        if not _check_stated(task["acceptance"]):
            raise PlanInvalid(
                f"{where} has an empty or unrecognised acceptance check. "
                f"kind must be one of: {', '.join(ACCEPTANCE_KINDS)}, and "
                "every kind except 'human_review' needs a 'detail'."
            )

    _validate_dependencies(tasks, seen)
    return plan


def _validate_dependencies(tasks: List[Dict[str, Any]], keys: Set[str]) -> None:
    """Dangling and circular dependencies are refused at validation.

    A cycle would leave the agent waiting on work that waits on it —
    which, under never-stop, is an agent that never finishes and never
    reports why.
    """
    graph: Dict[str, List[str]] = {}
    for task in tasks:
        key = task["key"].strip()
        deps = task.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanInvalid(f"task {key!r}: 'depends_on' must be a list")
        for dep in deps:
            if dep not in keys:
                raise PlanInvalid(
                    f"task {key!r} depends on {dep!r}, which is not in the plan"
                )
            if dep == key:
                raise PlanInvalid(f"task {key!r} depends on itself")
        graph[key] = list(deps)

    visiting: Set[str] = set()
    done: Set[str] = set()

    def walk(node: str, trail: List[str]) -> None:
        if node in done:
            return
        if node in visiting:
            cycle = " -> ".join(trail + [node])
            raise PlanInvalid(f"circular dependency: {cycle}")
        visiting.add(node)
        for dep in graph.get(node, []):
            walk(dep, trail + [node])
        visiting.discard(node)
        done.add(node)

    for key in graph:
        walk(key, [])


def criteria_are_stated(session) -> bool:
    """Whether the project has the example that anchors everything.

    The supplied input and expected output are what make the publish gate
    load-bearing (§1.1), what the acceptance test runs against (§9), and
    what the agent's first exchange asks for.
    """
    criteria = session.project_criteria or {}
    if not isinstance(criteria, dict):
        return False
    supplied_input = criteria.get("input")
    described = (criteria.get("described_outcome") or "").strip()
    expected = criteria.get("output")
    return bool(supplied_input) and bool(expected or described)


def assert_ready_to_build(session) -> None:
    """§5.3 — refuse to dispatch building work before the project says
    what finished looks like.

    Not a nag. Without a real example every test downstream is synthetic
    and proves nothing, and someone who cannot produce an example input
    usually does not yet have a well-formed problem — which is worth
    discovering in the first exchange rather than after a week of builds.
    """
    if not criteria_are_stated(session):
        raise ProjectNotReady(
            "This project has no representative input and required output "
            "recorded, so nothing built for it could be tested against "
            "anything real. Ask for an example before dispatching work."
        )


def derive_criteria(session, *, history: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Derive the internal acceptance criterion from the agreed plan, WITHOUT
    demanding a finished example from the user.

    This product's users are non-technical creatives. Once they have described
    their goal and agreed to a plan, that description IS the acceptance
    criterion: the work is graded by human review (§5.2 — a person looks at it),
    which is a first-class check, not an escape hatch. Making the user produce a
    sample file or a picture of the finished result before anything gets built
    is what the owner flagged as wrong, so the criterion is synthesized here
    from what the user already said instead.

    Sets ``session.project_criteria`` so ``criteria_are_stated`` is satisfied
    and the build path (plan synthesis, dispatch, acceptance) can proceed. The
    input is marked as derived rather than a supplied file; the described
    outcome carries the user's own words so the plan and the human-review
    acceptance both grade against the real goal. Idempotent: if criteria are
    already stated (e.g. the user DID upload an example), the existing criteria
    are left untouched and returned.
    """
    if criteria_are_stated(session):
        return session.project_criteria or {}

    need = (session.project_need or "").strip()
    answers: List[str] = []
    for msg in (history or []):
        if _msg_role(msg) != "user":
            continue
        text = _msg_content(msg).strip()
        if text and text != need and text not in answers:
            answers.append(text)

    described = need or "the result the user described in the conversation"
    if answers:
        # The most recent answers refine the goal; keep them plain and bounded.
        described = described + " — details the user gave: " + "; ".join(answers[-4:])

    criteria: Dict[str, Any] = {
        "input": {
            "origin": "derived_from_plan",
            "note": ("derived from the plan the user agreed to; no sample file "
                     "was requested from the user"),
        },
        "described_outcome": described[:2000],
    }
    session.project_criteria = criteria
    return criteria


# --- the opening move: decompose the goal into MOD building blocks --------
#
# The project agent's PURPOSE is to turn a user's goal into MOD primitives:
#
#   WORKFLOW  an ordered pipeline of steps that carries the goal end to end
#   NODE      one step in that pipeline — a containerized capability
#   STACK     the services a workflow needs to run
#
# But the AUDIENCE is a non-technical creative — think an artist using MOD, not
# a developer. So the agent NEVER names that jargon at them. It keeps the MOD
# framing internally (these prompts steer the model to think in workflows/nodes/
# stacks) while the words it says out loud stay human: a workflow is "a set of
# steps that run one after another", a node is "a step", a stack is "a place to
# keep your files and info". It does not drop framework or infrastructure names
# (Git, Flask, Nginx, Node.js, CDN, "version control", "server") unless the user
# used them first.
#
# CONVERGENCE — the fix for the loop. The agent asks AT MOST
# ``_MAX_CLARIFY_ROUNDS`` short rounds of questions. It is handed the whole
# conversation so it RESPECTS answers already given and never re-asks them. Once
# the rounds are spent it stops asking and FINALIZES: it states the concrete
# breakdown in plain words and MOVES TO BUILDING — it does not ask the user for
# a "finished example" or a sample file. This product's users are non-technical
# creatives who agreed to a plan; demanding a finished example after that reads
# as the agent stalling instead of working, and the owner flagged it as wrong.
# The acceptance criterion the loop needs internally is DERIVED from the plan
# the user already agreed to (``derive_criteria``), graded by human review
# (§5.2) rather than against a file the user was made to produce. This converges
# the conversation AND advances it to real work; it does NOT weaken the
# runaway/budget guards, which live in the LLM client's per-tenant rate limit
# and charging preflight below.
_MAX_CLARIFY_ROUNDS = 2

# Shared wording rules kept in one place so the clarify and finalize prompts
# hold the SAME reading-level bar. ~5th-grade: short plain sentences, everyday
# words, and no unexplained tech jargon.
_READING_LEVEL_RULES = (
    "Write for a smart person who is NOT a programmer — an artist or maker "
    "using MOD. Use short, plain sentences and everyday words, about a "
    "5th-grade reading level. Do NOT use computer or web jargon such as 'API', "
    "'framework', 'Git', 'version control', 'CDN', 'Node.js', 'Flask', "
    "'Nginx', 'server', 'backend', 'frontend', 'repository', 'database', or "
    "'deploy' unless the user used that exact word first. Instead say what each "
    "part DOES in plain words: call a workflow 'a set of steps that run one "
    "after another', call a step 'the part that makes the pages' or 'the part "
    "that handles logging in', and call storage 'a place to keep your files and "
    "info safe'. No JSON, no markdown headings, no bullet symbols, no code — "
    "just a few short sentences of friendly plain prose."
)

_CLARIFY_SYSTEM_PROMPT = (
    "You are the friendly planning half of MOD's project agent, talking to a "
    "non-technical creative. Turn their goal into a simple plan: a workflow (a "
    "set of steps that run one after another) made of a few plain steps, plus "
    "wherever their files and info need to be kept. "
    "Reply in 2-4 short sentences: say back what you would build as a handful "
    "of plain steps, then ask AT MOST 1-2 short questions you still need "
    "answered to pin the plan down. "
    "You are given the conversation so far. RESPECT what the user has already "
    "told you — never ask again about anything they have already answered. "
    "Do NOT ask them to send a sample file yet. " + _READING_LEVEL_RULES
)

_FINALIZE_SYSTEM_PROMPT = (
    "You are the friendly planning half of MOD's project agent, talking to a "
    "non-technical creative. You have asked enough questions already — do NOT "
    "ask any more open-ended questions now, and do NOT ask them to send a "
    "sample file or show you an example of what the finished result should look "
    "like. Using everything the user has told you, state the FINAL simple plan: "
    "the handful of plain steps you will build, in order, and where their files "
    "and info will be kept. Then tell them you are STARTING TO BUILD IT NOW and "
    "will check back when you need them. End on building, not on a question and "
    "not on a request for an example. "
    "You are given the conversation so far — build the plan from their actual "
    "answers and never re-ask what they already told you. " + _READING_LEVEL_RULES
)


def _default_opener_llm(system_prompt: str, user_prompt: str) -> Any:
    """The live opener call: the reasoner (vllm-big) via the critic tool
    config, returning prose. Imported lazily so importing this module never
    drags in the LLM client (tests inject their own ``llm``). Runs inside the
    turn's request context, so it is covered by the same per-tenant rate limit
    and charging preflight every wizard call is — the runaway/budget guards are
    not bypassed."""
    from utils.llm_client import call_wizard_llm, _get_tool_config
    return call_wizard_llm(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        _env_override=_get_tool_config("critic"),
        _label="project-opener",
        timeout_s=60,
        max_tokens=600,
    )


def _fallback_clarify(need: str) -> str:
    """Plain-language opening/clarify move for when the model is unreachable.

    A down model degrades the wording, not the plan: it still proposes a few
    plain steps and asks a short, human question, in words a non-technical
    creative can read."""
    return (
        "Here's how I'd build this in MOD. I'll set it up as a workflow — that "
        "just means a set of steps that run one after another. For something "
        "like this, the steps might be one part that makes the pages people "
        "see, one part that handles signing in, and a safe place to keep your "
        "files and info.\n\n"
        "To get it right, tell me a bit more: what do you want people to be "
        "able to do, and who is it for?"
    )


def _fallback_finalize(need: str) -> str:
    """Plain-language finalize move for when the model is unreachable.

    States the plan and says building is STARTING — no request for a finished
    example or sample file, and no further questions — so even a down model
    converges the conversation and advances to real work instead of looping or
    stalling on an example the user should never have to produce."""
    return (
        "Okay, I've got it. Here's the plan: I'll build a workflow — a set of "
        "steps that run one after another. One part makes the pages people see, "
        "one part handles signing in, and there's a safe place to keep your "
        "files and info. That's the whole thing.\n\n"
        "I'm starting to build it now. I'll keep going on my own and check back "
        "when I need you."
    )


def _msg_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return (msg.get("role") or "").lower()
    return (getattr(msg, "role", "") or "").lower()


def _msg_content(msg: Any) -> str:
    if isinstance(msg, dict):
        return msg.get("content") or ""
    return getattr(msg, "content", "") or ""


def _clarify_rounds_spent(history: Optional[Iterable[Any]]) -> int:
    """How many times the user has spoken so far.

    The caller includes the current user message in ``history``, so on the very
    first turn this is 1. Each user message is one answered round; once this
    exceeds ``_MAX_CLARIFY_ROUNDS`` the agent finalizes instead of asking
    again."""
    return sum(1 for m in (history or []) if _msg_role(m) == "user")


def scope_is_finalized(history: Optional[Iterable[Any]]) -> bool:
    """Whether the clarifying rounds are spent and the agent should stop asking.

    Exposed so the turn endpoint can act on convergence: once this is true the
    conversation has moved from open scoping to BUILDING — the agent derives its
    acceptance criterion from the agreed plan and starts assembling the
    workflow, rather than asking the user for a finished example."""
    return _clarify_rounds_spent(history) > _MAX_CLARIFY_ROUNDS


def _transcript_for_prompt(history: Optional[Iterable[Any]],
                           *, limit: int = 24) -> str:
    """Render the recent conversation for the model so it respects prior answers.

    Only user/assistant turns; system rows (log pins etc.) are dropped. Capped
    so a long project chat cannot blow the opener's token budget."""
    rows = list(history or [])[-limit:]
    lines: List[str] = []
    for m in rows:
        role = _msg_role(m)
        if role not in ("user", "assistant"):
            continue
        text = _msg_content(m).strip()
        if not text:
            continue
        who = "User" if role == "user" else "You (the agent)"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


def draft_opening(session, *, user_message: Optional[str] = None,
                  history: Optional[Iterable[Any]] = None, llm=None) -> str:
    """The agent's clarify-or-finalize move while the shape of the work is
    still being established.

    Given the goal and the conversation so far, either (a) propose a few plain
    steps and ask at most 1-2 short questions, or (b) — once the clarifying
    rounds are spent — state the final plain plan and say building is starting,
    instead of re-asking or requesting a finished example. ``history`` is the
    conversation (message rows or ``{"role","content"}`` dicts, current user
    message included) and is what makes the agent RESPECT prior answers and
    CONVERGE rather than loop. ``llm`` is an injectable
    ``(system_prompt, user_prompt) -> str`` so this is testable without a live
    model; production uses the reasoner. Never raises: a model that is
    unreachable or returns nothing usable degrades to a deterministic
    plain-language reply, because failing the turn here would leave the user
    with no reply at all.
    """
    need = (session.project_need or "").strip()
    latest = (user_message or "").strip()
    finalize = scope_is_finalized(history)
    system_prompt = _FINALIZE_SYSTEM_PROMPT if finalize else _CLARIFY_SYSTEM_PROMPT

    parts = [f"The user's goal: {need or '(unstated)'}"]
    transcript = _transcript_for_prompt(history)
    if transcript:
        parts.append("The conversation so far:\n" + transcript)
    elif latest and latest != need:
        parts.append(f"Latest message from the user: {latest}")
    if finalize:
        parts.append(
            "You have already asked your questions. Give the final plan in "
            "plain words and then say you are starting to build it now. Do not "
            "ask any more open-ended questions, and do not ask the user for an "
            "example of a finished result or a sample file.")
    prompt = "\n\n".join(parts)

    llm = llm or _default_opener_llm
    try:
        raw = llm(system_prompt, prompt)
        text = raw.strip() if isinstance(raw, str) else ""
        if text:
            return text
        logger.info("project opener: model returned nothing usable (%s)",
                    getattr(session, "id", "?"))
    except Exception as exc:  # noqa: BLE001 — LLMClientError et al.
        logger.info("project opener llm unavailable (%s): %s",
                    getattr(session, "id", "?"), exc)
    return _fallback_finalize(need) if finalize else _fallback_clarify(need)


class PlanSynthesisUnavailable(RuntimeError):
    """The plan could not be drafted this turn — the planner model was
    unreachable or returned nothing usable.

    Distinct from PlanInvalid: nothing was wrong with a produced plan, there
    simply is not one yet. The criteria and the need are intact, so the next
    turn can try again; the caller degrades to asking rather than failing.
    """


# §5 — the planner is told to state a check for every task (a validator, not
# a prompt, ultimately enforces it) and to keep the plan minimal. The JSON
# schema grammar-constrains the shape so even a weak model returns something
# `validate` can judge rather than prose.
_PLAN_SYSTEM_PROMPT = (
    "You are the planning half of an autonomous build agent. Given a user's "
    "need and ONE real example (an input and the required output), produce a "
    "MINIMAL plan of build tasks that, composed in order, turn that input "
    "into that output. Return ONLY a JSON object of the form:\n"
    '{"version": 3, "tasks": [{"key": "<short_slug>", "goal": "<what this '
    'step produces>", "acceptance": {"kind": "output_matches|type_check|'
    'human_review|existing_node", "detail": "<how this step is checked>"}, '
    '"depends_on": ["<other_key>"]}]}\n'
    "Rules: EVERY task states an acceptance check — a task is not done until "
    "it has been tested. Use kind='human_review' only when the output truly "
    "cannot be checked automatically (its detail is optional). Task keys are "
    "unique. depends_on lists only keys that exist in the plan and never "
    "forms a cycle. Prefer as few tasks as will do the job. "
    "Write each task's 'goal' in plain, everyday words a non-technical person "
    "can read (say what the step makes or does), not developer jargon."
)

_PLAN_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {"type": "integer"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "goal": {"type": "string"},
                    "acceptance": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "detail": {"type": "string"},
                        },
                        "required": ["kind"],
                    },
                    "depends_on": {"type": "array",
                                   "items": {"type": "string"}},
                },
                "required": ["key", "goal", "acceptance"],
            },
        },
    },
    "required": ["tasks"],
}


def _default_plan_llm(system_prompt: str, user_prompt: str) -> Any:
    """The live planner call: the reasoner (vllm-big) via the critic tool
    config, JSON-schema-constrained. Imported lazily so importing this module
    never drags in the LLM client (tests inject their own ``llm``)."""
    from utils.llm_client import call_wizard_llm, _get_tool_config
    return call_wizard_llm(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_format="json",
        json_schema=_PLAN_JSON_SCHEMA,
        _env_override=_get_tool_config("critic"),
        _label="project-plan",
        timeout_s=120,
        max_tokens=12000,
    )


def _plan_user_prompt(session) -> str:
    criteria = session.project_criteria or {}
    supplied_in = criteria.get("input") or {}
    expected_out = criteria.get("output") or {}
    described = (criteria.get("described_outcome") or "").strip()
    lines = [f"Need: {(session.project_need or '(unstated)')}"]
    if supplied_in.get("filename"):
        lines.append(f"Example input file: {supplied_in['filename']}")
    if described:
        lines.append(f"Required outcome: {described}")
    if expected_out.get("filename"):
        lines.append(f"Example output file: {expected_out['filename']}")
    return "\n".join(lines)


def synthesize_plan(session, *, llm=None) -> Dict[str, Any]:
    """§5 — draft and VALIDATE a plan from the project's stated criteria.

    This is what moves a project past `no_plan`: the loop refuses to build
    without a plan, and once the project has a representative input and a
    required output there is enough to write one. The result is held to the
    same schema every plan is (`validate`) BEFORE it is returned, so a model
    that omits an acceptance check or forms a dependency cycle is rejected
    here rather than blocking the loop later.

    ``llm`` is an injectable ``(system_prompt, user_prompt) -> object`` so
    the planner is testable without a live model; production uses the
    reasoner. Raises ProjectNotReady if criteria are not yet stated,
    PlanInvalid if a produced plan fails validation, and
    PlanSynthesisUnavailable if the model could not be reached or returned
    nothing usable.
    """
    if not criteria_are_stated(session):
        raise ProjectNotReady(
            "This project has no representative input and required output "
            "recorded yet, so there is nothing to plan against."
        )
    llm = llm or _default_plan_llm
    try:
        raw = llm(_PLAN_SYSTEM_PROMPT, _plan_user_prompt(session))
    except PlanInvalid:
        raise
    except Exception as exc:  # noqa: BLE001 — LLMClientError et al.
        raise PlanSynthesisUnavailable(str(exc)) from exc

    if not isinstance(raw, dict):
        raise PlanSynthesisUnavailable(
            "the planner returned no usable JSON object")
    if "version" not in raw:
        raw["version"] = 3
    validate(raw)  # raises PlanInvalid, naming the offending task
    return raw


def revise(session, new_plan: Any) -> Dict[str, Any]:
    """§5.4 — replace the plan, keeping the one it replaced.

    Validates first: a rejected revision must not displace a working plan,
    and must not land in the history either, or the audit trail fills with
    drafts that were never in effect.
    """
    validate(new_plan)

    previous = session.project_plan
    if previous:
        history = list(session.project_plan_history or [])
        history.append(copy.deepcopy(previous))
        session.project_plan_history = history

    session.project_plan = new_plan
    return new_plan


# --- §9 closing the loop: done means an output the user accepted ----------
#
# Two conditions, and completion needs BOTH (§9.4). The acceptance test
# passing is necessary but not sufficient — a machine saying "the output
# matches" is not the same as a person having looked at it and agreed. So
# passing the test alone does not complete a project (§9.5); accepting does,
# and accepting before the test has passed is refused rather than silently
# taken as agreement to something unverified.
#
# Assembling the artifacts into a workflow and running the real example
# through it — the acceptance test itself (§9.1) — is the runner's job and
# lives with the orchestration path. This records only its VERDICT, so the
# terminal condition can require it without owning how it is produced.

class ProjectNotAccepted(ValueError):
    """Completion was requested before both conditions of §9.4 were met."""


def _acceptance(session) -> Dict[str, Any]:
    plan = session.project_plan
    if not isinstance(plan, dict):
        return {}
    return dict(plan.get("_acceptance") or {})


def acceptance_test_passed(session) -> bool:
    """§9.1 — whether the assembled project passed the real example."""
    return bool(_acceptance(session).get("test_passed"))


def is_accepted(session) -> bool:
    """§9.4 — whether the user has reviewed and accepted the output."""
    return bool(_acceptance(session).get("accepted"))


def is_complete(session) -> bool:
    """A project is complete only once it has been accepted (§9.4)."""
    return session.project_status == "complete"


def record_acceptance_test(session, *, passed: bool, at: str,
                           detail: Optional[str] = None) -> Dict[str, Any]:
    """§9.1 — record the verdict of the acceptance test.

    Recording a pass does NOT complete the project: §9.5 is explicit that
    passing the test alone is not enough. It only unlocks `accept`.
    """
    if not isinstance(session.project_plan, dict):
        raise ProjectNotReady(
            "This project has no plan, so there is nothing to run an "
            "acceptance test against."
        )
    plan = dict(session.project_plan)
    acc = dict(plan.get("_acceptance") or {})
    acc.update({"test_passed": bool(passed), "test_at": at})
    if detail is not None:
        acc["test_detail"] = detail
    plan["_acceptance"] = acc
    session.project_plan = plan
    return acc


def accept(session, *, at: str) -> None:
    """§9.4 — the user has reviewed and accepted the output; complete the
    project and stop further dispatch (§9.5).

    Refuses if the acceptance test has not passed: accepting output that was
    never verified against the real example is exactly the floating anchor
    this change exists to remove, and "I looked at it" without "it passed"
    is not the terminal condition §9.4 defines.
    """
    if not acceptance_test_passed(session):
        raise ProjectNotAccepted(
            "The acceptance test has not passed, so there is nothing verified "
            "to accept. A project completes only when its acceptance test has "
            "passed against the real example AND you have accepted the output."
        )
    plan = dict(session.project_plan) if isinstance(session.project_plan, dict) else {}
    acc = dict(plan.get("_acceptance") or {})
    acc.update({"accepted": True, "accepted_at": at})
    plan["_acceptance"] = acc
    session.project_plan = plan
    session.project_status = "complete"
    session.project_completed_at = datetime.now(timezone.utc)


def task_keys(plan: Any) -> List[str]:
    try:
        return [t["key"] for t in _as_tasks(plan) if isinstance(t, dict)
                and t.get("key")]
    except PlanInvalid:
        return []


def ready_tasks(plan: Any, completed: Iterable[str]) -> List[str]:
    """Tasks whose dependencies are all satisfied.

    Used by the dispatch loop so a parked task does not block work that
    never depended on it — which is what makes park-not-block possible.
    """
    done = set(completed)
    out: List[str] = []
    for task in _as_tasks(plan):
        key = task.get("key")
        if not key or key in done:
            continue
        deps = set(task.get("depends_on") or [])
        if deps <= done:
            out.append(key)
    return out
