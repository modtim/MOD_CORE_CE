"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";

/**
 * project-agent §6 — the Project Wizard.
 *
 * One rung above the Workflow Wizard. The Workflow Wizard composes ONE
 * graph; a project holds a whole need across many workflows and nodes and
 * dispatches the wizards below it.
 *
 * Layout is deliberately the shape people already know from a chat
 * product: conversations down the left, one conversation in the middle,
 * a type box at the bottom. A project runs for days or weeks and is
 * picked up and put down — so getting BACK into one has to be as easy as
 * starting one, which means the history rail is not decoration.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Compass, Loader2, Paperclip, Play, Plus, Send, X } from "lucide-react";
import { ProjectConversationPanel } from "./project-conversation-panel";
import { useEntitlements } from "@/hooks/use-entitlements";

const API_URL = apiUrl();

interface ProjectSession {
  id: string;
  kind: string;
  title?: string | null;
  created_at: string;
  last_message_at: string;
}

interface Turn {
  role: "user" | "assistant";
  content: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  accessToken: string | null;
  /** When set, open this session on mount (e.g. a Wizard Queue "Open"
   *  artifact for an agent-project run). Consumed once. */
  initialSessionId?: string | null;
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  // A project parked for weeks is normal here, so days stay readable
  // rather than collapsing into "a while ago".
  return days < 30 ? `${days}d ago` : `${Math.floor(days / 7)}w ago`;
}

