"use client";

/**
 * Notifications bell for the WorkflowBuilder sidebar (P8a + P8f).
 *
 * Polls `/notifications/unread-count` every 30s and layers an SSE
 * subscription on `/notifications/stream` for instant pushes
 * (EventSource falls through to poll on disconnect). Opens a
 * redesigned popover (P8f) with:
 *   - type icons per row (workflow / node / billing)
 *   - filter chips at the top (All / Unread / Workflows / Nodes /
 *     Billing / Archived), chip state persists in localStorage
 *   - relative timestamps (hover for absolute)
 *   - per-row actions: mark-read, resume (for wizard-gen rows),
 *     archive
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { authenticatedFetch, mintStreamTicket } from "@/lib/api-client";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import {
  buildQueueStorageKey,
  deriveBuildQueueUserId,
} from "@/lib/build-queue-namespace";
import {
  Bell,
  CheckCircle2,
  AlertTriangle,
  Box,
  DollarSign,
  Archive,
  Loader2,
  ClipboardCheck,
} from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";

interface Notification {
  id: number;
  message: string;
  is_read: boolean;
  is_archived?: boolean;
  notification_type: string | null;
  link_url: string | null;
  created_at: string;
}

interface Props {
  accessToken: string | null;
  apiUrl: string;
  onResumeGeneration?: (taskId: number) => void;
  /** Open the build-queue panel in the parent. Called when a node-creation
   *  notification has an existing Phase-2 build row — we want to land the
   *  user on the row's logs, not reopen the wizard. */
  onOpenQueue?: () => void;
}

// Build-queue rows are persisted under a per-user localStorage key
// (`mod.buildQueue.v1.user.<sub>`) — see lib/build-queue-namespace.ts
// and workflow-builder.tsx. The key is derived from the JWT `sub` so
// each authenticated user gets their own queue and notification
// routing doesn't leak rows across browser users. Reading the legacy
// global key (the previous shape, before LDF-3 §5) made
// findBuildRowForNotification miss every row for authenticated users
// — auto-mode alerts then fell through to wizard-resume and the user
// could re-dispatch Phase 2 by mistake.

// 1-hour TTL on the Confirm-and-Build session log (issue #10).
// Notifications older than this fall back to the legacy wizard-resume
// path, which is the right call for an old build the user has long
// since walked away from.
const DISPATCHED_PHASE2_TTL_MS = 60 * 60 * 1000;

/** Did the user click Confirm and Build for this generation task
 *  recently? If yes, a stale "Node artifacts ready" notification for
 *  the same task should route to the build-queue panel rather than
 *  reopen the wizard at Preview (where the user would be asked to
 *  re-confirm a build they've already dispatched).
 */
