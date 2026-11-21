"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";
import { useEntitlements } from "@/hooks/use-entitlements";

/**
 * conversational-project-builder §5 — the man-in-the-middle Project view.
 *
 * This is the SAME surface as the legacy Project Wizard (opened from the same
 * button), but when PROJECT_CONVERSATION_ENABLED is on the parent renders THIS
 * component instead of the one-shot flow. It turns the project into a
 * conversation with the human as the checkpoint at every meaningful step:
 *
 *   align  → the chat pane (left of centre): describe the goal, MOD clarifies
 *            or proposes a plan, and NOTHING is built until you confirm.
 *   build  → confirming generates a shared, opsx-style checklist (right pane)
 *            that reflects each task's live status; the in-progress task is
 *            highlighted.
 *   test   → when MOD finishes a piece it STOPS and hands it off in-chat as a
 *            "Test this: <link> — check <thing>" card with an Open button.
 *   feedback → you reply in plain language; the reply is classified into
 *            approve / infra-broken / rework / change-scope, shown back to you
 *            with one-tap override chips, and routed. A change-scope reply
 *            renders a checklist diff you confirm before it is applied.
 *
 * The whole loop (§5.5) lives here — no external tool to converse, test, or
 * give feedback. Streaming (§5.4) is the existing SSE-with-polling-fallback
 * contract; the backend does not yet emit SSE for project turns, so this uses
 * the poll fallback (re-GET /conversation) while a build is live. See the
 * SSE TODO on `startPolling` below.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  CheckCircle2,
  Circle,
  CircleDashed,
  CircleDot,
  Compass,
  ExternalLink,
  Hammer,
  Loader2,
  Plus,
  RotateCcw,
  Send,
  Wrench,
  X,
} from "lucide-react";

const API_URL = apiUrl();

interface ProjectSession {
  id: string;
  kind: string;
  title?: string | null;
  created_at: string;
  last_message_at: string;
}

interface Turn {
  id: number;
  author: string;
  role: string; // "user" | "system"
  body: string;
  intent?: string | null;
  created_at?: string | null;
}

interface ChecklistTask {
  id: number;
  order_index: number;
  task_key: string;
  title: string;
  body?: string | null;
  status: string; // todo | building | awaiting-test | accepted | reworking
}

interface Checkpoint {
  id: number;
  checklist_task_id: number;
  artifact_ref?: unknown;
  artifact_link?: string | null;
  what_to_check?: string | null;
}

interface ScopeDiff {
  summary?: string;
  adds?: Array<{ title: string; body?: string; after_task_key?: string | null }>;
  reorders?: Array<{ task_key: string; after_task_key?: string | null }>;
  removes?: string[];
}

interface FeedbackResult {
  classified_intent?: string;
  intent_source?: string;
  reason?: string;
  override_options?: string[];
  action?: string;
  reply?: string;
  proposed_diff?: ScopeDiff;
  requires_confirm?: boolean;
}

interface Props {
  open: boolean;
  onClose: () => void;
  accessToken: string | null;
  /** When set, open this session on mount (e.g. a Wizard Queue "Open"
   *  artifact for an agent-project run). Consumed once. */
  initialSessionId?: string | null;
}

// Plain-language labels for the four routable intents. The backend speaks
// slugs; a person should never have to.
const INTENT_LABEL: Record<string, string> = {
  approve: "Looks good",
  "infra-broken": "Stack isn't up",
  rework: "Needs rework",
  "change-scope": "Change the plan",
};

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return days < 30 ? `${days}d ago` : `${Math.floor(days / 7)}w ago`;
}

/** One row of the live checklist. Icon + colour reflect the checkpoint
 *  lifecycle; the in-progress task is boxed so it reads at a glance. */
