/**
 * Describe-tab chat state machine. Replaces the search → pick → synthesize
 * multi-step form with a conversational flow (δ §21).
 *
 * Pure reducer — no React, no fetch. The wizard component owns the side
 * effects (describe-search call, synthesize call, repo selection) and
 * dispatches actions when each one resolves. This split lets the
 * transition logic be unit-tested without spinning up a renderer.
 *
 * Commit 1 (this file) ships scripted question text. Commit 2 will layer
 * an LLM rewrite on top of the question phrasing — same state machine,
 * just `system_text` strings get personalized before being appended.
 */

export type ChatStep =
  | "initial"          // before the user's first send
  | "searching"        // describe-search in flight
  | "choose_repo"      // candidates shown, awaiting click or "build fresh"
  | "ask_input"        // awaiting user's free-text answer about input shape
  | "ask_output"       // awaiting user's free-text answer about output shape
  | "ready_to_build"   // synthesize-ready, awaiting "Build it" click
  | "synthesizing"     // describe-synthesize in flight
  | "clarify"          // Critic D asked a clarifying question; awaiting answer
  | "search_clarify"   // discovery was weak; asking the user to name a tool/keyword
  | "route_to_workflow" // multi-step request — TERMINAL: must use Workflow Wizard
  | "done";            // synthesis complete; rest of wizard takes over

/** Mirror of the existing /describe-search candidate shape; kept loose
 * so the reducer doesn't depend on the API schema. */
export interface RepoCandidate {
  full_name: string;
  description?: string;
  license_spdx?: string | null;
  stars?: number;
  relevance?: number;
  fit_reason?: string;
  risk_flags?: string[];
  safety_warnings?: string[];
  html_url?: string;
  /** Composite of relevance × log-scaled stars. Drives the sort. */
  composite_score?: number;
  /** True for the single best match when it has a meaningful lead
   * over the runner-up AND decent absolute quality. The chat
   * renders this candidate as a prominent "best match" card above
   * the regular candidate cards. */
  is_recommended?: boolean;
  /** How the synthesize step should install this repo. One of
   * "pip" / "requirements" / "dockerfile" / "clone". Pre-filtered
   * by the server — repos with no recognisable Python entry point
   * never reach the candidate list. Passed through to
   * /describe-synthesize so the generated Dockerfile uses the
   * right install pattern. */
  install_strategy?: string;
}

export interface ChatMessage {
  id: string;
  role: "system" | "user";
  /** Plain text body. Always present. */
  text: string;
  /** When set, render repo-candidate cards inline below the text. */
  candidates?: RepoCandidate[];
  /** When true, the message renders a "Build fresh" inline button. */
  showBuildFreshButton?: boolean;
  /** When true, the message renders a "Build it" inline button. */
  showBuildItButton?: boolean;
  /** When true, the message renders a "Switch to Workflow Wizard" button. The
   * request is a multi-step pipeline that must be built as a workflow — this is
   * the ONLY forward action (no single-node fallback). */
  showSwitchToWorkflowButton?: boolean;
  /** When true, the message is showing a spinner ("thinking…"). */
  pending?: boolean;
}

export interface ChatState {
  step: ChatStep;
  messages: ChatMessage[];
  /** The original description the user typed at turn 1. Preserved so the
   * synthesize call can include it even if the user has typed more
   * messages since. May be re-augmented if the user pushes back in
   * `choose_repo` (re-search with combined text). */
  description: string;
  /** The repo the user picked, or null if "build fresh". */
  chosenRepo: string | null;
  /** Free-text answer to "what goes in?" — passed to synthesize as a
   * hint in the description body. */
  inputAnswer: string;
  /** Free-text answer to "what comes out?" — same purpose. */
  outputAnswer: string;
  /** Free-text refinements the user added after `ready_to_build`. Each
   * is a clarification or push-back ("actually only PDFs that are
   * scanned", "also handle landscape orientation"). Folded into the
   * description body sent to /describe-synthesize. */
  refinements: string[];
  /** True once the user has answered the input/output Q&A. The post-
   * `ask_output` user_send re-runs /describe-search with the enriched
   * description — if that search comes back empty/unavailable, we
   * commit to `ready_to_build` rather than looping the user through
   * Q&A again. */
  hasAskedQA: boolean;
}

export type ChatAction =
  | { type: "init" }
  | { type: "user_send"; text: string }
  | { type: "search_resolved"; candidates: RepoCandidate[]; notConfigured?: boolean; rateLimited?: boolean; recommendBuildFresh?: boolean }
  | { type: "search_failed"; reason: string; code?: string; serverMessage?: string }
  | { type: "pick_repo"; fullName: string }
  | { type: "build_fresh" }
  | { type: "build_it" }
  | { type: "synthesize_failed"; reason: string; category?: string }
  | { type: "synthesize_needs_clarification"; question: string }
  | { type: "search_needs_clarification"; question: string }
  | { type: "route_to_workflow"; reason: string }
  | { type: "synthesize_succeeded" };