export function ProjectWizardPanel({ open, onClose, accessToken, initialSessionId }: Props) {
  const [sessions, setSessions] = useState<ProjectSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [readyToBuild, setReadyToBuild] = useState(false);
  // conversational-project-builder §1.5/§5 — when PROJECT_CONVERSATION_ENABLED
  // is on the platform, the same button opens the man-in-the-middle
  // conversational surface instead of this legacy one-shot flow. `null` = not
  // yet known (probe in flight); until it resolves we render nothing so the
  // legacy view never flashes before the conversational one.
  const [convoEnabled, setConvoEnabled] = useState<boolean | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

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

  // Probe the feature flag once the panel is opened with a token. Fail-closed:
  // any error keeps the legacy view, which is the safe default.
  useEffect(() => {
    if (!open || !accessToken) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await authFetch("/platform/feature-config");
        if (!resp.ok) {
          if (!cancelled) setConvoEnabled(false);
          return;
        }
        const data = await resp.json();
        if (!cancelled) setConvoEnabled(!!data.project_conversation_enabled);
      } catch {
        if (!cancelled) setConvoEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, accessToken, authFetch]);

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
      /* the rail is navigation, not content — a failure here must not
         block someone starting a new project */
    }
  }, [accessToken, authFetch]);

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

  const openSession = async (id: string) => {
    setLoadingHistory(true);
    setError(null);
    try {
      const resp = await authFetch(`/support/sessions/${id}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const detail = await resp.json();
      setSessionId(id);
      setTurns(
        (detail.messages || []).map((m: any) => ({
          role: m.role, content: m.content,
        })),
      );
    } catch (e) {
      setError(`Could not open that project: ${(e as Error).message}`);
    } finally {
      setLoadingHistory(false);
    }
  };

  const startNew = () => {
    setSessionId(null);
    setTurns([]);
    setError(null);
    setDraft("");
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

  const send = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setDraft("");
    setTurns(t => [...t, { role: "user", content: text }]);
    try {
      const resp = await authFetch("/support/project-wizard/turn", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        throw new Error(`HTTP ${resp.status}: ${body.slice(0, 200)}`);
      }
      const data = await resp.json();
      setSessionId(data.session_id);
      setReadyToBuild(Boolean(data.ready_to_build));
      setTurns(t => [...t, { role: "assistant", content: data.reply }]);
      void loadSessions();
    } catch (e) {
      setError((e as Error).message);
      // Put the text back rather than losing it to a failed request.
      setDraft(text);
      setTurns(t => t.slice(0, -1));
    } finally {
      setBusy(false);
    }
  };

  /** The opening ask, made actionable. A description of the input is not
   *  the input — everything the agent builds is graded against these
   *  bytes, so the affordance has to be a real file picker rather than a
   *  prompt to paste something. */
  const attachExample = async (file: File) => {
    if (!sessionId) {
      setError("Describe what you need first, then attach a sample.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const b64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("Could not read that file"));
        reader.onload = () =>
          resolve(String(reader.result || "").split(",")[1] || "");
        reader.readAsDataURL(file);
      });
      const resp = await authFetch("/support/project-wizard/criteria", {
        method: "POST",
        body: JSON.stringify({
          session_id: sessionId,
          input_filename: file.name,
          input_body_b64: b64,
          // The chat so far IS the description of the outcome; the user
          // has already said what they need out of it.
          described_outcome: turns.find(t => t.role === "user")?.content || "",
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
      setReadyToBuild(Boolean(data.ready_to_build));
      setTurns(t => [
        ...t,
        { role: "user", content: `📎 ${file.name}` },
        { role: "assistant", content: data.message },
      ]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const workOnIt = async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await authFetch(
        `/support/project-wizard/${sessionId}/tick`, { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);

      const parts: string[] = [];
      if (data.blocked) parts.push(data.reason || data.blocked);
      if (data.dispatched?.length)
        parts.push(`Started ${data.dispatched.length} build(s).`);
      if (data.escalated?.length)
        parts.push(`Changed approach on ${data.escalated.length} task(s).`);
      if (data.parked?.length)
        parts.push(
          data.parked.map((p: any) => p.question).join("\n\n"));
      if (data.waiting?.length)
        parts.push(`Still working on: ${data.waiting.join(", ")}.`);
      setTurns(t => [...t, {
        role: "assistant",
        content: parts.join("\n\n") || "Nothing to do right now.",
      }]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open || typeof document === "undefined") return null;

  // While the flag probe is in flight, render nothing (avoids flashing the
  // legacy view before switching). When on, hand off to the conversational
  // surface entirely; the legacy body below is untouched and used only when
  // the flag is off.
  if (convoEnabled === null) return null;
  if (convoEnabled) {
    // The deep-link open (initialSessionId) is handled by the
    // conversational surface itself when the flag is on — pass it
    // through so either rendering path lands on the session.
    return (
      <ProjectConversationPanel
        open={open}
        onClose={onClose}
        accessToken={accessToken}
        initialSessionId={initialSessionId}
      />
    );
  }

  return createPortal(
    // Centered, floating modal — matches the Stack Wizard / Marketplace /
    // Triggers dialogs. It used to be a full-height right-side drawer, which
    // read as a different, heavier surface than every other wizard and was
    // flagged as inconsistent in QA. Backdrop click closes; inner clicks are
    // stopped so a drag inside the panel doesn't dismiss it.
    //
    // Portaled to <body> at z-[60] so it stacks ABOVE the persistent MOD
    // Assistant panel (fixed, z-50, itself body-portaled) and the rest of the
    // builder chrome. Rendered inline at z-40 it sat *under* the assistant
    // panel, which bled over the modal and left half of it unreadable
    // (mega-qa). z-[60] mirrors the Marketplace sub-modal; toasts stay above
    // at z-[100].
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-5xl h-[680px] max-h-[92vh] min-h-[420px] overflow-hidden rounded-lg border border-gray-200 bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={wbNavLabel('wb-nav-wizard-projects', 'Project Wizard')}
      >
      {/* Conversations rail. A project is picked up and put down over
          days, so getting back into one is a first-class action. */}
      <aside className="flex w-56 shrink-0 flex-col border-r border-gray-200 bg-gray-50">
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
              <p className="px-2 py-4 text-xs text-gray-400">
                No projects yet.
              </p>
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
                <span className="block truncate">
                  {s.title || "Untitled project"}
                </span>
                <span className="mt-0.5 block text-[10px] text-gray-400">
                  {relativeTime(s.last_message_at)}
                </span>
              </button>
            ))}
          </div>
        </ScrollArea>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <div className="flex items-center gap-2">
            <Compass className="h-4 w-4 text-indigo-600" />
            <h2 className="text-sm font-semibold">{wbNavLabel('wb-nav-wizard-projects', 'Project Wizard')}</h2>
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
                you want and what &quot;done&quot; looks like. MOD turns that
                into the workflows and nodes it needs.
              </p>
              <p className="mt-3 text-[11px] leading-relaxed text-gray-400">
                Type your goal in the box below, then press Send. When you
                attach a sample input file, MOD checks its results against it.
                Press <span className="font-medium text-gray-500">Work on it</span> to
                start the build — it runs in the background.
              </p>
            </div>
          )}

          {turns.map((t, i) => (
            <div
              key={i}
              className={`mb-3 flex ${t.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[80%] whitespace-pre-wrap rounded-lg px-3 py-2 text-xs leading-relaxed ${
                  t.role === "user"
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-100 text-gray-800"
                }`}
              >
                {t.content}
              </div>
            </div>
          ))}

          {busy && (
            <div className="flex items-center gap-2 text-xs text-gray-400">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Thinking…
            </div>
          )}
        </div>

        {error && (
          <div className="border-t border-red-100 bg-red-50 px-4 py-2 text-xs text-red-700">
            {error}
          </div>
        )}

        <div className="border-t border-gray-200 p-3">
          <div className="mb-2 flex items-center gap-2">
            <input
              ref={fileRef}
              type="file"
              className="hidden"
              onChange={e => {
                const f = e.target.files?.[0];
                if (f) void attachExample(f);
              }}
            />
            <Button
              size="sm"
              variant="outline"
              disabled={busy || !sessionId}
              onClick={() => fileRef.current?.click()}
              title={
                sessionId
                  ? "Attach one real input file (optional) — MOD compares its results against it"
                  : "Send your description first, then attach a sample input file"
              }
            >
              <Paperclip className="mr-1.5 h-3.5 w-3.5" />
              Attach sample
            </Button>
            {readyToBuild && (
              <Button size="sm" disabled={busy} onClick={() => void workOnIt()}>
                <Play className="mr-1.5 h-3.5 w-3.5" />
                Work on it
              </Button>
            )}
            {readyToBuild && (
              <span className="text-[10px] text-gray-400">
                You can close this — it keeps going and will notify you.
              </span>
            )}
          </div>
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
              placeholder="e.g. Convert every uploaded video to 1080p MP4 and keep the captions"
              disabled={busy}
              className="text-xs"
            />
            <Button
              size="sm"
              onClick={() => void send()}
              aria-label="Send message"
              disabled={busy || !draft.trim()}
            >
              <Send className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </div>
      </div>
    </div>,
    document.body,
  );
}

export default ProjectWizardPanel;