function ChecklistRow({ task }: { task: ChecklistTask }) {
  const live = task.status === "building" || task.status === "reworking";
  const map: Record<string, { icon: React.ReactNode; label: string; cls: string }> = {
    todo: { icon: <Circle className="h-3.5 w-3.5" />, label: "To do", cls: "text-gray-400" },
    building: {
      icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
      label: "Building",
      cls: "text-blue-600",
    },
    "awaiting-test": {
      icon: <CircleDot className="h-3.5 w-3.5" />,
      label: "Test this",
      cls: "text-amber-600",
    },
    reworking: {
      icon: <RotateCcw className="h-3.5 w-3.5 animate-spin" />,
      label: "Reworking",
      cls: "text-orange-600",
    },
    accepted: {
      icon: <CheckCircle2 className="h-3.5 w-3.5" />,
      label: "Accepted",
      cls: "text-green-600",
    },
  };
  const s = map[task.status] || {
    icon: <CircleDashed className="h-3.5 w-3.5" />,
    label: task.status,
    cls: "text-gray-400",
  };
  return (
    <div
      className={`mb-1.5 rounded-md border px-2.5 py-2 ${
        live
          ? "border-blue-300 bg-blue-50 ring-1 ring-blue-200"
          : task.status === "awaiting-test"
            ? "border-amber-300 bg-amber-50"
            : "border-transparent"
      }`}
    >
      <div className="flex items-start gap-2">
        <span className={`mt-0.5 shrink-0 ${s.cls}`}>{s.icon}</span>
        <div className="min-w-0 flex-1">
          <span
            className={`block text-xs leading-snug ${
              task.status === "accepted"
                ? "text-gray-400 line-through"
                : "text-gray-800"
            }`}
          >
            {task.title}
          </span>
          <span className={`mt-0.5 block text-[10px] font-medium uppercase tracking-wide ${s.cls}`}>
            {s.label}
          </span>
        </div>
      </div>
    </div>
  );
}

