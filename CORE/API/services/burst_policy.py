"""burst-to-saas slice 3 — should this run spill, and why not.

A pure function. No network, no database writes, no side effects, so it can be
tested exhaustively and reasoned about without a running system. Everything it
needs is passed in.

The ORDER of the checks is load-bearing, not incidental. It runs cheapest and
most authoritative first:

    kill switch → workflow eligibility → remote-executability → ceiling → wait

A cheap check must never be skipped because an expensive one already passed.
If eligibility were tested after the ceiling, a tenant who had switched burst
off entirely could still see spend attributed to a decision that should never
have been reached — and consent would be evaluated against a run that the
tenant had already forbidden.

Every refusal carries a DISTINGUISHABLE reason, because "why didn't this
burst?" has to be answerable from the run record without reading logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class StayLocalReason(str, Enum):
    """Why a run is not spilling. One value per cause, never a shared 'other'."""

    TENANT_DISABLED = "tenant_disabled"
    WORKFLOW_NOT_ELIGIBLE = "workflow_not_eligible"
    NOT_CONNECTED = "not_connected"
    TARGET_CANNOT_RUN = "target_cannot_run"
    SPEND_CEILING_REACHED = "spend_ceiling_reached"
    WAIT_BELOW_THRESHOLD = "wait_below_threshold"
    NO_CREDIT = "no_credit"


_HUMAN = {
    StayLocalReason.TENANT_DISABLED:
        "Burst is switched off for this tenant.",
    StayLocalReason.WORKFLOW_NOT_ELIGIBLE:
        "This workflow is not marked eligible to run on MOD.",
    StayLocalReason.NOT_CONNECTED:
        "This install is not connected to a MOD account.",
    StayLocalReason.TARGET_CANNOT_RUN:
        "MOD does not have every node this workflow uses.",
    StayLocalReason.SPEND_CEILING_REACHED:
        "The spend ceiling for this period has been reached.",
    StayLocalReason.WAIT_BELOW_THRESHOLD:
        "The queue is not backed up enough to be worth spilling.",
    StayLocalReason.NO_CREDIT:
        "The connected MOD account has no credit.",
}


@dataclass(frozen=True)
class Decision:
    spill: bool
    reason: Optional[StayLocalReason] = None
    detail: Optional[str] = None

    @property
    def human(self) -> Optional[str]:
        if self.spill:
            return None
        base = _HUMAN.get(self.reason, "This run is staying local.")
        return f"{base} {self.detail}".strip() if self.detail else base


@dataclass(frozen=True)
class BurstSettings:
    enabled: bool = False
    spill_threshold_minutes: int = 30
    spend_ceiling: Optional[Decimal] = None
    spend_period_days: int = 30


SPILL = Decision(spill=True)


def decide(
    *,
    settings: Optional[BurstSettings],
    workflow_eligible: bool,
    connected: bool,
    target_can_run: bool,
    projected_wait_minutes: Optional[float],
    period_spend: Optional[Decimal] = None,
    credit_balance: Optional[Decimal] = None,
    target_cannot_run_detail: Optional[str] = None,
) -> Decision:
    """Decide whether one run spills. Pure.

    `projected_wait_minutes` comes from the scheduler's EXISTING estimates
    rather than a second notion of "how long until this starts" — two sources
    for one number is how they drift apart (design D3).
    """
    # 1. The kill switch. Absent settings means never configured, which reads
    #    as off: burst is opt-in, and a missing row must never mean "yes".
    if settings is None or not settings.enabled:
        return Decision(False, StayLocalReason.TENANT_DISABLED)

    # 2. Consent, before anything is measured or spent. A tenant switch cannot
    #    carry the knowledge of what data flows through a given workflow.
    if not workflow_eligible:
        return Decision(False, StayLocalReason.WORKFLOW_NOT_ELIGIBLE)

    # 3. Is there anywhere to send it?
    if not connected:
        return Decision(False, StayLocalReason.NOT_CONNECTED)

    # 4. Can the target actually run it? Before cost, because a run MOD cannot
    #    execute has no cost to weigh.
    if not target_can_run:
        return Decision(False, StayLocalReason.TARGET_CANNOT_RUN,
                        target_cannot_run_detail)

    # 5. Money. Ceiling first, then balance: the operator's own limit outranks
    #    the account's, so a tenant that set £50 is told about their ceiling
    #    rather than about a balance they may not control.
    if settings.spend_ceiling is not None and period_spend is not None:
        if Decimal(period_spend) >= Decimal(settings.spend_ceiling):
            return Decision(
                False, StayLocalReason.SPEND_CEILING_REACHED,
                f"Spent {period_spend} of {settings.spend_ceiling} "
                f"in the last {settings.spend_period_days} days.",
            )
    if credit_balance is not None and Decimal(credit_balance) <= 0:
        return Decision(False, StayLocalReason.NO_CREDIT)

    # 6. Finally, is it even worth it? Last because it is the only check whose
    #    answer changes minute to minute — everything above is a standing
    #    decision, and a standing "no" should not depend on queue weather.
    if projected_wait_minutes is None:
        # Unknown wait is not an emergency. Spilling on a guess spends real
        # money on a queue that might be about to drain.
        return Decision(False, StayLocalReason.WAIT_BELOW_THRESHOLD,
                        "The wait could not be estimated.")
    if projected_wait_minutes < settings.spill_threshold_minutes:
        return Decision(
            False, StayLocalReason.WAIT_BELOW_THRESHOLD,
            f"Would start in about {int(projected_wait_minutes)} min; "
            f"the threshold is {settings.spill_threshold_minutes} min.",
        )

    return SPILL


def stamp_stay_local(run, reason: str) -> None:
    """Record WHY a run stayed local, on the run itself (task 4.2).

    So "why didn't this burst?" is answerable from the record, not by reading
    logs. The column is short; the enum value (`StayLocalReason`) is stored,
    not the human sentence. Leaves status untouched — the run still runs here.
    Lives here (not in burst_record) because the own-cloud scale controller
    stamps refusals too, and burst_record ships only with the MOD-hosted spill.
    """
    run.ran_remotely = False
    run.burst_stay_local_reason = (reason or "")[:48] or None
