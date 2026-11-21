/**
 * buildPreflight tests (GA.LSW.2).
 *
 * The Node Wizard build submit must refuse to enqueue a build while
 * the AI backend is down, and only show the load ETA while it is cold, using the same /wizard-llm-status
 * shape the warming banner renders. A status fetch that itself fails
 * leaves llmStatus null — that must be treated as unavailable.
 */

import { buildPreflight } from "../lib/describe-chat";

const ready = { loaded: true, model: "gemma3:12b", detail: "Model resident in memory." };
const warming = { loaded: false, model: "gemma3:12b", detail: "Model not currently loaded — first analysis will cold-load it." };
const down = { loaded: false, model: "", detail: "Can't reach LLM: Connection refused." };

test("ready (loaded=true) → ok, submit as today", () => {
  expect(buildPreflight(ready)).toEqual({ ok: true });
});

test("warming (loaded=false, cold-load detail) → still builds, with the load ETA notice", () => {
  const r = buildPreflight(warming);
  expect(r.ok).toBe(true);
  if (!r.ok) throw new Error("unreachable");
  expect(r.notice).toMatch(/2 minutes/i);
});

test("down (loaded=false, no model) → refuse with unavailable wording", () => {
  const r = buildPreflight(down);
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.kind).toBe("unavailable");
  expect(r.message).toMatch(/AI backend unavailable/i);
  expect(r.message).not.toMatch(/wrapper script/i);
});

test("fetch failed (status never arrived, null) → treat as unavailable", () => {
  const r = buildPreflight(null);
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.kind).toBe("unavailable");
  expect(r.message).toMatch(/AI backend unavailable/i);
});

test("down with a server detail → refusal message names that cause (fail-loud)", () => {
  const r = buildPreflight({ loaded: false, model: "", detail: "Wizard LLM not configured." });
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.message).toMatch(/AI backend unavailable/i);
  expect(r.message).toContain("Cause: Wizard LLM not configured.");
});

test("down with an empty/blank detail → the bare infra-unavailable constant", () => {
  for (const detail of ["", "   "]) {
    const r = buildPreflight({ loaded: false, model: "", detail });
    expect(r.ok).toBe(false);
    if (r.ok) throw new Error("unreachable");
    expect(r.message).toMatch(/AI backend unavailable/i);
    expect(r.message).not.toMatch(/Cause:/);
  }
});

// GA.LSW.2b — Image mode: needsLlm:false bypasses the unavailable gate
test("down + needsLlm false → ok, image mode skips the LLM gate", () => {
  const r = buildPreflight(down, { needsLlm: false });
  expect(r.ok).toBe(true);
});

test("fetch failed + needsLlm false → ok, image mode skips the LLM gate", () => {
  const r = buildPreflight(null, { needsLlm: false });
  expect(r.ok).toBe(true);
});

// 2400LR-g4bR — adversarial: the bypass must not leak to the other modes.
test("R6: needsLlm omitted + null status still refuses exactly as before", () => {
  const r = buildPreflight(null);
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.kind).toBe("unavailable");
  expect(r.message).toMatch(/AI backend unavailable/i);
});

test("R6: needsLlm true + down still refuses (git/dockerfile/script/describe unchanged)", () => {
  const r = buildPreflight(down, { needsLlm: true });
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.kind).toBe("unavailable");
  expect(r.message).toMatch(/AI backend unavailable/i);
});

test("R6: needsLlm false + healthy status still ok with no notice", () => {
  const r = buildPreflight(ready, { needsLlm: false });
  expect(r.ok).toBe(true);
  expect(r.notice).toBeUndefined();
});

test("R6: needsLlm false + cold status ok but keeps no ETA notice", () => {
  const r = buildPreflight(warming, { needsLlm: false });
  expect(r.ok).toBe(true);
  expect(r.notice).toBeUndefined();
});
