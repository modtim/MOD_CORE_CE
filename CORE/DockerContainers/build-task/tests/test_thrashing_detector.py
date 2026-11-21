"""Thrashing detector pins for the test-and-repair stage.

Stage repro 1 (spec 10): LLM oscillated between "add docker-client" and
"remove docker-client" for 10 attempts.
Stage repro 2 (spec 11): A,B,C,C,A,B,C — three ideas cycled across 7
attempts; old detector kept counting "only 1 in window" because each
unique explanation appeared once per 4-attempt window.

The new detector bails when:
  • the same explanation has been used 2 times (3rd time is the bail)
  • or a 2-cycle or 3-cycle has repeated MIN_CYCLE_REPEATS times
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)


def _build_detector():
    """Reconstruct the closure the stage builds inline. The detector
    is created inside main(); we mirror the same shape so we can
    drive it directly in tests."""
    all_explanations: list = []
    MAX_REPEAT_OF_SAME_IDEA = 2
    MIN_CYCLE_REPEATS = 2

    def _is_thrashing(new_explanation: str) -> bool:
        import re as _re
        def _norm(s: str) -> str:
            s = _re.sub(r"\s+", " ", (s or "").lower()).strip()
            return s[:120]
        new_n = _norm(new_explanation)
        if not new_n:
            return False
        history_n = [_norm(e) for e in all_explanations]
        if history_n.count(new_n) >= MAX_REPEAT_OF_SAME_IDEA:
            return True
        prospective = history_n + [new_n]
        for k in (2, 3):
            need = k * MIN_CYCLE_REPEATS
            if len(prospective) < need:
                continue
            tail = prospective[-need:]
            window = tail[:k]
            is_cycle = all(
                tail[i] == window[i % k] for i in range(need)
            )
            if is_cycle and len(set(window)) >= 2:
                return True
        return False

    return all_explanations, _is_thrashing


def test_first_few_unique_attempts_do_not_thrash():
    """A normal repair sequence with novel ideas each time isn't
    thrashing — let the loop run."""
    all_e, detect = _build_detector()
    for e in ["add torch first", "use pip wheel", "bump cuda version"]:
        assert detect(e) is False
        all_e.append(e)


def test_2_cycle_alternation_caught_on_second_full_cycle():
    """Spec 10 reproducer: A, B, A, B → bail when the 4th lands."""
    all_e, detect = _build_detector()
    for e in ["add docker-client", "remove docker-client", "add docker-client"]:
        assert detect(e) is False
        all_e.append(e)
    # 4th: completing the second A,B cycle → thrash
    assert detect("remove docker-client") is True


def test_3_cycle_alternation_caught_on_second_full_cycle():
    """A,B,C,A,B,C → bail when the 6th lands."""
    all_e, detect = _build_detector()
    seq = ["fix A", "try B", "try C", "fix A", "try B"]
    for e in seq:
        assert detect(e) is False
        all_e.append(e)
    assert detect("try C") is True


def test_third_occurrence_of_same_idea_bails():
    """Same explanation 3 times running → done."""
    all_e, detect = _build_detector()
    same = "added the docker socket to satisfy the daemon"
    all_e.append(same)
    all_e.append(same)
    # 3rd occurrence triggers bail.
    assert detect(same) is True


def test_explanations_below_120_chars_match_verbatim_only():
    """Short verbatim explanations (typical for LLM steady-state):
    matching is just string equality after normalize. Paraphrasing
    longer than 120 chars is best-effort; we accept some misses there
    in exchange for tighter false-positive control."""
    all_e, detect = _build_detector()
    short = "Added docker-cli to the image."
    all_e.append(short)
    all_e.append(short)
    # 3rd appearance bails.
    assert detect(short) is True


def test_spec_11_actual_history_bails_by_attempt_7():
    """Replay the spec 11 explanation sequence from the user's log:
    A, B, C, C, A, B, C — three unique ideas cycled across 7 attempts.
    The old detector kept counting "only 1 match in window" because
    each unique explanation appeared once per 4-attempt window. The
    new detector bails at attempt 7: history.count(C) == 2 by then,
    which is the same-idea threshold."""
    all_e, detect = _build_detector()
    A = "Added docker-client Python package to resolve the missing Docker API connection error."
    B = "Removed the docker-client package since the container doesn't need Docker-in-Docker."
    C = "The error indicates the container is trying to interact with Docker-in-Docker (DinD) which is not available. Keep CUDA base, remove docker bits."
    seq_through_3 = [A, B, C]
    for e in seq_through_3:
        assert detect(e) is False
        all_e.append(e)
    # 4th = C again → caught (3rd C is the bail per MAX_REPEAT…
    # actually the 2nd C is still under the threshold, so bail comes
    # later. Pin the actual behavior).
    res_4 = detect(C)
    all_e.append(C)
    # 5th = A again → still 2nd A, not yet bailing on same-idea count;
    # but the cycle detector might catch it (history is A,B,C,C,A —
    # not a clean k-cycle yet).
    res_5 = detect(A)
    all_e.append(A)
    res_6 = detect(B)
    all_e.append(B)
    # 7th = C — this is the 3rd C → SAME-IDEA bail.
    res_7 = detect(C)
    assert res_7 is True, \
        f"detector should have bailed by attempt 7 ({A=}, {B=}, {C=}, "\
        f"results: 4={res_4} 5={res_5} 6={res_6} 7={res_7})"


def test_empty_or_whitespace_explanation_does_not_match():
    """Defensive: an empty / whitespace explanation shouldn't pile up
    on itself and trigger a false-positive."""
    all_e, detect = _build_detector()
    all_e.append("")
    all_e.append("   ")
    assert detect("") is False
    assert detect("a real fix") is False