function makeId(): string {
  // Good enough for client-only message keys; collision-resistant within
  // a single session, which is the only scope this state lives in.
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

function systemMessage(text: string, extras: Partial<ChatMessage> = {}): ChatMessage {
  return { id: makeId(), role: "system", text, ...extras };
}

function userMessage(text: string): ChatMessage {
  return { id: makeId(), role: "user", text };
}

const FIRST_PROMPT =
  "Hey! Tell me what you're trying to build — in plain words. Don't worry about whether it's possible or how it'd work, just describe the thing you want.";

const SEARCHING_PROMPT =
  "Looking for similar tools on GitHub…";

const PICK_REPO_PROMPT =
  "I found a few projects that look close to what you described. Want to start from one of these, or build it fresh?";

const FROM_SCRATCH_PROMPT =
  "Got it — let's build this from scratch. First question: what kind of thing goes in? A photo? A video? A spreadsheet? Just describe it however makes sense to you.";

// Used when the user types their initial description: we ask the Q&A
// pair BEFORE running /describe-search so the search has full context
// from the start. Distinct from FROM_SCRATCH_PROMPT, which is shown
// only after the user explicitly clicks "Build fresh" past candidates.
const ASK_INPUT_INITIAL_PROMPT =
  "Got it. Before I look for anything, a couple quick questions to make sure I find the right match. What kind of thing goes in? A photo, a video, a spreadsheet — describe it however makes sense.";

const NO_REPOS_PROMPT =
  "I couldn't find a similar project on GitHub, so let's build it from scratch. First question: what kind of thing goes in? A photo? A video? A spreadsheet? Describe it however makes sense.";

const TRIVIAL_BUILD_FRESH_PROMPT =
  "This one's simple enough to build directly — none of the existing projects would do it better than a few lines of code. Let's build it fresh. What kind of thing goes in? A photo? A video? A spreadsheet?";

const NOT_CONFIGURED_PROMPT =
  "I can't search GitHub right now: the platform github_token isn't configured (a platform admin sets it in Settings → Secrets). That's fine — we'll build it from scratch. What kind of thing goes in? A photo? A video? A spreadsheet?";

const RATE_LIMITED_PROMPT =
  "GitHub search is rate-limited right now, so let's just build it from scratch. What kind of thing goes in? A photo? A video? A spreadsheet?";

const SEARCH_FAILED_PROMPT =
  "Search hit an error, but we can still build it from scratch. What kind of thing goes in?";

const ASK_OUTPUT_PROMPT =
  "And what should come out? Same kind of thing? Something different? Just describe what you want at the end.";

const READY_PROMPT_REPO =
  "Great. I'll build a wrapper around that project for you. Ready when you are.";

const READY_PROMPT_SCRATCH =
  "Got it. I have enough to put something together. Ready when you are.";

const BUILDING_PROMPT =
  "Putting it together — this takes a few seconds…";

const SYNTH_FAILED_PROMPT =
  "Couldn't build it on this attempt — the wrapper script didn't pass the pre-build checks. Click Build it again; the analyzer learns from the previous failure and usually gets it on the second try.";

// Category-specific lead-ins. The `binary` and `contract` categories
// are deterministic — retrying with no input change rarely fixes
// them. Surface the underlying mechanic so the user knows what to do.
const SYNTH_FAILED_PROMPT_BINARY =
  "The generated wrapper called binaries that weren't declared in the build manifest. Build it again — the analyzer will see the previous failure and try to declare them, OR rewrite the script to not need them.";

const SYNTH_FAILED_PROMPT_CONTRACT =
  "The generated wrapper's input/output types didn't match what you described. Build it again — the analyzer will fix the mismatch, OR refine your description if it still misses.";

// Infrastructure outages (502/503/504, network drop) are NOT build
// failures — the LLM never ran, so the "wrapper script didn't pass"
// wording is wrong and retrying can't help. The reducer renders a
// distinct infra state with NO "Build it" button for these.
const SYNTH_FAILED_PROMPT_INFRA_UNAVAILABLE =
  "AI backend unavailable — I couldn't reach the build service. This isn't a problem with your description; the platform side is down or unreachable. Please try again in a moment.";

const SYNTH_FAILED_PROMPT_INFRA_WARMING =
  "AI backend warming up — the build model is loading into memory. First analysis can take up to 2 minutes. Please try again in a moment.";

const INFRA_STATUS_CODES = new Set([502, 503, 504]);

/** Classify a synthesize-call failure the way the UI should render
 * it. 502/503/504 HTTP statuses and fetch network TypeErrors are
 * infrastructure states (warming when the reason says so); anything
 * else is a regular build failure (license/binary/contract/default). */
export function classifySynthFailure(opts: {
  httpStatus?: number;
  networkError?: boolean;
  reason?: string;
}): "infra" | "failure" {
  if (typeof opts.httpStatus === "number" && INFRA_STATUS_CODES.has(opts.httpStatus)) {
    return "infra";
  }
  if (opts.networkError) return "infra";
  return "failure";
}

/** True when the failure payload indicates the backend is warming up
 * (cold model load) rather than fully down. Case-insensitive check on
 * the reason string so both API detail text and UI-side messages
 * ("HTTP 503 (warming up…)") match. */
export function isInfraWarming(reason?: string): boolean {
  return !!reason && /warming/i.test(reason);
}

/** Detail wording the API uses when the model is merely cold (will
 * load on first analysis) vs. when the LLM itself is unreachable.
 * /wizard-llm-status returns loaded:false for BOTH states, so the
 * pre-flight splits on the detail text: "not currently loaded —
 * first analysis will cold-load it" = warming; "Can't reach LLM" /
 * "LLM returned HTTP …" / "not configured" = down. */
// "Not loaded, first use will cold-load it" is the model's normal idle
// state, so warming never refuses the build (the build is what loads it);
// it only surfaces the ETA.
const BUILD_PREFLIGHT_WARMING_NOTICE =
  "The build model is loading into memory — the first step can take up to 2 minutes.";

const WIZARD_LLM_COLD_LOAD_DETAIL = /cold-load|not currently loaded|warming/i;

/** Pre-flight decision for the Node Wizard build submit (GA.LSW.2).
 * Takes the same /wizard-llm-status shape the warming banner renders
 * (null = the status fetch itself failed or hasn't returned yet →
 * treat as unavailable). Ready → submit; warming → refuse with the
 * warming ETA message; down → refuse with the infra-unavailable
 * message. Reuses the GA.LSW.1 wording — no duplicated strings. */
export function buildPreflight(
  llmStatus: { loaded: boolean; model: string; detail: string } | null,
  opts?: { needsLlm?: boolean },
): { ok: true; notice?: string } | { ok: false; kind: "unavailable"; message: string } {
  if (!llmStatus) {
    if (opts?.needsLlm === false) {
      return { ok: true };
    }
    return { ok: false, kind: "unavailable", message: SYNTH_FAILED_PROMPT_INFRA_UNAVAILABLE };
  }
  if (!llmStatus.loaded) {
    if (opts?.needsLlm === false) {
      return { ok: true };
    }
    if (WIZARD_LLM_COLD_LOAD_DETAIL.test(llmStatus.detail)) {
      return { ok: true, notice: BUILD_PREFLIGHT_WARMING_NOTICE };
    }
    const cause = (llmStatus.detail ?? "").trim();
    return {
      ok: false,
      kind: "unavailable",
      message: cause
        ? `${SYNTH_FAILED_PROMPT_INFRA_UNAVAILABLE} Cause: ${cause}`
        : SYNTH_FAILED_PROMPT_INFRA_UNAVAILABLE,
    };
  }
  return { ok: true };
}

const SYNTH_SUCCESS_PROMPT =
  "Built! I've handed it over to the next step where you can review the code and tweak anything before it ships.";

const REFINE_SEARCH_PROMPT =
  "Got it — let me look again with that extra context.";

const REFINEMENT_ACK_PROMPT =
  "Noted — I'll factor that in when I build it. Anything else, or want to go?";

// ---------------------------------------------------------------------------
// Type inference from the description.
//
// The wizard used to ALWAYS ask "what kind of file goes in?" then
// "what should come out?" — even if the user's first message was
// "convert pdf to png", where both answers are already on the table.
// Asking redundant questions reads as the bot not listening.
//
// inferTypesFromDescription extracts (input, output) when the
// description mentions a clear "X to Y" / "X into Y" pattern OR
// names a single recognised file type. Returns nulls when it can't
// tell — the wizard falls back to the scripted questions for those
// slots only.
//
// Conservative on purpose: false positives (asking nothing when we
// actually needed clarification) lead to wrong-base-image picks
// downstream, so we only skip when the pattern is unambiguous.
// ---------------------------------------------------------------------------

// File-type tokens we recognise in user prose. Maps the prose word
// to the canonical token the wizard's downstream UI expects (matches
// the wizard's INPUT_TYPE_OPTIONS / OUTPUT_TYPE_OPTIONS shape).
const _FILE_TYPE_VOCAB: Record<string, string> = {
  pdf: "pdf",
  pdfs: "pdf",
  png: "image", pngs: "image",
  jpg: "image", jpgs: "image", jpeg: "image", jpegs: "image",
  gif: "image", gifs: "image",
  webp: "image", webps: "image",
  tiff: "image", tiffs: "image",
  image: "image", images: "image", picture: "image", pictures: "image",
  photo: "image", photos: "image",
  bmp: "image",
  svg: "image",
  mp4: "video", mov: "video", avi: "video", webm: "video", mkv: "video",
  video: "video", videos: "video", movie: "video", movies: "video",
  clip: "video", clips: "video",
  mp3: "audio", wav: "audio", flac: "audio", ogg: "audio",
  audio: "audio", sound: "audio", music: "audio",
  csv: "csv", tsv: "csv",
  spreadsheet: "csv", excel: "csv", xlsx: "csv", xls: "csv",
  json: "json",
  xml: "text",
  txt: "text", text: "text",
  html: "text", htm: "text",
  md: "text", markdown: "text",
  doc: "text", docx: "text", word: "text",
  ppt: "text", pptx: "text", powerpoint: "text",
  glb: "mesh", obj: "mesh", fbx: "mesh", stl: "mesh", ply: "mesh",
  mesh: "mesh", model: "mesh", "3d": "mesh",
  step: "cad", iges: "cad", cad: "cad",
};

function _matchType(word: string): string | null {
  const w = word.toLowerCase().replace(/[^\w]/g, "");
  return _FILE_TYPE_VOCAB[w] || null;
}

export interface InferredTypes {
  input: string | null;
  output: string | null;
  /** True when the inference came from a strong "X to/into Y" pattern
   * (vs. e.g. only ONE type token found and we're guessing). The
   * caller uses this to decide whether to skip ASK_INPUT and
   * ASK_OUTPUT independently or as a pair. */
  confident: boolean;
}

// Domain-vocabulary shortcut. A single-word description like
// "photogrammetry" or "OCR" carries unambiguous I/O — asking the user
// "what kind of thing goes in?" right after they typed it reads as
// the bot not knowing its own domain. Map the well-known categories
// directly; the regex passes below stay as the long-tail fallback for
// "X to Y"-style prose.
//
// Conservative bias: only add a term when the I/O is genuinely
// unambiguous. "image classification" → image-in, json-out is fine;
// something fuzzy like "video understanding" should fall through and
// let the user disambiguate.
const _DOMAIN_VOCAB: Record<string, { input: string; output: string }> = {
  // 3D reconstruction / photogrammetry / SfM / MVS / NeRF
  "photogrammetry":           { input: "image", output: "mesh" },
  "structure-from-motion":    { input: "image", output: "mesh" },
  "structure from motion":    { input: "image", output: "mesh" },
  "sfm":                      { input: "image", output: "mesh" },
  "multi-view stereo":        { input: "image", output: "mesh" },
  "multi view stereo":        { input: "image", output: "mesh" },
  "mvs":                      { input: "image", output: "mesh" },
  "nerf":                     { input: "image", output: "mesh" },
  "neural radiance":          { input: "image", output: "mesh" },
  "gaussian splatting":       { input: "image", output: "mesh" },
  "3d reconstruction":        { input: "image", output: "mesh" },
  "slam":                     { input: "video", output: "mesh" },
  "monocular depth":          { input: "image", output: "image" },
  "depth estimation":         { input: "image", output: "image" },
  // Speech / language
  "ocr":                      { input: "image", output: "text" },
  "transcription":            { input: "audio", output: "text" },
  "speech-to-text":           { input: "audio", output: "text" },
  "speech to text":           { input: "audio", output: "text" },
  "asr":                      { input: "audio", output: "text" },
  "text-to-speech":           { input: "text",  output: "audio" },
  "text to speech":           { input: "text",  output: "audio" },
  "tts":                      { input: "text",  output: "audio" },
  "translation":              { input: "text",  output: "text" },
  "summarization":            { input: "text",  output: "text" },
  "summarisation":            { input: "text",  output: "text" },
  // Vision tasks
  "image captioning":         { input: "image", output: "text" },
  "object detection":         { input: "image", output: "json" },
  "image classification":     { input: "image", output: "json" },
  "semantic segmentation":    { input: "image", output: "image" },
  "instance segmentation":    { input: "image", output: "image" },
  "pose estimation":          { input: "image", output: "json" },
  "face detection":           { input: "image", output: "json" },
  // Document / data
  "pdf extraction":           { input: "pdf",   output: "text" },
  "document parsing":         { input: "pdf",   output: "json" },
};

// Compile each domain term into a word-boundary regex so we don't
// false-fire on substrings (e.g. "translation" inside "transliterate")
// and we still catch the term inside a longer phrase ("OCR pipeline").
const _DOMAIN_VOCAB_RE: Array<{ re: RegExp; io: { input: string; output: string } }> =
  Object.entries(_DOMAIN_VOCAB).map(([term, io]) => ({
    // \W or string-boundary on either side. Hyphens are treated as
    // separators so "structure-from-motion" matches inside
    // "a structure-from-motion library".
    re: new RegExp(
      `(^|[\\s,.;:!?(\\[/])${term.replace(/[-\s]/g, "[\\s\\-]")}($|[\\s,.;:!?)\\]/])`,
      "i",
    ),
    io,
  }));


export function inferTypesFromDescription(text: string): InferredTypes {
  if (!text) return { input: null, output: null, confident: false };
  const lower = text.toLowerCase();

  // 0. Domain vocabulary — a single recognised term is enough to set
  //    both I/O confidently, no question pair needed.
  for (const { re, io } of _DOMAIN_VOCAB_RE) {
    if (re.test(lower)) {
      return { input: io.input, output: io.output, confident: true };
    }
  }


  // 1. "X to Y" / "X into Y" / "X → Y" — covers the common
  //    "convert PDF into PNG", "PDF to image", etc. The X / Y
  //    capture groups stop at any non-word character so we don't
  //    accidentally grab adjectives or modifiers.
  //    Allows optional articles between the verb and the noun
  //    ("convert a pdf into an image").
  const toRe = /(?:convert(?:ing|s)?|transform(?:ing|s)?|turn(?:ing|s)?|change(?:ing|s)?|render(?:ing|s)?|export(?:ing|s)?|extract(?:ing|s)?|parse(?:ing|s)?|process(?:ing|es)?|make(?:ing|s)?|generate(?:ing|s)?)\s+(?:a\s+|an\s+|the\s+|some\s+|my\s+|each\s+)?(\w+)\s+(?:into|to|->|→|as)\s+(?:a\s+|an\s+|the\s+|some\s+)?(\w+)/i;
  const m = lower.match(toRe);
  if (m) {
    const inT = _matchType(m[1]);
    const outT = _matchType(m[2]);
    if (inT && outT) {
      return { input: inT, output: outT, confident: true };
    }
  }

  // 2. "X to Y converter" / "X-to-Y" — same idea, different
  //    phrasing where the noun isn't preceded by a verb.
  const noVerbRe = /(\w+)\s*(?:to|-to-|→|->)\s*(\w+)\s*(?:converter|transformer|exporter|extractor|generator|parser|processor)?/i;
  const m2 = lower.match(noVerbRe);
  if (m2) {
    const inT = _matchType(m2[1]);
    const outT = _matchType(m2[2]);
    if (inT && outT) {
      return { input: inT, output: outT, confident: true };
    }
  }

  // 3. "verb Y from X" — reverse idiom of pattern 1. "Reconstruct
  //    3D scenes from a sequence of images", "Generate a thumbnail
  //    from a video", "Build a mesh from photos". The "from"
  //    preposition is a strong signal that the noun after it is
  //    the INPUT and the noun phrase before it is the OUTPUT.
  //    Y and X are captured as short phrases (≤4 tokens) and
  //    scanned for any recognised vocab token — necessary for
  //    multi-word outputs like "3d scenes" where the head noun
  //    isn't in our vocab but a modifier ("3d") is.
  // Article/quantifier prefix the user might place before the
  // noun on either side of "from". Split into two optional groups
  // so phrases like "a sequence of images" absorb the article AND
  // the collection-quantifier in one go, leaving the bare noun
  // for the capture.
  const _ART = "(?:(?:a|an|the|some|my|each)\\s+)?(?:(?:sequence|series|set|collection|batch|stream|bunch|group|pile|bundle|few|several|multiple)\\s+of\\s+)?";
  const fromRe = new RegExp(
    "(?:reconstruct(?:ing|s)?|build(?:ing|s)?|creat(?:e|es|ing|ed)|generat(?:e|es|ing|ed)|mak(?:e|es|ing)|render(?:ing|s|ed)?|deriv(?:e|es|ing|ed)|recover(?:ing|s|ed)?|produc(?:e|es|ing|ed)|extract(?:ing|s|ed)?|compos(?:e|es|ing|ed)|assembl(?:e|es|ing|ed)|infer(?:s|red|ring)?|synthesi[sz](?:e|es|ing|ed))" +
    "\\s+" + _ART + "([\\w\\s]{1,40}?)\\s+from\\s+" + _ART + "(\\w+)",
    "i",
  );
  const m3 = lower.match(fromRe);
  if (m3) {
    const yTokens = m3[1].split(/\s+/).filter(Boolean);
    const yMatched = yTokens.map(_matchType).find((t): t is string => !!t) || null;
    const inT = _matchType(m3[2]);
    if (inT && yMatched && inT !== yMatched) {
      return { input: inT, output: yMatched, confident: true };
    }
  }

  // 4. Fallback: pick out any recognised type tokens. If exactly
  //    one is found and the description hints at an "out" word, use
  //    it for output; otherwise for input. Less confident — caller
  //    should still validate.
  const tokens = lower.split(/\W+/).filter(Boolean);
  const found = tokens.map(_matchType).filter((t): t is string => !!t);
  const uniq = Array.from(new Set(found));
  if (uniq.length === 1) {
    return { input: uniq[0], output: null, confident: false };
  }
  if (uniq.length === 2) {
    // Two distinct types in prose without a "to" connector — likely
    // still input/output but order may be ambiguous. Treat as
    // non-confident; the wizard will still ask.
    return { input: uniq[0], output: uniq[1], confident: false };
  }
  return { input: null, output: null, confident: false };
}

export const initialChatState: ChatState = {
  step: "initial",
  messages: [systemMessage(FIRST_PROMPT)],
  description: "",
  chosenRepo: null,
  inputAnswer: "",
  outputAnswer: "",
  refinements: [],
  hasAskedQA: false,
};

export function reduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "init":
      return initialChatState;

    case "user_send": {
      const text = action.text.trim();
      if (!text) return state;
      const msgs = [...state.messages, userMessage(text)];

      if (state.step === "initial") {
        // Listen-first: if the user's description already answers
        // "what goes in / what comes out", skip the redundant
        // questions. Asking "what kind of file?" right after the
        // user said "convert PDF into PNG" reads as the bot
        // ignoring them — and reliably annoys users into giving
        // up. inferTypesFromDescription returns confident=true
        // only on unambiguous patterns ("X to/into Y"); anything
        // less and we still ask.
        const inferred = inferTypesFromDescription(text);
        if (inferred.confident && inferred.input && inferred.output) {
          // Both types known — skip ask_input AND ask_output.
          // Surface what we inferred so the user can correct it
          // before /describe-search burns LLM tokens on the wrong
          // problem.
          return {
            ...state,
            description: text,
            inputAnswer: inferred.input,
            outputAnswer: inferred.output,
            hasAskedQA: true,
            step: "searching",
            messages: [
              ...msgs,
              systemMessage(
                `Got it — ${inferred.input} in, ${inferred.output} out. ` +
                "Looking for similar tools on GitHub…",
                { pending: true },
              ),
            ],
          };
        }
        if (inferred.confident && inferred.input) {
          // Only input known; ask just the output question.
          return {
            ...state,
            description: text,
            inputAnswer: inferred.input,
            step: "ask_output",
            messages: [
              ...msgs,
              systemMessage(
                `Got it — sounds like the input is a ${inferred.input}. ` +
                "What should come out at the end?"
              ),
            ],
          };
        }
        // Couldn't infer — fall back to the scripted question pair.
        return {
          ...state,
          description: text,
          step: "ask_input",
          messages: [...msgs, systemMessage(ASK_INPUT_INITIAL_PROMPT)],
        };
      }
      if (state.step === "ask_input") {
        return {
          ...state,
          inputAnswer: text,
          step: "ask_output",
          messages: [...msgs, systemMessage(ASK_OUTPUT_PROMPT)],
        };
      }
      if (state.step === "ask_output") {
        // If the user already picked a repo, we already have what we
        // need — go straight to ready_to_build. Otherwise, take a
        // second pass at /describe-search with the enriched
        // description (initial description + input + output answers).
        // The initial search may have returned 0 hits on a sparse
        // description that the Q&A has now fleshed out.
        if (state.chosenRepo) {
          return {
            ...state,
            outputAnswer: text,
            step: "ready_to_build",
            hasAskedQA: true,
            messages: [...msgs, systemMessage(READY_PROMPT_REPO, { showBuildItButton: true })],
          };
        }
        return {
          ...state,
          outputAnswer: text,
          step: "searching",
          hasAskedQA: true,
          messages: [...msgs, systemMessage(SEARCHING_PROMPT, { pending: true })],
        };
      }
      if (state.step === "choose_repo") {
        // User pushed back on the candidates. Augment the description
        // with their refinement, transition to searching, and let the
        // wizard fire a fresh /describe-search. Clear chosenRepo
        // defensively (they hadn't picked yet but be safe).
        const augmented = `${state.description}\n\n${text}`.trim();
        return {
          ...state,
          description: augmented,
          chosenRepo: null,
          step: "searching",
          messages: [...msgs, systemMessage(SEARCHING_PROMPT, { pending: true })],
        };
      }
      if (state.step === "clarify") {
        // User answered Critic D's clarifying question. Fold the answer in as
        // a refinement and go straight back to synthesizing — the effect layer
        // re-fires synthesize with already_clarified=true so we never ask
        // twice. (buildSynthesizeDescription folds refinements into the prose.)
        return {
          ...state,
          refinements: [...state.refinements, text],
          step: "synthesizing",
          messages: [...msgs, systemMessage(BUILDING_PROMPT, { pending: true })],
        };
      }
      if (state.step === "search_clarify") {
        // User answered the discovery "name a tool/keyword" question. Go back
        // to searching; the effect layer re-fires describe-search with
        // user_tool_answer (and won't ask again — guarded in the component).
        return {
          ...state,
          step: "searching",
          messages: [...msgs, systemMessage(SEARCHING_PROMPT, { pending: true })],
        };
      }
      if (state.step === "ready_to_build") {
        // User is about to hit Build it but wants to add context. Stack
        // the refinement, acknowledge, and stay put — the Build-it
        // button stays available, and synthesize will fold refinements
        // into the description.
        return {
          ...state,
          refinements: [...state.refinements, text],
          messages: [
            ...msgs,
            systemMessage(REFINEMENT_ACK_PROMPT, { showBuildItButton: true }),
          ],
        };
      }
      // Step doesn't accept text input (searching / synthesizing /
      // done) — drop the send.
      return state;
    }

    case "search_resolved": {
      // Replace the trailing pending "Searching…" placeholder with the
      // resolved system message. Avoids two system bubbles in a row.
      const withoutPending = state.messages.filter(m => !m.pending);
      // Candidates worth showing: render them so the user can pick or
      // refine. Suppressed when the backend flags recommend_build_fresh
      // — a search ran but every result is keyword noise for a trivial
      // task (e.g. "convert pdf to png"), so we skip the wall of
      // non-matches and go straight to build-fresh below.
      if (action.candidates.length > 0 && !action.recommendBuildFresh) {
        return {
          ...state,
          step: "choose_repo",
          messages: [
            ...withoutPending,
            systemMessage(PICK_REPO_PROMPT, {
              candidates: action.candidates,
              showBuildFreshButton: true,
            }),
          ],
        };
      }
      // Trivial task — no result was worth showing. Proceed straight to
      // the from-scratch flow with a tailored note (unless Q&A already
      // done, handled just below).
      if (action.recommendBuildFresh && !state.hasAskedQA) {
        return {
          ...state,
          step: "ask_input",
          messages: [...withoutPending, systemMessage(TRIVIAL_BUILD_FRESH_PROMPT)],
        };
      }
      // No candidates (empty / notConfigured / rateLimited / trivial).
      // If Q&A is already done, commit to ready_to_build instead of
      // asking again.
      if (state.hasAskedQA) {
        return {
          ...state,
          step: "ready_to_build",
          messages: [...withoutPending, systemMessage(READY_PROMPT_SCRATCH, { showBuildItButton: true })],
        };
      }
      if (action.notConfigured) {
        return {
          ...state,
          step: "ask_input",
          messages: [...withoutPending, systemMessage(NOT_CONFIGURED_PROMPT)],
        };
      }
      if (action.rateLimited) {
        return {
          ...state,
          step: "ask_input",
          messages: [...withoutPending, systemMessage(RATE_LIMITED_PROMPT)],
        };
      }
      return {
        ...state,
        step: "ask_input",
        messages: [...withoutPending, systemMessage(NO_REPOS_PROMPT)],
      };
    }

    case "search_failed": {
      const withoutPending = state.messages.filter(m => !m.pending);
      // 2400LR-wb12f — a not_configured failure names what is missing (server
      // message when it sent one, else the built-in text); only genuinely
      // unknown errors fall through to the generic SEARCH_FAILED_PROMPT.
      const notConfiguredText =
        action.code === "not_configured"
          ? action.serverMessage
            ? `${action.serverMessage} We can still build it from scratch. What kind of thing goes in?`
            : NOT_CONFIGURED_PROMPT
          : null;
      // Q&A already done → commit to from-scratch synthesis instead of
      // looping the user back through the questions.
      if (state.hasAskedQA) {
        return {
          ...state,
          step: "ready_to_build",
          messages: [
            ...withoutPending,
            ...(notConfiguredText ? [systemMessage(notConfiguredText)] : []),
            systemMessage(READY_PROMPT_SCRATCH, { showBuildItButton: true }),
          ],
        };
      }
      return {
        ...state,
        step: "ask_input",
        messages: [...withoutPending, systemMessage(notConfiguredText ?? SEARCH_FAILED_PROMPT)],
      };
    }

    case "pick_repo": {
      const ready = READY_PROMPT_REPO;
      return {
        ...state,
        chosenRepo: action.fullName,
        step: "ready_to_build",
        messages: [
          ...state.messages,
          userMessage(`Build on top of ${action.fullName}`),
          systemMessage(ready, { showBuildItButton: true }),
        ],
      };
    }

    case "build_fresh": {
      return {
        ...state,
        chosenRepo: null,
        step: "ask_input",
        messages: [
          ...state.messages,
          userMessage("Build it fresh"),
          systemMessage(FROM_SCRATCH_PROMPT),
        ],
      };
    }

    case "build_it": {
      return {
        ...state,
        step: "synthesizing",
        messages: [...state.messages, systemMessage(BUILDING_PROMPT, { pending: true })],
      };
    }

    case "synthesize_failed": {
      const withoutPending = state.messages.filter(m => !m.pending);
      // Infrastructure state: 502/503/504 or a network drop. The LLM
      // never ran, so this is NOT a build failure — never show the
      // "wrapper script didn't pass" text and never offer a retry
      // (no showBuildItButton). If the payload says the model is
      // warming up, use the warming wording (same banner as the
      // Source-step loading overlay); otherwise "unavailable".
      if (action.category === "infra") {
        const lead = isInfraWarming(action.reason)
          ? SYNTH_FAILED_PROMPT_INFRA_WARMING
          : SYNTH_FAILED_PROMPT_INFRA_UNAVAILABLE;
        return {
          ...state,
          step: "ready_to_build",
          messages: [
            ...withoutPending,
            systemMessage(`${lead}${action.reason ? "\n\nDetail: " + action.reason : ""}`),
          ],
        };
      }
      // Licence rejection → re-show the candidate list so the user
      // can pick a different repo (or build fresh). Same-repo retry
      // produces the same failure: the rejected dependency is baked
      // into how the wizard wrapped that specific project.
      //
      // We find the LAST system message that carried candidate cards
      // (set during the earlier `search_resolved` transition) and
      // re-render them under the licence-rejection explanation. If
      // there's no prior candidate list (e.g. the user went straight
      // to build-fresh from initial), fall back to the regular
      // ready-to-build failure flow so they can at least retry.
      const lastCandidateMsg = [...withoutPending]
        .reverse()
        .find(m => m.candidates && m.candidates.length > 0);
      if (action.category === "license" && lastCandidateMsg && state.chosenRepo) {
        const repoLabel = state.chosenRepo;
        const intro = (
          `${repoLabel}'s recipe pulls in code we can't ship — ` +
          `${action.reason || "copyleft licence"}. ` +
          `Pick a different one below, or build it fresh.`
        );
        return {
          ...state,
          step: "choose_repo",
          chosenRepo: null,
          messages: [
            ...withoutPending,
            systemMessage(intro, {
              candidates: lastCandidateMsg.candidates,
              showBuildFreshButton: true,
            }),
          ],
        };
      }
      // Default failure shape: stay in ready_to_build with a retry
      // button + the rejection reason in the bubble. Category-
      // specific lead-ins so the user knows whether retrying is
      // likely to help (binary, contract) or futile (other classes
      // — usually pip licence, schema, etc.).
      let lead = SYNTH_FAILED_PROMPT;
      if (action.category === "binary") {
        lead = SYNTH_FAILED_PROMPT_BINARY;
      } else if (action.category === "contract") {
        lead = SYNTH_FAILED_PROMPT_CONTRACT;
      }
      return {
        ...state,
        step: "ready_to_build",
        messages: [
          ...withoutPending,
          systemMessage(`${lead}${action.reason ? "\n\nDetail: " + action.reason : ""}`, {
            showBuildItButton: true,
          }),
        ],
      };
    }

    case "synthesize_needs_clarification": {
      // Critic D judged the intent ambiguous and asked ONE question. Show it
      // and wait for the user's free-text answer (handled by user_send in the
      // "clarify" step). Non-blocking: the user can still answer briefly and
      // move on — we only ever ask once per request.
      const withoutPending = state.messages.filter(m => !m.pending);
      const q = (action.question || "").trim() ||
        "Quick check — can you clarify exactly what this node should do?";
      return {
        ...state,
        step: "clarify",
        messages: [...withoutPending, systemMessage(q)],
      };
    }

    case "search_needs_clarification": {
      // Discovery found no strong candidate (human-assisted-discovery, Tier 2).
      // Ask the user to name a library / GitHub repo/URL / technical term
      // instead of silently dropping to build-from-scratch. The answer is
      // handled by user_send in the "search_clarify" step (re-fires
      // describe-search with user_tool_answer). We only ask once per request.
      const withoutPending = state.messages.filter(m => !m.pending);
      const q = (action.question || "").trim() ||
        "I couldn't find a strong match. If you know a library, a GitHub repo " +
        "or URL, or the technical term for this, tell me — otherwise I'll build " +
        "it from scratch.";
      return {
        ...state,
        step: "search_clarify",
        messages: [...withoutPending, systemMessage(q)],
      };
    }

    case "route_to_workflow": {
      // The request is a multi-step pipeline. This is TERMINAL for the node
      // path: no "Build it", no single-node fallback — the only way forward is
      // the Workflow Wizard (which decomposes it into nodes). The composer is
      // disabled in this step; the user clicks the button below.
      const withoutPending = state.messages.filter(m => !m.pending);
      const reason = (action.reason || "").trim();
      const body =
        "This looks like a multi-step workflow" +
        (reason ? ` — ${reason}` : ".") +
        "\n\nIt needs more than one node, so it can't be built as a single node. " +
        "Switch to the Workflow Wizard and it'll compose the pipeline for you.";
      return {
        ...state,
        step: "route_to_workflow",
        messages: [...withoutPending, systemMessage(body, { showSwitchToWorkflowButton: true })],
      };
    }

    case "synthesize_succeeded": {
      const withoutPending = state.messages.filter(m => !m.pending);
      return {
        ...state,
        step: "done",
        messages: [...withoutPending, systemMessage(SYNTH_SUCCESS_PROMPT)],
      };
    }

    default:
      return state;
  }
}