export function ProjectConversationPanel({ open, onClose, accessToken, initialSessionId }: Props) {
  const [sessions, setSessions] = useState<ProjectSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [checklist, setChecklist] = useState<ChecklistTask[]>([]);
  const [gateState, setGateState] = useState<string | null>(null);
  const [checkpoint, setCheckpoint] = useState<Checkpoint | null>(null);
  const [draft, setDraft] = useState("");
  const [feedbackDraft, setFeedbackDraft] = useState("");
  const [feedback, setFeedback] = useState<FeedbackResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const authFetch = useCallback(
    (path: string, init?: RequestInit) =>
      fetch(`${API_URL}${path}`, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
          ...(init?.headers || {}),
        },
      }),
    [accessToken],
  );

  // FIX-CE-SHOW-PROJECT-CHAT-HISTORY (11-10, owner decision Tim): the project
  // chat history rail ships on EVERY edition. The support_chat router package
  // ships in CE (open-core/manifest.yaml deny globs :1595/:1753 are overridden
  // by deny_exceptions :2475-:2488), so GET /v1/support/sessions answers on CE
  // too — there is no edition skip and no CE notice here. What remains is only
  // the fail-open entitlements read: it holds the rail fetch until entitlements
  // resolve (paid behaviour unchanged) and releases on a 5s fallback.
  const { edition, loading } = useEntitlements();
  const railGateRef = useRef<{ wait: Promise<void>; release: () => void } | null>(null);
  if (!railGateRef.current) {
    let release: (() => void) | null = null;
    const wait = new Promise<void>((resolve) => { release = resolve; });
    railGateRef.current = { wait, release: release ?? (() => {}) };
  }

  const loadSessions = useCallback(async () => {
    if (!accessToken) return;
    try {
      await railGateRef.current!.wait;
      const resp = await authFetch("/support/sessions?kind=project&limit=50");
      if (!resp.ok) return;
      setSessions(await resp.json());
    } catch {
      /* the rail is navigation, not content */
    }
  }, [accessToken, authFetch]);

  /** The single source of truth: the full persisted conversation + checklist +
   *  gate + current hand-off. Every action re-reads this, and the poll
   *  fallback re-reads it on a timer while a build is live. */
  const refresh = useCallback(
    async (id: string) => {
      try {
        const resp = await authFetch(`/projects/${id}/conversation`);
        if (!resp.ok) return;
        const data = await resp.json();
        setTurns(data.turns || []);
        setChecklist(data.checklist || []);
        setGateState(data.gate_state || null);
        setCheckpoint(data.checkpoint || null);
      } catch {
        /* a dropped poll must not surface as an error banner */
      }
    },
    [authFetch],
  );

  useEffect(() => {
    if (open) void loadSessions();
  }, [open, loadSessions]);

  useEffect(() => {
    if (!loading && railGateRef.current) railGateRef.current.release();
    const fallback = setTimeout(() => {
      if (railGateRef.current) railGateRef.current.release();
    }, 5000);
    return () => clearTimeout(fallback);
  }, [edition, loading]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [turns]);

  // §5.4 — stream new turns/checklist changes. The contract is
  // SSE-with-polling-fallback; the backend does not yet emit SSE on the
  // project channel, so we poll /conversation while the project is actively
  // building or awaiting the user. When SSE lands for project turns, subscribe
  // here and keep this poll as the fallback (mirror workflow-wizard-panel).
  const startPolling = useCallback(
    (id: string) => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(() => {
        if (document.visibilityState === "hidden") return;
        void refresh(id);
      }, 5000);
    },
    [refresh],
  );

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!open || !sessionId) {
      stopPolling();
      return;
    }
    // Only poll while there is live movement to catch — an aligning or done
    // project changes only on a user action, which refreshes inline.
    if (gateState === "building" || gateState === "awaiting-user") {
      startPolling(sessionId);
    } else {
      stopPolling();
    }
    return () => stopPolling();
  }, [open, sessionId, gateState, startPolling, stopPolling]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const openSession = async (id: string) => {
    setLoadingHistory(true);
    setError(null);
    setFeedback(null);
    try {
      setSessionId(id);
      await refresh(id);
    } catch (e) {
      setError(`Could not open that project: ${(e as Error).message}`);
    } finally {
      setLoadingHistory(false);
    }
  };

  // Wizard Queue "Open" artifact: land on the specific session. Runs once
  // per open when a session is supplied.
  const consumedInitial = useRef(false);
  useEffect(() => {
    if (!open || !initialSessionId || consumedInitial.current) return;
    consumedInitial.current = true;
    void openSession(initialSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialSessionId]);

  const startNew = () => {
    setSessionId(null);
    setTurns([]);
    setChecklist([]);
    setGateState(null);
    setCheckpoint(null);
    setFeedback(null);
    setError(null);
    setDraft("");
  };

  // Post an aligning turn. Creates the project on the first message.
  const send = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setDraft("");
    // optimistic echo
    setTurns(t => [
      ...t,
      { id: -Date.now(), author: "me", role: "user", body: text },
    ]);
    try {
      const path = sessionId
        ? `/projects/${sessionId}/conversation`
        : "/projects/conversation";
      const resp = await authFetch(path, {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      const id = data.session_id as string;
      setSessionId(id);
      setGateState(data.gate_state || "aligning");
      await refresh(id);
      void loadSessions();
    } catch (e) {
      setError((e as Error).message);
      setDraft(text);
      setTurns(t => t.filter(x => x.id !== undefined && x.id > 0));
    } finally {
      setBusy(false);
    }
  };

  // §2.2 align-before-build guard — the ONLY thing that generates the checklist
  // and starts the build. A deliberate button, never inferred from the text.
  const confirmPlan = async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch(`/projects/${sessionId}/conversation`, {
        method: "POST",
        body: JSON.stringify({
          message: "Looks good — go ahead and build it.",
          confirm: true,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      setGateState(data.gate_state || "building");
      await refresh(sessionId);
      void loadSessions();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const approveCheckpoint = async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    setFeedback(null);
    try {
      const resp = await authFetch(
        `/projects/${sessionId}/checkpoint/approve`,
        { method: "POST" },
      );
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      await refresh(sessionId);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // §4 — classify (or re-route on override) free-text feedback on the current
  // checkpoint. `intentOverride` is the one-tap override chip.
  const submitFeedback = async (intentOverride?: string) => {
    if (!sessionId || busy) return;
    const text = feedbackDraft.trim();
    if (!intentOverride && !text) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch(
        `/projects/${sessionId}/checkpoint/feedback`,
        {
          method: "POST",
          body: JSON.stringify(
            intentOverride ? { intent: intentOverride } : { message: text },
          ),
        },
      );
      const data: FeedbackResult = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error((data as any)?.detail || `HTTP ${resp.status}`);
      setFeedback(data);
      if (!intentOverride) setFeedbackDraft("");
      await refresh(sessionId);
      void loadSessions();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // §4.6 — apply a CONFIRMED checklist diff the change-scope route proposed.
  const confirmScope = async (diff: ScopeDiff) => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch(`/projects/${sessionId}/checklist/scope`, {
        method: "POST",
        body: JSON.stringify({ diff }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      setFeedback(null);
      await refresh(sessionId);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open || typeof document === "undefined") return null;

  const showConfirm =
    !!sessionId &&
    gateState === "aligning" &&
    turns.some(t => t.role !== "user");
  const awaiting = gateState === "awaiting-user" && !!checkpoint;

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-6xl h-[720px] max-h-[94vh] min-h-[440px] overflow-hidden rounded-lg border border-gray-200 bg-white shadow-xl"
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={wbNavLabel('wb-nav-wizard-projects', 'Project Wizard')}
      >
        {/* Conversations rail */}
        <aside className="flex w-52 shrink-0 flex-col border-r border-gray-200 bg-gray-50">
          <div className="flex items-center justify-between px-3 py-3">
            <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
              Projects
            </span>
            <button
              type="button"
              onClick={startNew}
              title="Start a new project"
              className="rounded p-1 text-gray-500 hover:bg-gray-200 hover:text-gray-800"
            >
              <Plus className="h-4 w-4" />
            </button>
          </div>
          <ScrollArea className="flex-1">
            <div className="px-2 pb-3">
              {sessions.length === 0 && (
                <p className="px-2 py-4 text-xs text-gray-400">No projects yet.</p>
              )}
              {sessions.map(s => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => void openSession(s.id)}
                  className={`mb-1 w-full rounded px-2 py-2 text-left text-xs transition-colors ${
                    s.id === sessionId
                      ? "bg-white font-medium text-gray-900 shadow-sm"
                      : "text-gray-600 hover:bg-gray-100"
                  }`}
                >
                  <span className="block truncate">{s.title || "Untitled project"}</span>
                  <span className="mt-0.5 block text-[10px] text-gray-400">
                    {relativeTime(s.last_message_at)}
                  </span>
                </button>
              ))}
            </div>
          </ScrollArea>
        </aside>

        {/* Chat column */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
            <div className="flex items-center gap-2">
              <Compass className="h-4 w-4 text-indigo-600" />
              <h2 className="text-sm font-semibold">Project</h2>
              {gateState && (
                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-500">
                  {gateState === "awaiting-user" ? "waiting for you" : gateState}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </header>

          <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
            {loadingHistory && (
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
              </div>
            )}

            {!loadingHistory && turns.length === 0 && (
              <div className="mx-auto mt-16 max-w-md text-center">
                <Compass className="mx-auto mb-3 h-8 w-8 text-indigo-200" />
                <h3 className="text-sm font-medium text-gray-800">
                  What should MOD build for you?
                </h3>
                <p className="mt-2 text-xs leading-relaxed text-gray-500">
                  Describe the end result in one or two sentences — the output
                  you want and what &quot;done&quot; looks like. MOD proposes a
                  plan first; nothing is built until you confirm it.
                </p>
              </div>
            )}

            {turns.map(t => (
              <div
                key={t.id}
                className={`mb-3 flex ${t.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-xs leading-relaxed ${
                    t.role === "user"
                      ? "bg-indigo-600 text-white"
                      : "bg-gray-100 text-gray-800"
                  }`}
                >
                  {t.body}
                  {t.intent && (
                    <span className="mt-1 block text-[10px] font-medium uppercase tracking-wide opacity-70">
                      {INTENT_LABEL[t.intent] || t.intent}
                    </span>
                  )}
                </div>
              </div>
            ))}

            {/* §5.3 — the checkpoint "Test this" hand-off card */}
            {awaiting && checkpoint && (
              <div className="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-3">
                <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-amber-700">
                  <CircleDot className="h-3.5 w-3.5" /> Test this
                </div>
                {checkpoint.what_to_check && (
                  <p className="text-xs leading-relaxed text-gray-800">
                    Check: {checkpoint.what_to_check}
                  </p>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {checkpoint.artifact_link ? (
                    <a
                      href={checkpoint.artifact_link}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 rounded-md bg-amber-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-700"
                    >
                      <ExternalLink className="h-3.5 w-3.5" /> Open
                    </a>
                  ) : (
                    <span className="text-[11px] text-gray-500">
                      (no openable artifact was attached)
                    </span>
                  )}
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => void approveCheckpoint()}
                  >
                    <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" /> Approve
                  </Button>
                </div>

                {/* feedback box */}
                <div className="mt-3 border-t border-amber-200 pt-2">
                  <div className="flex items-end gap-2">
                    <Input
                      value={feedbackDraft}
                      onChange={e => setFeedbackDraft(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void submitFeedback();
                        }
                      }}
                      placeholder="Reply in plain language — e.g. “works”, “the stack isn't up”, “the UI looks off”…"
                      disabled={busy}
                      className="text-xs"
                    />
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void submitFeedback()}
                      aria-label="Send feedback"
                      disabled={busy || !feedbackDraft.trim()}
                    >
                      <Send className="h-3.5 w-3.5" />
                    </Button>
                  </div>

                  {/* §4.2 — classified intent + one-tap override chips */}
                  {feedback?.classified_intent && (
                    <div className="mt-2 text-[11px] text-gray-600">
                      <span>
                        Read as{" "}
                        <span className="font-semibold text-gray-800">
                          {INTENT_LABEL[feedback.classified_intent] ||
                            feedback.classified_intent}
                        </span>
                        {feedback.intent_source === "override"
                          ? " (your choice)"
                          : ""}
                        . Not right?
                      </span>
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {(feedback.override_options || []).map(opt => (
                          <button
                            key={opt}
                            type="button"
                            disabled={busy || opt === feedback.classified_intent}
                            onClick={() => void submitFeedback(opt)}
                            className={`rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
                              opt === feedback.classified_intent
                                ? "border-indigo-300 bg-indigo-50 font-medium text-indigo-700"
                                : "border-gray-300 text-gray-600 hover:bg-gray-100"
                            }`}
                          >
                            {INTENT_LABEL[opt] || opt}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* §4.6 — change-scope diff card, confirmed before applying */}
                  {feedback?.proposed_diff && feedback.requires_confirm && (
                    <div className="mt-3 rounded-md border border-indigo-200 bg-indigo-50 p-2.5">
                      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-indigo-700">
                        <Wrench className="h-3.5 w-3.5" /> Proposed plan change
                      </div>
                      {feedback.proposed_diff.summary && (
                        <p className="mb-1.5 text-xs text-gray-800">
                          {feedback.proposed_diff.summary}
                        </p>
                      )}
                      <ul className="space-y-0.5 text-[11px]">
                        {(feedback.proposed_diff.adds || []).map((a, i) => (
                          <li key={`a${i}`} className="text-green-700">
                            + Add: {a.title}
                          </li>
                        ))}
                        {(feedback.proposed_diff.reorders || []).map((r, i) => (
                          <li key={`r${i}`} className="text-blue-700">
                            ↕ Reorder: {r.task_key}
                          </li>
                        ))}
                        {(feedback.proposed_diff.removes || []).map((r, i) => (
                          <li key={`d${i}`} className="text-red-700">
                            − Remove: {r}
                          </li>
                        ))}
                      </ul>
                      <div className="mt-2 flex gap-2">
                        <Button
                          size="sm"
                          disabled={busy}
                          onClick={() =>
                            void confirmScope(feedback.proposed_diff as ScopeDiff)
                          }
                        >
                          Confirm change
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => setFeedback(null)}
                        >
                          Dismiss
                        </Button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            {busy && (
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Working…
              </div>
            )}
          </div>

          {error && (
            <div className="border-t border-red-100 bg-red-50 px-4 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          {/* Aligning composer + confirm-to-build */}
          <div className="border-t border-gray-200 p-3">
            {showConfirm && (
              <div className="mb-2 flex items-center gap-2">
                <Button size="sm" disabled={busy} onClick={() => void confirmPlan()}>
                  <Hammer className="mr-1.5 h-3.5 w-3.5" /> Confirm plan &amp; build
                </Button>
                <span className="text-[10px] text-gray-400">
                  Nothing is built until you confirm.
                </span>
              </div>
            )}
            <div className="flex items-end gap-2">
              <Input
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={e => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
                placeholder={
                  awaiting
                    ? "Reply to the hand-off above, or keep chatting…"
                    : "e.g. Convert every uploaded video to 1080p MP4 and keep the captions"
                }
                disabled={busy}
                className="text-xs"
              />
              <Button size="sm" onClick={() => void send()} disabled={busy || !draft.trim()} aria-label="Send message">
                <Send className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        </div>

        {/* §5.1/§5.2 — live checklist pane */}
        <aside className="hidden w-64 shrink-0 flex-col border-l border-gray-200 bg-gray-50 md:flex">
          <div className="flex items-center gap-1.5 border-b border-gray-200 px-3 py-3">
            <Hammer className="h-3.5 w-3.5 text-gray-500" />
            <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
              Checklist
            </span>
          </div>
          <ScrollArea className="flex-1">
            <div className="px-2.5 py-3">
              {checklist.length === 0 ? (
                <p className="px-1 py-4 text-xs leading-relaxed text-gray-400">
                  {gateState === "aligning" || !sessionId
                    ? "The plan turns into a checklist once you confirm."
                    : "No tasks yet."}
                </p>
              ) : (
                checklist.map(task => <ChecklistRow key={task.id} task={task} />)
              )}
            </div>
          </ScrollArea>
        </aside>
      </div>
    </div>,
    document.body,
  );
}

export default ProjectConversationPanel;