function checkDispatchedPhase2(generationTaskId: number): { buildTaskId: number; nodeName?: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(`mod.wizard.dispatchedPhase2.${generationTaskId}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const ts = Number(parsed.ts || 0);
    if (Date.now() - ts > DISPATCHED_PHASE2_TTL_MS) {
      // Expired — clean up so the key doesn't accumulate forever.
      window.localStorage.removeItem(`mod.wizard.dispatchedPhase2.${generationTaskId}`);
      return null;
    }
    const buildTaskId = Number(parsed.buildTaskId || 0);
    if (!Number.isFinite(buildTaskId) || buildTaskId <= 0) return null;
    return { buildTaskId, nodeName: parsed.nodeName };
  } catch {
    return null;
  }
}

interface PersistedQueueEntry {
  taskId: number;
  nodeName?: string;
  phase?: "generation" | "build" | "build_only";
  outcome?: "success" | "failed";
  /** Preserved by applyTransitionToBuild when a Phase 1 row morphs
   *  into Phase 2 (or by addBuild on the singleStep path). Lets the
   *  bell match a notification keyed on the original Containerizer
   *  task id to its morphed build row. */
  generationTaskId?: number;
}

/** Read the persisted build queue from localStorage. Returns []
 *  on parse failure or when the key is absent. The caller passes the
 *  per-user storage key derived from the access token. */
function readPersistedQueue(storageKey: string): PersistedQueueEntry[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** Find the most-recent Phase-2 ("build") row whose nodeName matches
 *  the notification's title or matches the resumeTaskId on a sibling
 *  Phase-1 row. Returns the build row's taskId if found.
 *
 *  Heuristic:
 *    1. Find a `generation` row with taskId === resumeTaskId.
 *    2. Find a sibling `build` row whose nodeName matches the gen row.
 *  If no resumeTaskId, fall back to finding any `build` row whose
 *  nodeName matches `notification.title`.
 */
export function findBuildRowForNotification(
  resumeTaskId: number | null,
  notificationFallback: string,
  storageKey: string,
): number | null {
  const entries = readPersistedQueue(storageKey);
  if (entries.length === 0) return null;

  // Direct hit: a build/build_only row that was morphed in place from
  // the Phase 1 row carries the original generation task id as
  // `generationTaskId`. Match it first — this is the reliable path
  // even when the user has multiple builds with the same nodeName,
  // because the gen taskId is unique per Containerizer run.
  if (resumeTaskId !== null) {
    for (let i = entries.length - 1; i >= 0; i--) {
      const e = entries[i];
      if ((e.phase === "build" || e.phase === "build_only") &&
          e.generationTaskId === resumeTaskId) {
        return e.taskId;
      }
    }
  }

  // Resolve the gen row's nodeName to match against build rows.
  let nodeName: string | undefined;
  if (resumeTaskId !== null) {
    const gen = entries.find(
      (e) => e.taskId === resumeTaskId && e.phase === "generation",
    );
    if (gen) nodeName = gen.nodeName;
  }
  if (!nodeName) nodeName = notificationFallback;
  if (!nodeName) return null;

  // Most recent build row with the same nodeName. Match BOTH "build"
  // (Step 2 of 2 in the two-phase image flow) AND "build_only" (the
  // single-step repo flow) — both are the "real" build row the user
  // wants to land on. Fallback after the generationTaskId hit above.
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i];
    if ((e.phase === "build" || e.phase === "build_only") && e.nodeName === nodeName) {
      return e.taskId;
    }
  }
  return null;
}

const POLL_INTERVAL_MS = 30_000;
const FILTER_STORAGE_KEY = "mod.notifBellFilter.v1";


/** Pure decision of where a notification click should route.
 *
 *  Extracted so the routing contract can be unit-tested without
 *  rendering the bell.
 *
 *  Routing precedence:
 *    1. `expand-queue-row` — localStorage knows the exact build row;
 *       open the queue panel and expand that row.
 *    2. `open-queue` — Tim's regression: a `node_generation_complete`
 *       notification whose build row isn't in localStorage (fresh
 *       browser, cleared cache, cross-tab race). Always land on the
 *       queue panel; opening the wizard at Preview is wrong because
 *       the system is already past that point and a re-confirm would
 *       re-dispatch.
 *    3. `wizard-resume` — non-success notifications (notably
 *       `node_generation_failed`) where the user genuinely wants to
 *       inspect what failed at the review step.
 *    4. `link-url-hash` — final fallback.
 */
export type NotificationRoute =
  | { kind: "expand-queue-row"; taskId: number }
  | { kind: "open-queue" }
  | { kind: "wizard-resume"; taskId: number }
  | { kind: "link-url-hash"; hash: string }
  | { kind: "noop" };


export function decideNotificationRoute(args: {
  notificationType: string | null;
  linkUrl: string | null;
  resumeId: number | null;
  buildRowTaskId: number | null;
  dispatchedPhase2BuildTaskId: number | null;
}): NotificationRoute {
  const { notificationType, linkUrl, resumeId,
          buildRowTaskId, dispatchedPhase2BuildTaskId } = args;

  if (buildRowTaskId !== null) {
    return { kind: "expand-queue-row", taskId: buildRowTaskId };
  }
  if (dispatchedPhase2BuildTaskId !== null) {
    return { kind: "expand-queue-row", taskId: dispatchedPhase2BuildTaskId };
  }
  // Phase-1-success notifications never re-open the wizard. See
  // the comment block on `decideNotificationRoute` for why.
  if (notificationType === "node_generation_complete") {
    return { kind: "open-queue" };
  }
  if (resumeId !== null) {
    return { kind: "wizard-resume", taskId: resumeId };
  }
  if (linkUrl && linkUrl.startsWith("#")) {
    return { kind: "link-url-hash", hash: linkUrl.slice(1) };
  }
  return { kind: "noop" };
}

type Filter = "all" | "unread" | "workflows" | "nodes" | "billing" | "archived";

const FILTER_CHIPS: Array<{ key: Filter; label: string }> = [
  { key: "all",       label: "All" },
  { key: "unread",    label: "Unread" },
  { key: "workflows", label: "Workflows" },
  { key: "nodes",     label: "Nodes" },
  { key: "archived",  label: "Archived" },
];

function typeIconFor(notification_type: string | null) {
  const t = (notification_type || "").toLowerCase();
  if (t.startsWith("workflow_complete"))       return { Icon: CheckCircle2, cls: "text-emerald-600" };
  if (t.startsWith("workflow_failed"))         return { Icon: AlertTriangle, cls: "text-rose-600" };
  if (t.startsWith("node_generation_complete")) return { Icon: Box, cls: "text-indigo-600" };
  if (t.startsWith("node_generation_failed"))   return { Icon: Box, cls: "text-rose-600" };
  if (t.startsWith("node_"))                    return { Icon: Box, cls: "text-indigo-500" };
  // conversational-project-builder §6.1 — a project checkpoint awaiting the
  // user (project_waiting_for_user) reads as "there's something for you to
  // test", so it gets the clipboard-check glyph, not the generic bell.
  if (t.startsWith("project_"))                 return { Icon: ClipboardCheck, cls: "text-sky-600" };
  if (t.startsWith("payment_") || t.startsWith("invoice_") || t.startsWith("billing_") || t.startsWith("overage"))
    return { Icon: DollarSign, cls: "text-amber-600" };
  return { Icon: Bell, cls: "text-gray-500" };
}

function extractResumeTaskId(link_url: string | null): number | null {
  if (!link_url) return null;
  const m = link_url.match(/resume=(\d+)/);
  return m ? parseInt(m[1], 10) : null;
}

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export default function NotificationsBell({ accessToken, apiUrl, onResumeGeneration, onOpenQueue }: Props) {
  const [unread, setUnread] = useState<number>(0);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Notification[]>([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Filter>(() => {
    if (typeof window === "undefined") return "all";
    try {
      return ((window.localStorage.getItem(FILTER_STORAGE_KEY) as Filter) || "all");
    } catch {
      return "all";
    }
  });

  // Per-user storage key for the build queue. Must match what
  // workflow-builder.tsx writes (also derived from the JWT `sub`),
  // otherwise findBuildRowForNotification reads from an empty namespace
  // and Phase-1-done alerts mis-route to wizard-resume instead of the
  // queue panel.
  const buildsStorageKey = useMemo(
    () => buildQueueStorageKey(deriveBuildQueueUserId(accessToken)),
    [accessToken],
  );

  // Persist chip state across reopens / tabs.
  useEffect(() => {
    try { window.localStorage.setItem(FILTER_STORAGE_KEY, filter); } catch { /* ignore */ }
  }, [filter]);

  // 30s poll for unread count. Resilient baseline; SSE pushes below.
  // authenticatedFetch silently refreshes an expired token; raw fetch would
  // 401-storm every interval forever. Throw on failure so the shared poller
  // backs off (and pauses when the tab is hidden) instead of hammering.
  const fetchUnread = useCallback(async () => {
    if (!accessToken) return;
    const resp = await authenticatedFetch(`${apiUrl}/notifications/unread-count`);
    const err = pollErrorForResponse(resp);
    if (err) throw err;
    const data = await resp.json();
    setUnread(Number(data.unread_count) || 0);
  }, [accessToken, apiUrl]);
  usePollingBackoff(
    fetchUnread,
    { baseMs: POLL_INTERVAL_MS, enabled: !!accessToken },
    [accessToken, apiUrl],
  );

  // Build /list query string from the active filter chip.
  const listQuery = useMemo(() => {
    const params = new URLSearchParams({ limit: "30" });
    if (filter === "unread") params.set("unread_only", "true");
    if (filter === "workflows") params.set("category", "workflows");
    if (filter === "nodes")     params.set("category", "nodes");
    if (filter === "billing")   params.set("category", "billing");
    if (filter === "archived")  params.set("include_archived", "true");
    return params.toString();
  }, [filter]);

  const fetchList = useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const resp = await authenticatedFetch(`${apiUrl}/notifications/list?${listQuery}`);
      if (!resp.ok) return;
      const data = await resp.json();
      let list: Notification[] = Array.isArray(data.notifications) ? data.notifications : [];
      // When the user explicitly picked the Archived chip, keep only
      // archived rows — the server returns include_archived=true but
      // that means "archived AND non-archived". We want archived-only.
      if (filter === "archived") {
        list = list.filter((n) => n.is_archived);
      }
      setItems(list);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }, [accessToken, apiUrl, listQuery, filter]);

  useEffect(() => {
    if (open) fetchList();
  }, [open, fetchList]);

  // SSE push (P8e) — instant wake-ups from the server. WS5.34b: auth is a
  // short-lived single-use `?ticket=` (minted via POST /stream-tickets)
  // rather than the full JWT, so a fresh ticket must be minted before EACH
  // (re)connect — the browser's own auto-retry would otherwise replay a
  // dead ticket into a 401 loop. Reconnects are done manually here on an
  // exponential backoff (capped) so a flaky connection can't turn into a
  // reconnect storm; the 30s poll (usePollingBackoff above) is the floor
  // either way.
  useEffect(() => {
    if (!accessToken) return;
    if (typeof window === "undefined" || typeof EventSource === "undefined") return;
    let es: EventSource | null = null;
    let closed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let backoffMs = 2000;
    const MAX_BACKOFF_MS = 30_000;

    const refresh = async () => {
      try {
        const resp = await authenticatedFetch(`${apiUrl}/notifications/unread-count`);
        if (!resp.ok) return;
        const data = await resp.json();
        setUnread(Number(data.unread_count) || 0);
      } catch { /* ignore */ }
      if (open) fetchList();
    };

    const scheduleReconnect = () => {
      if (closed) return;
      retryTimer = setTimeout(connect, backoffMs);
      backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
    };

    const connect = async () => {
      if (closed) return;
      const ticket = await mintStreamTicket(apiUrl, "notifications");
      if (closed) return;
      if (!ticket) {
        scheduleReconnect();
        return;
      }
      const url = `${apiUrl}/notifications/stream?ticket=${encodeURIComponent(ticket)}`;
      try {
        es = new EventSource(url, { withCredentials: false });
      } catch {
        scheduleReconnect();
        return;
      }
      es.addEventListener("open", () => { backoffMs = 2000; });
      es.addEventListener("message", () => { if (!closed) refresh(); });
      es.addEventListener("error", () => {
        try { es?.close(); } catch { /* ignore */ }
        es = null;
        scheduleReconnect();
      });
    };

    connect();

    return () => {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      try { es?.close(); } catch { /* ignore */ }
    };
  }, [accessToken, apiUrl, open, fetchList]);

  const handleRowClick = useCallback(
    async (n: Notification) => {
      if (!n.is_read) {
        try {
          await authenticatedFetch(`${apiUrl}/notifications/${n.id}/mark-read`, {
            method: "PUT",
          });
          setUnread((u) => Math.max(0, u - 1));
          setItems((prev) => prev.map((x) => x.id === n.id ? { ...x, is_read: true } : x));
        } catch { /* ignore */ }
      }
      const resumeId = extractResumeTaskId(n.link_url);
      // The Notification model carries `message` (not `title`) — use
      // it as the fallback nodeName matcher when the link_url has no
      // resume= hint to anchor from.
      const buildRowTaskId = findBuildRowForNotification(
        resumeId, n.message || "", buildsStorageKey,
      );
      const dispatchedPhase2BuildTaskId = resumeId !== null
        ? (checkDispatchedPhase2(resumeId)?.buildTaskId ?? null)
        : null;

      const route = decideNotificationRoute({
        notificationType: n.notification_type,
        linkUrl: n.link_url,
        resumeId,
        buildRowTaskId,
        dispatchedPhase2BuildTaskId,
      });
      console.debug(
        `notifications-bell: route=${route.kind} for notification ${n.id}`,
        route,
      );

      switch (route.kind) {
        case "expand-queue-row":
          onOpenQueue?.();
          if (typeof window !== "undefined") {
            window.dispatchEvent(
              new CustomEvent("mod:queue:expand-row", { detail: { taskId: route.taskId } }),
            );
          }
          setOpen(false);
          return;
        case "open-queue":
          onOpenQueue?.();
          setOpen(false);
          return;
        case "wizard-resume":
          if (onResumeGeneration) {
            onResumeGeneration(route.taskId);
            setOpen(false);
            return;
          }
          // No handler — fall through to link-url-hash if available.
          if (n.link_url && n.link_url.startsWith("#")) {
            window.location.hash = n.link_url.slice(1);
          }
          return;
        case "link-url-hash":
          window.location.hash = route.hash;
          return;
        case "noop":
          return;
      }
    },
    [accessToken, apiUrl, onResumeGeneration, onOpenQueue, buildsStorageKey],
  );

  const handleArchive = useCallback(
    async (id: number, e: React.MouseEvent) => {
      e.stopPropagation();
      if (!accessToken) return;
      try {
        await authenticatedFetch(`${apiUrl}/notifications/${id}/archive`, {
          method: "PUT",
        });
        // Remove from current view unless the user is already filtered
        // to the archived chip.
        if (filter !== "archived") {
          setItems((prev) => prev.filter((x) => x.id !== id));
        } else {
          setItems((prev) => prev.map((x) => x.id === id ? { ...x, is_archived: true } : x));
        }
        // If the row was unread, archiving implicitly marked it read.
        setUnread((u) => Math.max(0, u));
      } catch { /* ignore */ }
    },
    [accessToken, apiUrl, filter],
  );

  const handleMarkAllRead = useCallback(async () => {
    if (!accessToken) return;
    try {
      await authenticatedFetch(`${apiUrl}/notifications/mark-all-read`, {
        method: "PUT",
      });
      setUnread(0);
      setItems((prev) => prev.map((x) => ({ ...x, is_read: true })));
    } catch { /* ignore */ }
  }, [accessToken, apiUrl]);

  // Build the same filter params /list uses, for the bulk-action POSTs.
  // Mirrors listQuery above so the bulk action's scope is exactly the
  // chip the user is looking at.
  const buildBulkParams = useCallback(() => {
    const params = new URLSearchParams();
    if (filter === "unread")    params.set("unread_only", "true");
    if (filter === "workflows") params.set("category", "workflows");
    if (filter === "nodes")     params.set("category", "nodes");
    if (filter === "billing")   params.set("category", "billing");
    if (filter === "archived")  params.set("include_archived", "true");
    return params.toString();
  }, [filter]);

  const handleArchiveAll = useCallback(async () => {
    if (!accessToken || items.length === 0) return;
    try {
      const qs = buildBulkParams();
      await authenticatedFetch(
        `${apiUrl}/notifications/archive-all${qs ? `?${qs}` : ""}`,
        { method: "POST" },
      );
      // Optimistic local update — clear the visible list (the server
      // archived everything matching the current filter). Then refetch
      // so the count matches reality.
      setItems([]);
      setUnread(0);
      fetchList();
    } catch { /* ignore — the refetch will reconcile state */ }
  }, [accessToken, apiUrl, items.length, buildBulkParams, fetchList]);

  const handleDeleteAll = useCallback(async () => {
    if (!accessToken || items.length === 0) return;
    // Destructive operation — require explicit confirmation. window.prompt
    // demanding the literal string "delete" matches the contract the
    // Dashboard's bulk-delete already uses (see API docstring).
    const confirmation = window.prompt(
      `This will permanently delete ${items.length} notification(s) ` +
      `matching the current filter. Type "delete" to confirm.`
    );
    if (confirmation !== "delete") return;
    try {
      const qs = buildBulkParams();
      await authenticatedFetch(
        `${apiUrl}/notifications/delete-all${qs ? `?${qs}` : ""}`,
        { method: "POST" },
      );
      setItems([]);
      setUnread(0);
      fetchList();
    } catch { /* ignore — the refetch will reconcile state */ }
  }, [accessToken, apiUrl, items.length, buildBulkParams, fetchList]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="relative p-2 text-gray-600 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors"
          title="Notifications"
          aria-label="Notifications"
        >
          <Bell className="w-6 h-6" />
          {/* FX.68 p2b — axe WCAG2.aa colour-contrast: the unread badge was
              bg-red-500 #ef4444 + white text = 3.76:1 (needs >= 4.5).
              bg-red-600 #dc2626 + white = 4.83:1 (computed, WCAG relative
              luminance). app/globals.css overrides neither bg-red-600 nor
              text-white in .dark, so the same pair holds in both themes. */}
          {unread > 0 && (
            <span className="absolute top-1 right-1 inline-flex items-center justify-center text-[10px] font-bold rounded-full bg-red-600 text-white h-5 w-5">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[400px] p-0">
        <div className="flex items-center justify-between px-3 py-2 border-b">
          <span className="text-sm font-semibold">Notifications</span>
          <div className="flex items-center gap-3">
            {unread > 0 && (
              <button
                onClick={handleMarkAllRead}
                className="text-[11px] text-gray-500 hover:text-gray-800"
              >
                Mark all read
              </button>
            )}
            <button
              onClick={handleArchiveAll}
              disabled={items.length === 0}
              className="text-[11px] text-gray-500 hover:text-gray-800 disabled:opacity-40 disabled:cursor-not-allowed"
              title="Archive every notification matching the current filter"
            >
              Archive all
            </button>
            <button
              onClick={handleDeleteAll}
              disabled={items.length === 0}
              className="text-[11px] text-rose-600 hover:text-rose-800 disabled:opacity-40 disabled:cursor-not-allowed"
              title="Permanently delete every notification matching the current filter"
            >
              Delete all
            </button>
          </div>
        </div>
        {/* Filter chips */}
        <div className="flex flex-wrap gap-1 px-3 py-2 border-b bg-slate-50">
          {FILTER_CHIPS.map((chip) => (
            <button
              key={chip.key}
              onClick={() => setFilter(chip.key)}
              className={`text-[11px] px-2 py-0.5 rounded-full border transition-colors ${
                filter === chip.key
                  ? "bg-indigo-600 text-white border-indigo-600"
                  : "bg-white text-gray-600 border-gray-200 hover:border-gray-300"
              }`}
            >
              {chip.label}
            </button>
          ))}
        </div>
        <ScrollArea className="max-h-[380px]">
          {loading && (
            <div className="flex items-center justify-center py-8 text-gray-400">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          )}
          {!loading && items.length === 0 && (
            <div className="py-8 text-center text-xs text-gray-400">
              {filter === "archived" ? "Nothing archived yet." : "No notifications."}
            </div>
          )}
          {!loading && items.map((n) => {
            const { Icon, cls } = typeIconFor(n.notification_type);
            // Unread rows get three distinctions so the inside of the
            // popover matches the outer red unread badge: a left-edge
            // accent dot, a non-default background tint, and bolder
            // title weight. Read rows keep the previous dim so they
            // visually sink under the unread queue.
            const unread = !n.is_read;
            return (
              <div
                key={n.id}
                className={`group flex gap-2 px-3 py-2 border-b last:border-b-0 hover:bg-gray-50 ${
                  unread ? "bg-indigo-50/60" : "opacity-70"
                }`}
              >
                <Icon className={`w-4 h-4 ${cls} flex-shrink-0 mt-0.5`} />
                <button
                  onClick={() => handleRowClick(n)}
                  className="flex-1 min-w-0 text-left"
                >
                  <div
                    className={`text-xs line-clamp-2 ${
                      unread
                        ? "text-gray-900 font-semibold"
                        : "text-gray-700 font-normal"
                    }`}
                  >
                    {unread && (
                      <span
                        aria-label="unread"
                        className="inline-block w-1.5 h-1.5 mr-1.5 rounded-full bg-indigo-500 align-middle"
                      />
                    )}
                    {n.message}
                  </div>
                  <div className="text-[10px] text-gray-400 mt-0.5" title={n.created_at}>
                    {relativeTime(n.created_at)}
                  </div>
                </button>
                {!n.is_archived && (
                  <button
                    onClick={(e) => handleArchive(n.id, e)}
                    className="opacity-0 group-hover:opacity-100 transition-opacity text-gray-400 hover:text-gray-700 flex-shrink-0"
                    title="Archive"
                  >
                    <Archive className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            );
          })}
        </ScrollArea>
      </PopoverContent>
    </Popover>
  );
}