/** Known categorical type tokens the backend will enforce against.
 * Mirror of ``_TYPE_EXTENSIONS`` keys in describe_synthesize.py plus
 * the extension-free categoricals. When ``inputAnswer`` /
 * ``outputAnswer`` exactly matches one of these (case-insensitive),
 * we send it as an authoritative ``user_input_type`` /
 * ``user_output_type`` field instead of burying it in prose.
 *
 * Why both: free-form Q&A answers (the user types "a folder of
 * scanned PDFs" in ask_input) won't match — that's fine, they keep
 * going into the description text as before. Only clean tokens
 * land in the contract field. */
const _ENFORCED_TYPE_TOKENS = new Set<string>([
  "image", "video", "audio", "text", "json", "csv", "binary",
  "mesh", "texture", "pointcloud", "pdf", "document", "other",
]);

function _cleanTypeToken(value: string | null | undefined): string | null {
  if (!value) return null;
  const t = value.trim().toLowerCase();
  if (!t) return null;
  return _ENFORCED_TYPE_TOKENS.has(t) ? t : null;
}

/** Authoritative I/O contract for a synthesize call. The backend
 * uses this to override what the LLM might otherwise hallucinate
 * (the "photogrammetry → video" repro). Returns null fields when
 * the wizard only has free-form Q&A answers; the backend then
 * falls back to inferring from description text. */
