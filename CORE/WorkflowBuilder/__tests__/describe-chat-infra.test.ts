/**
 * Reducer tests for the infrastructure-failure state of the
 * Describe-tab chat machine (GA.LSW.1).
 *
 * When /describe-synthesize-stream returns 502/503/504, or the fetch
 * itself fails (network TypeError), the component dispatches
 * synthesize_failed with category "infra". The reducer must:
 *   - render "AI backend unavailable" (or "AI backend warming up"
 *     when the reason says the model is still loading)
 *   - NEVER show the "the wrapper script didn't…" build-failure text
 *   - NEVER offer the "Build it" retry button
 * Other 4xx/5xx statuses and non-network errors keep the existing
 * build-failure behaviour (retry button + wrapper wording).
 */

import {
  ChatState,
  RepoCandidate,
  initialChatState,
  reduce,
  classifySynthFailure,
  isInfraWarming,
} from "../lib/describe-chat";

const candidate: RepoCandidate = {
  full_name: "owner/repo",
  description: "the thing",
  license_spdx: "MIT",
  stars: 1234,
  relevance: 80,
  fit_reason: "purpose-built",
  risk_flags: [],
  safety_warnings: [],
  html_url: "https://github.com/owner/repo",
};

/** Drive the machine to step="synthesizing" (build_it dispatched,
 * pending "Putting it together…" bubble in flight) — the state the
 * synthesize call is in when it fails. */
function synthesizingState(): ChatState {
  let s = reduce(initialChatState, { type: "user_send", text: "convert PDF into PNG" });
  s = reduce(s, { type: "search_resolved", candidates: [candidate] });
  s = reduce(s, { type: "pick_repo", fullName: "owner/repo" });
  return reduce(s, { type: "build_it" });
}

function lastMsg(s: ChatState) {
  return s.messages[s.messages.length - 1];
}

// ----------------------------------------------------------------------
// classifySynthFailure status mapping
// ----------------------------------------------------------------------

test("classifySynthFailure: 502/503/504 map to infra", () => {
  expect(classifySynthFailure({ httpStatus: 502 })).toBe("infra");
  expect(classifySynthFailure({ httpStatus: 503 })).toBe("infra");
  expect(classifySynthFailure({ httpStatus: 504 })).toBe("infra");
});

test("classifySynthFailure: fetch TypeError (network error) maps to infra", () => {
  expect(classifySynthFailure({ networkError: true })).toBe("infra");
  expect(classifySynthFailure({ networkError: true, reason: "Failed to fetch" })).toBe("infra");
});

test("classifySynthFailure: 500/400 and other statuses map to failure", () => {
  expect(classifySynthFailure({ httpStatus: 500 })).toBe("failure");
  expect(classifySynthFailure({ httpStatus: 400 })).toBe("failure");
  expect(classifySynthFailure({ httpStatus: 401 })).toBe("failure");
  expect(classifySynthFailure({ httpStatus: 429 })).toBe("failure");
});

test("isInfraWarming only fires on warming wording in the reason", () => {
  expect(isInfraWarming("HTTP 503 (warming up… 60s elapsed)")).toBe(true);
  expect(isInfraWarming("model is WARMING")).toBe(true);
  expect(isInfraWarming("HTTP 503")).toBe(false);
  expect(isInfraWarming("HTTP 502")).toBe(false);
  expect(isInfraWarming(undefined)).toBe(false);
});

// ----------------------------------------------------------------------
// infra state rendering
// ----------------------------------------------------------------------

test("502 → infra state: 'AI backend unavailable', no retry button, no wrapper text", () => {
  const s = synthesizingState();
  const next = reduce(s, {
    type: "synthesize_failed",
    reason: "HTTP 502",
    category: classifySynthFailure({ httpStatus: 502 }),
  });
  expect(next.step).toBe("ready_to_build");
  const m = lastMsg(next);
  expect(m.text).toMatch(/AI backend unavailable/i);
  expect(m.text).not.toMatch(/wrapper script didn't/i);
  expect(m.showBuildItButton).toBeUndefined();
});

test("503 with warming payload → 'AI backend warming up' wording, no retry button", () => {
  const s = synthesizingState();
  const next = reduce(s, {
    type: "synthesize_failed",
    reason: "HTTP 503 (model warming up — 42s elapsed)",
    category: classifySynthFailure({ httpStatus: 503 }),
  });
  const m = lastMsg(next);
  expect(m.text).toMatch(/AI backend warming up/i);
  expect(m.text).not.toMatch(/wrapper script didn't/i);
  expect(m.showBuildItButton).toBeUndefined();
});

test("504 → infra state with no retry button", () => {
  const s = synthesizingState();
  const next = reduce(s, {
    type: "synthesize_failed",
    reason: "HTTP 504",
    category: classifySynthFailure({ httpStatus: 504 }),
  });
  const m = lastMsg(next);
  expect(m.text).toMatch(/AI backend unavailable/i);
  expect(m.text).not.toMatch(/wrapper script didn't/i);
  expect(m.showBuildItButton).toBeUndefined();
});

test("network error (fetch TypeError) → infra state, no retry button, no wrapper text", () => {
  const s = synthesizingState();
  const msg = "Failed to fetch";
  const next = reduce(s, {
    type: "synthesize_failed",
    reason: msg,
    category: classifySynthFailure({ networkError: true }),
  });
  const m = lastMsg(next);
  expect(m.text).toMatch(/AI backend unavailable/i);
  expect(m.text).not.toMatch(/wrapper script didn't/i);
  expect(m.showBuildItButton).toBeUndefined();
  // The raw detail is still surfaced for debugging.
  expect(m.text).toContain("Detail: Failed to fetch");
});

// ----------------------------------------------------------------------
// unchanged behaviour for regular build failures
// ----------------------------------------------------------------------

test("500 → unchanged build-failure state: wrapper wording + retry button", () => {
  const s = synthesizingState();
  const next = reduce(s, {
    type: "synthesize_failed",
    reason: "HTTP 500",
    category: classifySynthFailure({ httpStatus: 500 }),
  });
  expect(next.step).toBe("ready_to_build");
  const m = lastMsg(next);
  expect(m.text).toMatch(/the wrapper script didn't pass the pre-build checks/i);
  expect(m.text).toMatch(/HTTP 500/);
  expect(m.showBuildItButton).toBe(true);
});

test("400 → unchanged build-failure state: wrapper wording + retry button", () => {
  const s = synthesizingState();
  const next = reduce(s, {
    type: "synthesize_failed",
    reason: "HTTP 400",
    category: classifySynthFailure({ httpStatus: 400 }),
  });
  const m = lastMsg(next);
  expect(m.text).toMatch(/the wrapper script didn't pass the pre-build checks/i);
  expect(m.showBuildItButton).toBe(true);
});

test("parse error (non-network, no category) keeps legacy failure shape with retry", () => {
  // The component only tags fetch rejects as infra; other caught
  // errors arrive without a category and must keep the old behaviour.
  const s = synthesizingState();
  const next = reduce(s, { type: "synthesize_failed", reason: "JSON parse error" });
  const m = lastMsg(next);
  expect(m.text).toMatch(/the wrapper script didn't pass the pre-build checks/i);
  expect(m.showBuildItButton).toBe(true);
});
