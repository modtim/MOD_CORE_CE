/**
 * 2400LR-b15f — the CE wizard build-refusal toast must name the CAUSE.
 *
 * On CE no wizard LLM is configured, so the /wizard-llm-status payload is
 * { loaded:false, detail:"No LLM base URL configured…" } and buildPreflight's
 * refusal is exactly the toast the user sees. The old code (ce-1900y1k copy,
 * describe-chat.ts:249/255) returned the bare SYNTH_FAILED_PROMPT_INFRA_UNAVAILABLE
 * constant and dropped llmStatus.detail — the toast said "AI backend unavailable"
 * with no reason, a dead end for a CE user. Lock in (describe-chat.ts:262-268):
 *   - the server's detail string reaches the refusal payload verbatim
 *   - it is carried with the literal "Cause: " prefix, never dropped or reworded
 *   - the detail is carried as plain TEXT (no HTML escaping/marking)
 *   - blank detail still yields the bare constant (no dangling "Cause:")
 *   - the cold-load (warming) path never gains a Cause suffix
 */

import { buildPreflight } from "../lib/describe-chat";

const ceNoBaseUrl = {
  loaded: false,
  model: "",
  detail: "No LLM base URL configured — set one in Settings → Secrets.",
};
const probeDetail = {
  loaded: false,
  model: "gemma3:12b",
  detail: "Can't reach LLM: gemma3:12b at ollama:11434 refused the probe.",
};
const warming = {
  loaded: false,
  model: "gemma3:12b",
  detail: "Model not currently loaded — first analysis will cold-load it.",
};

test("CE refusal (no LLM base URL) → toast payload names the server detail as Cause:", () => {
  const r = buildPreflight(ceNoBaseUrl);
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.kind).toBe("unavailable");
  expect(r.message).toMatch(/AI backend unavailable/i);
  expect(r.message).toContain(`Cause: ${ceNoBaseUrl.detail}`);
});

test("unreachable LLM (model + host:port probe detail) → detail reaches the payload verbatim", () => {
  const r = buildPreflight(probeDetail);
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.kind).toBe("unavailable");
  expect(r.message).toContain(`Cause: ${probeDetail.detail}`);
});

test("server detail is rendered as TEXT, never HTML markup/escaping", () => {
  const r = buildPreflight({
    loaded: false,
    model: "",
    detail: 'LLM returned HTTP 500 <b>&"x"</b>',
  });
  expect(r.ok).toBe(false);
  if (r.ok) throw new Error("unreachable");
  expect(r.message).toContain('Cause: LLM returned HTTP 500 <b>&"x"</b>');
  expect(r.message).not.toMatch(/&lt;|&gt;|&quot;|&#39;|<span/);
});

test("blank detail → bare infra-unavailable constant, no dangling Cause:", () => {
  for (const detail of ["", "    "]) {
    const r = buildPreflight({ loaded: false, model: "", detail });
    expect(r.ok).toBe(false);
    if (r.ok) throw new Error("unreachable");
    expect(r.message).not.toMatch(/Cause:/);
  }
});

test("cold-load (warming) detail is NOT appended as a cause", () => {
  const r = buildPreflight(warming);
  expect(r.ok).toBe(true);
  if (!r.ok) throw new Error("unreachable");
  expect(r.notice).toMatch(/2 minutes/i);
  expect(String(r.notice)).not.toContain(warming.detail);
  expect(String(r.notice)).not.toMatch(/Cause:/);
});