export function buildSynthesizeContract(state: ChatState): {
  user_input_type: string | null;
  user_output_type: string | null;
} {
  return {
    user_input_type: _cleanTypeToken(state.inputAnswer),
    user_output_type: _cleanTypeToken(state.outputAnswer),
  };
}

/** Compose the description-with-hints string passed to /describe-synthesize.
 *
 * For clean type tokens we send them as separate ``user_input_type`` /
 * ``user_output_type`` fields (see ``buildSynthesizeContract``) — the
 * backend treats those as authoritative. For free-form Q&A answers
 * (the user typed prose, not a recognised type) we still fold them
 * into the description body since the backend can't enforce on free
 * text. Refinements always go into the body. */
export function buildSynthesizeDescription(state: ChatState): string {
  const parts = [state.description];
  // Only fold I/O answers into the description prose when they're
  // NOT recognised tokens — those go in the contract fields instead
  // and would just duplicate (and potentially conflict with) the
  // CONTRACT block in the backend prompt.
  if (state.inputAnswer && !_cleanTypeToken(state.inputAnswer)) {
    parts.push(`Input: ${state.inputAnswer}`);
  }
  if (state.outputAnswer && !_cleanTypeToken(state.outputAnswer)) {
    parts.push(`Output: ${state.outputAnswer}`);
  }
  if (state.refinements.length > 0) {
    parts.push("Refinements from the user:\n" + state.refinements.map(r => `- ${r}`).join("\n"));
  }
  return parts.join("\n\n");
}

/** Does the composer accept user input in this step? UI should disable
 * the input when this returns false. Enabled in every step where the
 * user can either answer a scripted question OR push back on what the
 * system just proposed (`choose_repo`, `ready_to_build`). Disabled
 * only during in-flight waits (`searching`, `synthesizing`) and after
 * `done`. */
export function composerEnabledIn(step: ChatStep): boolean {
  return step === "initial"
      || step === "ask_input"
      || step === "ask_output"
      || step === "choose_repo"
      || step === "ready_to_build"
      || step === "clarify"
      || step === "search_clarify";
}
