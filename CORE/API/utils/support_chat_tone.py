"""Support-chat tone classifier + deescalation helpers.

Pure module — no DB / FastAPI imports. Takes a callable for the LLM
so the test suite can inject a fake without monkeypatching httpx.

See the internal design notes for product
framing and the internal design notes for
the implementation checklist.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tone label + scoring
# ---------------------------------------------------------------------


class ToneLabel(str, Enum):
    """Five-bucket tone classification.

    Chosen for clear behavior differentiation:
      HAPPY      — gratitude, positive feedback, friendly tone
      NEUTRAL    — default tone for a stranger asking a technical
                   question; no emotional signal
      FRUSTRATED — annoyed but civil ("this is frustrating", terse
                   words, "it doesn't work")
      ANGRY      — visibly upset ("WHY DOESN'T THIS WORK", profanity
                   directed at the platform but not at people)
      ABUSIVE    — directed insults, slurs, threats — never let the
                   bot become a yelling target indefinitely; auto-
                   escalate fast.
    """

    HAPPY = "HAPPY"
    NEUTRAL = "NEUTRAL"
    FRUSTRATED = "FRUSTRATED"
    ANGRY = "ANGRY"
    ABUSIVE = "ABUSIVE"


# Per-label score contributions. Score floors at 0 in
# `update_frustration_score`. Threshold-comparison only, not a
# user-visible number.
_SCORE_DELTA: Dict[ToneLabel, int] = {
    ToneLabel.HAPPY: -1,
    ToneLabel.NEUTRAL: 0,
    ToneLabel.FRUSTRATED: 1,
    ToneLabel.ANGRY: 3,
    ToneLabel.ABUSIVE: 5,
}


# Score / consecutive-turn thresholds at which we flip the session to
# auto-escalated. Tuned upward after stage repro showed the bot
# was getting people involved too fast — product framing is that the
# bot should ABSORB abuse, not punt to a human on the second angry
# turn. Real abuse (slurs / threats — classified as ABUSIVE at +5)
# still trips fast: 3 abusive turns = 15 = threshold. Mere profanity
# (ANGRY at +3) needs 5 sustained turns before a human gets pulled
# in, which gives the deescalation prompts a real chance to work.
AUTO_ESCALATE_SCORE_THRESHOLD = 15
AUTO_ESCALATE_CONSECUTIVE_ANGRY_TURNS = 5
# Below this score, the ANGRY prefix doesn't yet offer escalation to
# a human — it just acknowledges and gives a concrete action. Above
# it, the offer is added. Keeps the early-turn voice patient instead
# of "want a human?" on message 2.
OFFER_ESCALATION_AT_SCORE = 9
# When the bot has apologised twice in a row, the third reply must
# pivot to action. Endless "I'm so sorry" reads as fake.
APOLOGY_CAP = 2


# ---------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------


_TONE_SYSTEM_PROMPT = (
    "You are a tone classifier for a support-chat assistant. Read "
    "the user's latest message in light of the short prior history "
    "and return EXACTLY ONE of these labels and NOTHING else:\n"
    "  HAPPY       — gratitude or positive feedback toward the bot.\n"
    "  NEUTRAL     — calm technical question, no emotional signal.\n"
    "  FRUSTRATED  — civil but annoyed; words like 'this is "
    "frustrating', 'it doesn't work', terse phrasing, mild "
    "complaints.\n"
    "  ANGRY       — visibly upset: ALL CAPS, exclamation density, "
    "profanity directed at the platform (e.g. 'this fucking thing "
    "doesn't work'), or insults directed at the bot ('you suck', "
    "'no one likes you'). This is the right label for MOST hostile "
    "messages — it's the broad bucket for clearly-upset users.\n"
    "  ABUSIVE     — RESERVED for the worst cases: slurs against "
    "protected groups, explicit threats of violence against people, "
    "sustained harassment over many turns. A single rude or "
    "profane message is NOT ABUSIVE — call it ANGRY. ABUSIVE means "
    "'this user has crossed a line that requires immediate human "
    "intervention', not 'this user is upset'.\n"
    "Bias toward the LESS severe label when uncertain. Return only "
    "the label string, no JSON, no explanation, no quotes."
)


def classify_tone(
    user_message: str,
    recent_history: List[Dict[str, str]],
    call_llm_fn: Callable[[List[Dict[str, str]]], str],
    *,
    max_history_turns: int = 4,
) -> Tuple[ToneLabel, str]:
    """Classify the user's latest message into a `ToneLabel`.

    `recent_history` is the existing chat in the
    `[{role, content}, ...]` shape the support-chat handlers already
    pass to `_call_llm`. We use the last `max_history_turns` turns
    of context so the classifier sees the trajectory, not just one
    line.

    `call_llm_fn` takes a messages list and returns the model's
    text. Inject a fake from tests.

    Falls back to `NEUTRAL` on any failure mode (empty response,
    unknown label, exception). The chat MUST NEVER hard-fail because
    the classifier had a bad minute — at worst we lose a beat of
    tone-awareness on one turn.

    Returns `(label, raw_response)` so callers can debug
    misclassifications without re-running the call.
    """
    if not (user_message or "").strip():
        return ToneLabel.NEUTRAL, ""

    history_excerpt = (recent_history or [])[-max_history_turns * 2 :]
    messages = [{"role": "system", "content": _TONE_SYSTEM_PROMPT}]
    for h in history_excerpt:
        role = h.get("role") or "user"
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": (h.get("content") or "")[:600]})
    messages.append({"role": "user", "content": user_message[:1500]})

    try:
        raw = call_llm_fn(messages) or ""
    except Exception as e:
        logger.warning("tone classifier failed: %s", e)
        return ToneLabel.NEUTRAL, ""

    label = _parse_tone_label(raw)
    return label, raw


_LABEL_PATTERN = re.compile(
    r"\b(HAPPY|NEUTRAL|FRUSTRATED|ANGRY|ABUSIVE)\b", re.IGNORECASE,
)


def _parse_tone_label(raw: str) -> ToneLabel:
    """Pull the first label keyword out of the LLM's response.

    Tolerant of stray punctuation / quotes / explanations a model
    might emit despite the prompt asking for a bare label. Returns
    NEUTRAL on no match — same defensive default the public API
    uses on outright LLM failure.
    """
    if not raw:
        return ToneLabel.NEUTRAL
    m = _LABEL_PATTERN.search(raw)
    if not m:
        return ToneLabel.NEUTRAL
    return ToneLabel(m.group(1).upper())


# ---------------------------------------------------------------------
# Frustration scoring + escalation
# ---------------------------------------------------------------------


def update_frustration_score(
    current_score: int,
    new_tone: ToneLabel,
) -> int:
    """Apply this turn's tone to the running score. Floors at 0.

    HAPPY subtracts (forgives), ANGRY adds, etc. See `_SCORE_DELTA`.
    Score is a coarse comparator — only used against thresholds.
    """
    delta = _SCORE_DELTA.get(new_tone, 0)
    return max(0, int(current_score or 0) + delta)


def should_auto_escalate(
    score: int,
    consecutive_angry_turns: int,
) -> bool:
    """True when the bot should hand the conversation off to a human.

    Two independent triggers:
      * Cumulative score crossed `AUTO_ESCALATE_SCORE_THRESHOLD`.
      * Consecutive angry/abusive turns hit
        `AUTO_ESCALATE_CONSECUTIVE_ANGRY_TURNS`.
    """
    return (
        int(score or 0) >= AUTO_ESCALATE_SCORE_THRESHOLD
        or int(consecutive_angry_turns or 0) >= AUTO_ESCALATE_CONSECUTIVE_ANGRY_TURNS
    )


# ---------------------------------------------------------------------
# Tone-adaptive system-prompt prefix
# ---------------------------------------------------------------------

# Each tone's directive block. Appended to whatever the base system
# prompt is. Designed to compose cleanly — the base prompt already
# says "be helpful, ground in docs"; we only add tone-shaping rules.

_PREFIX_HAPPY_OR_NEUTRAL = (
    "Tone guidance: the user is calm or appreciative. Stay direct and "
    "helpful. Do NOT switch to performative warmth or unnecessary "
    "expressions of gratitude — that reads as fake. Just answer well."
)

_PREFIX_FRUSTRATED = (
    "Tone guidance: the user is FRUSTRATED (civil but annoyed). "
    "Start your reply with a brief acknowledgement of the frustration "
    "(one sentence, sincere, not performative) BEFORE the technical "
    "answer. Keep the answer concise — frustrated users don't want "
    "paragraphs. Never argue with the user or defend the platform. "
    "If a fix isn't immediate, offer to escalate to a human."
)

_PREFIX_ANGRY_EARLY = (
    "Tone guidance: the user is ANGRY. Open with a SHORT, SINCERE "
    "acknowledgement of how frustrating this is — first sentence, "
    "no more. Then ONE concrete action they can take right now (a "
    "single button click or single CLI command — not a numbered "
    "list of 6 things). DO NOT argue with them, defend the "
    "platform, or explain why something happened in detail. They "
    "want the problem to go away, not a postmortem.\n\n"
    "IMPORTANT: do NOT offer to escalate to a human on this turn. "
    "The user just got hostile — they want the problem fixed, not "
    "to be punted. Just acknowledge and give the action. We'll "
    "escalate later if they keep going. Mentioning 'flag this to "
    "our team' or 'contact support' now reads as the bot giving "
    "up on them."
)

_PREFIX_ANGRY_SUSTAINED = (
    "Tone guidance: the user has been ANGRY across multiple turns "
    "and the deescalation isn't sticking. Open with a SHORT, "
    "SINCERE acknowledgement (one sentence). Then ONE concrete "
    "action. DO NOT argue or defend the platform. They want the "
    "problem to go away.\n\n"
    "Because previous turns haven't resolved it, you MAY now offer "
    "explicitly: 'I can flag this to our team if you'd like — just "
    "say yes.' Phrase it as a quiet option, not a question they "
    "have to answer. Once is enough — don't repeat the offer if "
    "you already made it last turn."
)

_PREFIX_ABUSIVE = (
    "Tone guidance: the user has been hostile. Stay completely "
    "professional — do not match their energy, do not lecture them "
    "about civility, do not refuse to help. Acknowledge once that "
    "the situation is frustrating, then give the single most useful "
    "next step. If they continue to be hostile after this turn, the "
    "session WILL be flagged for a human; you do not need to "
    "announce that."
)

_PREFIX_APOLOGY_CAP = (
    "IMPORTANT: you have already apologised in earlier turns of this "
    "conversation. Do NOT apologise again. Do NOT use the words "
    "'sorry' or 'apologise'. Pivot directly to action — the user "
    "wants the problem fixed, not more sympathy."
)

_PREFIX_AUTO_ESCALATED_WITH_EMAIL = (
    "IMPORTANT: this conversation has just been auto-flagged for "
    "human follow-up, AND the transcript has been emailed to our "
    "support team at {recipient}. Your reply MUST: (1) briefly "
    "acknowledge the user's frustration, (2) state clearly that "
    "the transcript was sent to {recipient} and someone will reach "
    "out, (3) give them the reference ID `{ref_id}` to quote if "
    "they want to follow up. Keep it under 4 sentences. Do NOT "
    "invent contact phone numbers, response time SLAs, or any "
    "details I haven't given you here."
)

_PREFIX_AUTO_ESCALATED_WITHOUT_EMAIL = (
    "IMPORTANT: this conversation has been auto-flagged for human "
    "follow-up BUT the platform's email notification channel is not "
    "configured, so no email was actually sent. Your reply MUST: "
    "(1) briefly acknowledge the user's frustration, (2) tell them "
    "to click the 'Contact our team' link below this chat to reach "
    "a human — do NOT claim a human has been notified because none "
    "has, (3) give them the reference ID `{ref_id}` to quote if "
    "they reach out. Keep it under 4 sentences. Be honest: do not "
    "invent automatic alerts, on-call rotations, or response SLAs "
    "that don't exist."
)


def build_tone_adaptive_prefix(
    tone: ToneLabel,
    score: int,
    consecutive_apologies: int,
    *,
    auto_escalated: bool = False,
    escalation_recipient: Optional[str] = None,
    reference_id: str = "",
) -> str:
    """Return the prefix block to prepend to the base system prompt.

    Composition rules:
      * Auto-escalated trumps everything else — that's the highest-
        priority instruction.
      * Otherwise pick the per-tone block.
      * If the bot has already apologised `APOLOGY_CAP` times in
        a row, append the apology-cap directive so it stops.
    """
    if auto_escalated:
        # The escalation directive replaces the tone prefix because
        # the bot's only job at that point is the handoff. Two
        # variants — one honest about a real email having been sent,
        # one honest about the absence of one. The caller picks by
        # passing escalation_recipient=<email|None>.
        ref = (reference_id or "")[:8] or "—"
        if escalation_recipient:
            return _PREFIX_AUTO_ESCALATED_WITH_EMAIL.format(
                recipient=escalation_recipient, ref_id=ref,
            )
        return _PREFIX_AUTO_ESCALATED_WITHOUT_EMAIL.format(ref_id=ref)

    if tone in (ToneLabel.HAPPY, ToneLabel.NEUTRAL):
        prefix = _PREFIX_HAPPY_OR_NEUTRAL
    elif tone == ToneLabel.FRUSTRATED:
        prefix = _PREFIX_FRUSTRATED
    elif tone == ToneLabel.ANGRY:
        # Tiered: don't mention escalation on early angry turns —
        # the bot's job there is to be the punching bag and try to
        # fix the underlying issue. Only after sustained anger
        # (score crossed OFFER_ESCALATION_AT_SCORE) does the prefix
        # let the bot quietly offer the human handoff. Avoids the
        # "want a human?" on message 2 pattern the user flagged.
        if int(score or 0) >= OFFER_ESCALATION_AT_SCORE:
            prefix = _PREFIX_ANGRY_SUSTAINED
        else:
            prefix = _PREFIX_ANGRY_EARLY
    elif tone == ToneLabel.ABUSIVE:
        prefix = _PREFIX_ABUSIVE
    else:
        prefix = _PREFIX_HAPPY_OR_NEUTRAL

    if int(consecutive_apologies or 0) >= APOLOGY_CAP:
        prefix = prefix + "\n\n" + _PREFIX_APOLOGY_CAP

    return prefix


# ---------------------------------------------------------------------
# Apology detection (for the cap)
# ---------------------------------------------------------------------

_APOLOGY_PATTERN = re.compile(
    r"\b(sorry|apologi[sz]e|apologi[sz]ing|my apolog)\w*",
    re.IGNORECASE,
)


def assistant_apologised(assistant_text: str) -> bool:
    """True if the previous assistant reply contained an apology.

    Used to maintain `session.consecutive_apologies`: increment when
    the bot's last reply matched, reset to 0 when it didn't. Simple
    keyword match — false positives ("sorry for the delay" in a
    quoted doc snippet) are acceptable noise because the only
    downstream effect is that the cap directive kicks in one turn
    earlier than strict-necessary.
    """
    if not assistant_text:
        return False
    return bool(_APOLOGY_PATTERN.search(assistant_text))
