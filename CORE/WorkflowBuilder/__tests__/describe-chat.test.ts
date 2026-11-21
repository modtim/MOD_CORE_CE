/**
 * Reducer tests for the Describe-tab chat state machine (δ §21).
 *
 * The reducer is pure — no React, no fetch — so we can exercise every
 * transition without spinning up a renderer. Locks in:
 *   - initial state has the first system prompt
 *   - user_send transitions depend on the current step
 *   - search_resolved branches by candidates/notConfigured/rateLimited
 *   - synthesize lifecycle (build_it → succeeded/failed) cleans up the
 *     pending placeholder so we never have two "thinking…" bubbles
 *   - composerEnabledIn gates input to the three steps that accept text
 *   - buildSynthesizeDescription folds input/output answers into the
 *     synthesize body so the existing endpoint sees the chat context
 */

import {
  ChatState,
  ChatAction,
  RepoCandidate,
  initialChatState,
  reduce,
  buildSynthesizeDescription,
  composerEnabledIn,
  buildSynthesizeContract,
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

// ----------------------------------------------------------------------
// initial state
// ----------------------------------------------------------------------

test("initial state has step='initial' and one system prompt asking what to build", () => {
  expect(initialChatState.step).toBe("initial");
  expect(initialChatState.messages).toHaveLength(1);
  expect(initialChatState.messages[0].role).toBe("system");
  expect(initialChatState.messages[0].text).toMatch(/tell me what you're trying to build/i);
  expect(initialChatState.description).toBe("");
  expect(initialChatState.chosenRepo).toBeNull();
});

// ----------------------------------------------------------------------
// initial → searching: first user_send kicks off the search
// ----------------------------------------------------------------------

test("first user_send with vague description transitions initial→ask_input", () => {
  // The description doesn't carry recognisable file-type tokens, so
  // the wizard MUST fall back to the scripted question pair.
  const next = reduce(initialChatState, { type: "user_send", text: "build me something cool" });
  expect(next.step).toBe("ask_input");
  expect(next.description).toBe("build me something cool");
  expect(next.messages).toHaveLength(3);
  expect(next.messages[1].role).toBe("user");
  expect(next.messages[1].text).toBe("build me something cool");
  expect(next.messages[2].role).toBe("system");
  expect(next.messages[2].pending).toBeUndefined();
  expect(next.messages[2].text).toMatch(/before I look for anything/i);
});

test("first user_send with 'verb Y from X' (reverse idiom) SKIPS the questions and goes to searching", () => {
  // Tim's repro: "Reconstruct 3D scenes from a sequence of images
  // using photogrammetry". Old behaviour: fell through to the
  // fallback, returned confident=false, wizard then asked
  // "what kind of images or videos…" — redundant AND the LLM
  // rewriter hallucinated "videos" out of nothing.
  // Fix: the "Y from X" pattern recognises "reconstruct … from …"
  // as a strong input/output signal even when Y is a multi-word
  // phrase like "3d scenes" whose head noun isn't in the vocab.
  const next = reduce(initialChatState, {
    type: "user_send",
    text: "Reconstruct 3D scenes from a sequence of images using photogrammetry",
  });
  expect(next.step).toBe("searching");
  expect(next.inputAnswer).toBe("image");
  expect(next.outputAnswer).toBe("mesh");
  expect(next.hasAskedQA).toBe(true);
  const last = next.messages[next.messages.length - 1];
  expect(last.text).toContain("image");
  expect(last.text).toContain("mesh");
  expect(last.pending).toBe(true);
});

test("first user_send with 'convert X to Y' SKIPS the questions and goes to searching", () => {
  // User-reported pain point: bot used to ask "what file are you
  // starting with?" right after the user said "convert PDF into PNG".
  // The inference now folds both into the description and goes
  // straight to /describe-search.
  const next = reduce(initialChatState, { type: "user_send", text: "convert pdfs to png" });
  expect(next.step).toBe("searching");
  expect(next.description).toBe("convert pdfs to png");
  expect(next.inputAnswer).toBe("pdf");
  expect(next.outputAnswer).toBe("image");
  expect(next.hasAskedQA).toBe(true);
  // Last system message must SHOW what was inferred so the user can
  // correct it before search burns LLM tokens on the wrong problem.
  const last = next.messages[next.messages.length - 1];
  expect(last.role).toBe("system");
  expect(last.text).toContain("pdf");
  expect(last.text).toContain("image");
  expect(last.pending).toBe(true);  // search is in-flight
});

// ----------------------------------------------------------------------
// Domain-vocabulary shortcut (Pass 0 of inferTypesFromDescription).
//
// Tim's pushback: "it keeps asking me 'what goes in' it's EXTREAMLY
// obvious on what goes in (which is why the system, when given a
// little time, was able to answer it on its own) — it's better to
// wait than to overask questions. It makes us look unprofessional."
// The X-to-Y / Y-from-X regexes don't match single-word domain
// terms ("photogrammetry", "OCR", "transcription") even though the
// I/O is unambiguous for anyone in the field. The Pass-0 vocab
// table closes that gap.
// ----------------------------------------------------------------------

test("first user_send with bare 'photogrammetry' SKIPS the questions (domain vocab → image/mesh)", () => {
  const next = reduce(initialChatState, { type: "user_send", text: "photogrammetry" });
  expect(next.step).toBe("searching");
  expect(next.inputAnswer).toBe("image");
  expect(next.outputAnswer).toBe("mesh");
  expect(next.hasAskedQA).toBe(true);
});

test("first user_send with bare 'OCR' SKIPS the questions (domain vocab → image/text)", () => {
  const next = reduce(initialChatState, { type: "user_send", text: "OCR" });
  expect(next.step).toBe("searching");
  expect(next.inputAnswer).toBe("image");
  expect(next.outputAnswer).toBe("text");
});

test("first user_send with bare 'transcription' SKIPS the questions (domain vocab → audio/text)", () => {
  const next = reduce(initialChatState, { type: "user_send", text: "transcription" });
  expect(next.step).toBe("searching");
  expect(next.inputAnswer).toBe("audio");
  expect(next.outputAnswer).toBe("text");
});

test("first user_send with 'NeRF' inside a longer phrase still matches (word boundary, not whole-string)", () => {
  const next = reduce(initialChatState, {
    type: "user_send",
    text: "I want to run a NeRF on some pictures",
  });
  expect(next.step).toBe("searching");
  expect(next.inputAnswer).toBe("image");
  expect(next.outputAnswer).toBe("mesh");
});

test("domain vocab matches case-insensitively ('Gaussian Splatting' → image/mesh)", () => {
  const next = reduce(initialChatState, {
    type: "user_send",
    text: "Gaussian Splatting from photos",
  });
  expect(next.step).toBe("searching");
  expect(next.inputAnswer).toBe("image");
  expect(next.outputAnswer).toBe("mesh");
});

test("domain vocab is word-bounded: 'transliterate' must NOT match 'translation'", () => {
  // Substring guard. If the regex slipped to substring matching, the
  // wizard would silently jump to searching with bogus I/O.
  const next = reduce(initialChatState, { type: "user_send", text: "transliterate cuneiform" });
  expect(next.step).toBe("ask_input");
});

test("first user_send with only ONE inferred type asks just the OTHER question", () => {
  // "convert my pdf into something nice" — input is clearly PDF
  // but output is ambiguous ("something nice" isn't a file type).
  // Bot should acknowledge the input it heard and only ask the
  // output question.
  // Note: the regex requires both X and Y to be recognised types
  // for confident inference. If output isn't a type token, we fall
  // back to the scripted pair. Real one-side case is rarer in
  // practice; this test documents the conservative fallback.
  const next = reduce(initialChatState, { type: "user_send", text: "do something with my pdf" });
  // "pdf" appears alone in the prose → confident is FALSE per the
  // inference rules. Bot still asks both questions.
  expect(next.step).toBe("ask_input");
});

test("user_send with empty/whitespace text is dropped", () => {
  const next = reduce(initialChatState, { type: "user_send", text: "   " });
  expect(next).toBe(initialChatState);
});

// ----------------------------------------------------------------------
// search_resolved branches
// ----------------------------------------------------------------------

// In the current state machine, the only way to reach `searching` is
// via Q&A (the initial-search path was removed in favour of asking
// before searching). So a state in `searching` always has
// hasAskedQA=true.
// "build me something cool" doesn't contain any file-type tokens
// (per inferTypesFromDescription's vocab), so the wizard correctly
// falls back to the scripted question pair — keeping these helpers
// landing in ask_input / ask_output as the rest of the test file
// expects. The inference-skip path is covered by its own dedicated
// tests above.
function askInputState(): ChatState {
  return reduce(initialChatState, { type: "user_send", text: "build me something cool" });
}
function searchingState(): ChatState {
  const ai = askInputState();
  // ask_input → ask_output. Use answers that won't trigger the
  // ask_input→searching shortcut (no recognised file-type tokens).
  const ao = reduce(ai, { type: "user_send", text: "some kind of file" });
  return reduce(ao, { type: "user_send", text: "a result" });
}

test("search_resolved with candidates → choose_repo step + repo cards in last message", () => {
  const next = reduce(searchingState(), {
    type: "search_resolved",
    candidates: [candidate],
  });
  expect(next.step).toBe("choose_repo");
  const last = next.messages[next.messages.length - 1];
  expect(last.role).toBe("system");
  expect(last.candidates).toEqual([candidate]);
  expect(last.showBuildFreshButton).toBe(true);
  // pending placeholder gone
  expect(next.messages.find(m => m.pending)).toBeUndefined();
});

test("search_resolved recommendBuildFresh suppresses the candidate wall (Q&A done → ready_to_build, no cards)", () => {
  // The 'pdf to png' case: candidates came back but the backend flagged
  // them all as keyword noise. We must NOT show choose_repo — go to the
  // build-fresh path instead.
  const next = reduce(searchingState(), {
    type: "search_resolved",
    candidates: [candidate],
    recommendBuildFresh: true,
  });
  expect(next.step).toBe("ready_to_build");
  const last = next.messages[next.messages.length - 1];
  expect(last.candidates).toBeUndefined();
  expect(last.showBuildItButton).toBe(true);
});

test("search_resolved recommendBuildFresh before Q&A → ask_input with trivial-task note (no cards)", () => {
  const next = reduce(initialChatState, {
    type: "search_resolved",
    candidates: [candidate],
    recommendBuildFresh: true,
  });
  expect(next.step).toBe("ask_input");
  const last = next.messages[next.messages.length - 1];
  expect(last.candidates).toBeUndefined();
  expect(last.text).toMatch(/build it fresh|simple enough|directly/i);
});

test("search_resolved with no candidates → ready_to_build (Q&A is already done; don't re-ask)", () => {
  const next = reduce(searchingState(), { type: "search_resolved", candidates: [] });
  expect(next.step).toBe("ready_to_build");
  const last = next.messages[next.messages.length - 1];
  expect(last.showBuildItButton).toBe(true);
});

test("search_resolved notConfigured → ready_to_build (commit to from-scratch synthesis)", () => {
  const next = reduce(searchingState(), {
    type: "search_resolved",
    candidates: [],
    notConfigured: true,
  });
  expect(next.step).toBe("ready_to_build");
  expect(next.messages[next.messages.length - 1].showBuildItButton).toBe(true);
});

test("search_resolved rateLimited → ready_to_build", () => {
  const next = reduce(searchingState(), {
    type: "search_resolved",
    candidates: [],
    rateLimited: true,
  });
  expect(next.step).toBe("ready_to_build");
  expect(next.messages[next.messages.length - 1].showBuildItButton).toBe(true);
});

test("search_failed → ready_to_build", () => {
  const next = reduce(searchingState(), { type: "search_failed", reason: "timeout" });
  expect(next.step).toBe("ready_to_build");
  expect(next.messages[next.messages.length - 1].showBuildItButton).toBe(true);
});

// ----------------------------------------------------------------------
// choose_repo → ready_to_build via pick_repo OR ask_input via build_fresh
// ----------------------------------------------------------------------

function chooseRepoState(): ChatState {
  return reduce(searchingState(), {
    type: "search_resolved",
    candidates: [candidate],
  });
}

test("pick_repo → ready_to_build, records chosenRepo, appends user+system messages", () => {
  const next = reduce(chooseRepoState(), { type: "pick_repo", fullName: "owner/repo" });
  expect(next.step).toBe("ready_to_build");
  expect(next.chosenRepo).toBe("owner/repo");
  // expect a user echo + a ready prompt with Build-it button
  const userEcho = next.messages.find(m => m.role === "user" && m.text.includes("owner/repo"));
  expect(userEcho).toBeTruthy();
  const last = next.messages[next.messages.length - 1];
  expect(last.role).toBe("system");
  expect(last.showBuildItButton).toBe(true);
});

test("build_fresh → ask_input, clears chosenRepo, asks input format", () => {
  const next = reduce(chooseRepoState(), { type: "build_fresh" });
  expect(next.step).toBe("ask_input");
  expect(next.chosenRepo).toBeNull();
  const last = next.messages[next.messages.length - 1];
  expect(last.text).toMatch(/what kind of thing goes in/i);
});

test("user_send in choose_repo pushes back: augments description and re-searches", () => {
  const next = reduce(chooseRepoState(), {
    type: "user_send",
    text: "none of these — I specifically need scanned PDFs, not text",
  });
  expect(next.step).toBe("searching");
  expect(next.description).toContain("build me something cool");
  expect(next.description).toContain("scanned PDFs, not text");
  expect(next.chosenRepo).toBeNull();
  // user echo + pending placeholder
  const last = next.messages[next.messages.length - 1];
  expect(last.role).toBe("system");
  expect(last.pending).toBe(true);
  const userEcho = next.messages.find(m => m.role === "user" && m.text.includes("scanned PDFs"));
  expect(userEcho).toBeTruthy();
});

// ----------------------------------------------------------------------
// scripted Q&A: ask_input → ask_output → ready_to_build
// ----------------------------------------------------------------------

test("user_send in ask_input captures input answer, transitions to ask_output", () => {
  const next = reduce(askInputState(), { type: "user_send", text: "a folder of jpg images" });
  expect(next.step).toBe("ask_output");
  expect(next.inputAnswer).toBe("a folder of jpg images");
  expect(next.messages[next.messages.length - 1].text).toMatch(/what should come out/i);
});

test("user_send in ask_output (no chosenRepo) captures output answer, transitions to searching with hasAskedQA=true", () => {
  const ask_output = reduce(askInputState(), { type: "user_send", text: "jpgs" });
  const next = reduce(ask_output, { type: "user_send", text: "a single PNG mosaic" });
  expect(next.step).toBe("searching");
  expect(next.outputAnswer).toBe("a single PNG mosaic");
  expect(next.hasAskedQA).toBe(true);
  // Pending search placeholder appended, no Build-it button yet.
  const last = next.messages[next.messages.length - 1];
  expect(last.pending).toBe(true);
  expect(last.text).toMatch(/looking for similar/i);
  expect(last.showBuildItButton).toBeUndefined();
});

test("user_send in ask_output WITH chosenRepo skips the search and goes straight to ready_to_build", () => {
  // Path to construct: initial → ask_input → ask_output → search →
  // choose_repo → pick_repo lands in ready_to_build, not ask_output.
  // So to exercise the "chosenRepo + ask_output" branch we splice
  // chosenRepo into an ask_output state directly.
  const ask_output_with_repo: ChatState = {
    ...reduce(askInputState(), { type: "user_send", text: "pdf" }),
    chosenRepo: "owner/repo",
  };
  const next = reduce(ask_output_with_repo, { type: "user_send", text: "a png" });
  expect(next.step).toBe("ready_to_build");
  expect(next.hasAskedQA).toBe(true);
  const last = next.messages[next.messages.length - 1];
  expect(last.showBuildItButton).toBe(true);
});

test("user_send in ready_to_build stacks a refinement, posts ack, stays in ready_to_build", () => {
  const ready = readyToBuildState();
  const next = reduce(ready, {
    type: "user_send",
    text: "wait — only handle PDFs under 10 pages",
  });
  expect(next.step).toBe("ready_to_build");
  expect(next.refinements).toEqual(["wait — only handle PDFs under 10 pages"]);
  // Build-it button must still be reachable after the refinement
  const last = next.messages[next.messages.length - 1];
  expect(last.showBuildItButton).toBe(true);
  expect(last.text).toMatch(/factor that in|noted/i);
});

test("multiple refinements stack and synthesize description folds all of them", () => {
  const ready = readyToBuildState();
  const r1 = reduce(ready, { type: "user_send", text: "only scanned PDFs" });
  const r2 = reduce(r1, { type: "user_send", text: "and skip blank pages" });
  expect(r2.refinements).toEqual(["only scanned PDFs", "and skip blank pages"]);
  const desc = buildSynthesizeDescription(r2);
  expect(desc).toContain("- only scanned PDFs");
  expect(desc).toContain("- and skip blank pages");
});

// ----------------------------------------------------------------------
// synthesize lifecycle
// ----------------------------------------------------------------------

function readyToBuildState(): ChatState {
  // Path through the from-scratch flow:
  //   initial → ask_input → ask_output → search empty → ready_to_build
  //   (the post-Q&A search came up dry).
  const searching = searchingState();
  return reduce(searching, { type: "search_resolved", candidates: [] });
}

test("build_it → synthesizing with pending placeholder", () => {
  const next = reduce(readyToBuildState(), { type: "build_it" });
  expect(next.step).toBe("synthesizing");
  const last = next.messages[next.messages.length - 1];
  expect(last.pending).toBe(true);
  expect(last.text).toMatch(/putting it together/i);
});

test("synthesize_succeeded → done, pending placeholder cleared, success prompt shown", () => {
  const synthesizing = reduce(readyToBuildState(), { type: "build_it" });
  const next = reduce(synthesizing, { type: "synthesize_succeeded" });
  expect(next.step).toBe("done");
  expect(next.messages.find(m => m.pending)).toBeUndefined();
  expect(next.messages[next.messages.length - 1].text).toMatch(/built|handed it over/i);
});

test("synthesize_failed → back to ready_to_build with Build-it retry button", () => {
  const synthesizing = reduce(readyToBuildState(), { type: "build_it" });
  const next = reduce(synthesizing, { type: "synthesize_failed", reason: "rate limited" });
  expect(next.step).toBe("ready_to_build");
  expect(next.messages.find(m => m.pending)).toBeUndefined();
  const last = next.messages[next.messages.length - 1];
  expect(last.showBuildItButton).toBe(true);
  // Asserts the ACTION the message has to offer, not the wording it happens to
  // use. This previously matched /snag/i and broke when the copy was reworded,
  // which tells you nothing about whether the retry still works.
  expect(last.text).toMatch(/again/i);
  expect(last.text.length).toBeGreaterThan(0);
});

// ----------------------------------------------------------------------
// synthesize_failed with category="license" — re-show candidate list.
//
// Photogrammetry repro 2026-05-31: the LLM picked `convert`
// (ImageMagick GPL) for the openMVG wrapper. Licence validator
// rejected. The wizard used to show a generic "Hit a snag" + Build-it
// retry button — but retrying with the same repo produces the same
// failure since the rejected dependency is baked into how the wizard
// wrapped THAT specific project. Re-show the candidate list so the
// user can pick a different repo or build fresh.
// ----------------------------------------------------------------------

function chosenRepoState(): ChatState {
  // Drive the chat through Q&A → search → candidates → pick a repo →
  // ready_to_build.
  const searching = searchingState();
  const choose = reduce(searching, {
    type: "search_resolved",
    candidates: [candidate],
  });
  return reduce(choose, { type: "pick_repo", fullName: "owner/repo" });
}

test("synthesize_failed with category='license' re-shows candidates + drops chosenRepo", () => {
  const synthesizing = reduce(chosenRepoState(), { type: "build_it" });
  const next = reduce(synthesizing, {
    type: "synthesize_failed",
    reason: "convert ships GPL-2.0+ (ImageMagick) code; use Pillow",
    category: "license",
  });
  expect(next.step).toBe("choose_repo");
  // chosenRepo cleared so the next pick_repo dispatch doesn't double-
  // up on the prior selection.
  expect(next.chosenRepo).toBeNull();
  const last = next.messages[next.messages.length - 1];
  // Same candidate list re-rendered under the licence explanation.
  expect(last.candidates).toEqual([candidate]);
  expect(last.showBuildFreshButton).toBe(true);
  // The explanation mentions the repo + the licence problem.
  expect(last.text).toMatch(/owner\/repo/);
  expect(last.text).toMatch(/can't ship|copyleft|GPL|ImageMagick/i);
  // No Build-it retry button — same-repo retry is pointless for licence.
  expect(last.showBuildItButton).toBeUndefined();
});

test("synthesize_failed with category='license' but no prior candidates falls back to retry shape", () => {
  // User went straight to build-fresh from initial — never saw a
  // candidate list. License rejection in this state has no list to
  // re-show; fall back to the regular failure shape.
  const synthesizing = reduce(readyToBuildState(), { type: "build_it" });
  const next = reduce(synthesizing, {
    type: "synthesize_failed",
    reason: "license violation",
    category: "license",
  });
  expect(next.step).toBe("ready_to_build");
  const last = next.messages[next.messages.length - 1];
  expect(last.showBuildItButton).toBe(true);
});

test("synthesize_failed with category='binary' keeps the retry shape (different repo would not help)", () => {
  // Binary-undeclared is repo-agnostic — the LLM can retry on the
  // same chosen repo with the rejection in context and pick a
  // different binary. Don't re-show candidates.
  const synthesizing = reduce(chosenRepoState(), { type: "build_it" });
  const next = reduce(synthesizing, {
    type: "synthesize_failed",
    reason: "ffmpeg not declared",
    category: "binary",
  });
  expect(next.step).toBe("ready_to_build");
  // chosenRepo preserved — same-repo retry is fine.
  expect(next.chosenRepo).toBe("owner/repo");
  const last = next.messages[next.messages.length - 1];
  expect(last.showBuildItButton).toBe(true);
});

test("synthesize_failed with no category behaves identically to legacy retry shape", () => {
  // Backwards-compat: callers that don't include category still get
  // the original failure UX.
  const synthesizing = reduce(chosenRepoState(), { type: "build_it" });
  const next = reduce(synthesizing, {
    type: "synthesize_failed",
    reason: "network timeout",
  });
  expect(next.step).toBe("ready_to_build");
  expect(next.messages[next.messages.length - 1].showBuildItButton).toBe(true);
});

// ----------------------------------------------------------------------
// helpers
// ----------------------------------------------------------------------

test("composerEnabledIn allows input wherever the user can speak (incl. push-back steps)", () => {
  expect(composerEnabledIn("initial")).toBe(true);
  expect(composerEnabledIn("ask_input")).toBe(true);
  expect(composerEnabledIn("ask_output")).toBe(true);
  // Push-back: the user can refine in these steps even though buttons exist.
  expect(composerEnabledIn("choose_repo")).toBe(true);
  expect(composerEnabledIn("ready_to_build")).toBe(true);
  // In-flight waits + terminal — disabled.
  expect(composerEnabledIn("searching")).toBe(false);
  expect(composerEnabledIn("synthesizing")).toBe(false);
  expect(composerEnabledIn("done")).toBe(false);
});

test("buildSynthesizeDescription folds FREE-FORM input/output answers into the body", () => {
  // Free-form Q&A answers (not clean type tokens) still go in the
  // description text — the backend can't enforce on prose.
  const state: ChatState = {
    step: "ready_to_build",
    messages: [],
    description: "convert pdfs to png",
    chosenRepo: null,
    inputAnswer: "a single PDF file",
    outputAnswer: "a folder of pngs, one per page",
    refinements: [],
    hasAskedQA: true,
  };
  const desc = buildSynthesizeDescription(state);
  expect(desc).toContain("convert pdfs to png");
  expect(desc).toContain("Input: a single PDF file");
  expect(desc).toContain("Output: a folder of pngs, one per page");
});

test("buildSynthesizeDescription OMITS clean type tokens from the body (they go in the contract instead)", () => {
  // Photogrammetry repro: domain vocab set inputAnswer='image',
  // outputAnswer='mesh'. Those are AUTHORITATIVE and travel in the
  // contract fields — duplicating them as prose can only cause drift.
  const state: ChatState = {
    step: "searching",
    messages: [],
    description: "photogrammetry",
    chosenRepo: null,
    inputAnswer: "image",
    outputAnswer: "mesh",
    refinements: [],
    hasAskedQA: true,
  };
  const desc = buildSynthesizeDescription(state);
  expect(desc).toBe("photogrammetry");
  expect(desc).not.toContain("Input:");
  expect(desc).not.toContain("Output:");
});

test("buildSynthesizeContract returns clean type tokens when the wizard inferred them", () => {
  const state: ChatState = {
    step: "searching",
    messages: [],
    description: "photogrammetry",
    chosenRepo: null,
    inputAnswer: "image",
    outputAnswer: "mesh",
    refinements: [],
    hasAskedQA: true,
  };
  expect(buildSynthesizeContract(state)).toEqual({
    user_input_type: "image",
    user_output_type: "mesh",
  });
});

test("buildSynthesizeContract returns nulls for free-form answers", () => {
  const state: ChatState = {
    step: "ready_to_build",
    messages: [],
    description: "convert pdfs to png",
    chosenRepo: null,
    inputAnswer: "a single PDF file",
    outputAnswer: "a folder of pngs, one per page",
    refinements: [],
    hasAskedQA: true,
  };
  expect(buildSynthesizeContract(state)).toEqual({
    user_input_type: null,
    user_output_type: null,
  });
});

test("buildSynthesizeContract is case-insensitive and trims whitespace", () => {
  const state: ChatState = {
    step: "searching",
    messages: [],
    description: "OCR",
    chosenRepo: null,
    inputAnswer: "  IMAGE  ",
    outputAnswer: "Text",
    refinements: [],
    hasAskedQA: true,
  };
  expect(buildSynthesizeContract(state)).toEqual({
    user_input_type: "image",
    user_output_type: "text",
  });
});

test("buildSynthesizeContract returns null for unknown tokens (niche domain formats)", () => {
  // Domain formats like 'ifc' aren't in the categorical enforcement
  // set; they pass through as the script's accepted_formats but the
  // wizard doesn't try to police them.
  const state: ChatState = {
    step: "searching",
    messages: [],
    description: "parse IFC building model",
    chosenRepo: null,
    inputAnswer: "ifc",
    outputAnswer: "json",
    refinements: [],
    hasAskedQA: true,
  };
  // json is in the enforced set, ifc is not.
  expect(buildSynthesizeContract(state)).toEqual({
    user_input_type: null,
    user_output_type: "json",
  });
});

test("buildSynthesizeDescription folds refinements into the description body", () => {
  const state: ChatState = {
    step: "ready_to_build",
    messages: [],
    description: "convert pdfs to png",
    chosenRepo: null,
    inputAnswer: "a single PDF file",
    outputAnswer: "a folder of pngs",
    refinements: [
      "actually only scanned PDFs, not text ones",
      "also handle landscape orientation",
    ],
    hasAskedQA: true,
  };
  const desc = buildSynthesizeDescription(state);
  expect(desc).toContain("Refinements from the user");
  expect(desc).toContain("- actually only scanned PDFs, not text ones");
  expect(desc).toContain("- also handle landscape orientation");
});

test("buildSynthesizeDescription omits empty input/output sections", () => {
  const state: ChatState = {
    ...initialChatState,
    description: "do a thing",
  };
  expect(buildSynthesizeDescription(state)).toBe("do a thing");
});

// ----------------------------------------------------------------------
// init action resets state (useful when wizard reopens)
// ----------------------------------------------------------------------

test("init action returns a fresh initial state, discarding any prior session", () => {
  const dirty = reduce(initialChatState, { type: "user_send", text: "anything" });
  const fresh = reduce(dirty, { type: "init" });
  expect(fresh).toEqual(initialChatState);
});

// (The δ §21.5 in-place LLM rewrite of scripted prompts was removed.
// Background fetches mutating already-rendered chat text felt like
// the bot "changing its own question" mid-read. Scripted prompts now
// stand on their own; there's no `replace_message_text` action or
// `rewriteKeyForStep` helper to test anymore.)

// 2400LR-wb12f — a not_configured search failure names the cause; only
// unknown errors get the generic "Search hit an error" prompt.
describe("search_failed not_configured (wb12f)", () => {
  const lastText = (st: ChatState) => st.messages[st.messages.length - 1].text;

  // searchingState() has hasAskedQA=true (Q&A already done → the reducer
  // commits to ready_to_build). These cases need the pre-Q&A searching
  // state, where a failed search loops the user back through ask_input.
  const preQA: ChatState = {
    step: "searching",
    messages: [],
    description: "build me something cool",
    chosenRepo: null,
    inputAnswer: "",
    outputAnswer: "",
    refinements: [],
    hasAskedQA: false,
  };

  test("not_configured with a server message renders that message, not the generic prompt", () => {
    const msg = "Node search needs a GitHub token: set github_token in Settings → Secrets.";
    const next = reduce(preQA, {
      type: "search_failed",
      reason: "HTTP 503",
      code: "not_configured",
      serverMessage: msg,
    });
    expect(next.step).toBe("ask_input");
    expect(lastText(next)).toContain(msg);
    expect(lastText(next)).not.toContain("Search hit an error");
  });

  test("not_configured without a server message still names github_token and Settings", () => {
    const next = reduce(preQA, {
      type: "search_failed",
      reason: "HTTP 503",
      code: "not_configured",
    });
    expect(lastText(next)).toContain("github_token");
    expect(lastText(next)).toContain("Settings");
    expect(lastText(next)).not.toContain("Search hit an error");
  });

  test("unknown errors keep the generic prompt", () => {
    const next = reduce(preQA, { type: "search_failed", reason: "HTTP 500" });
    expect(lastText(next)).toContain("Search hit an error");
  });
});
