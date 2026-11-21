/**
 * GG.UI.6 — map a run's failure `error_message` to a "blocked run" CTA.
 *
 * The API refuses a run that needs a paid node/workflow it doesn't own, with
 * plain-string `detail` raised from `services/entitlement_guard.py`:
 *
 *   402  "Node '<kind>' must be purchased before it can run (id <n>)"  (paywall)
 *   403  "Node '<name or kind>' is a paid marketplace node and
 *         tenant has no active entitlement (id <n>)"                  (SaaS guard)
 *   402  "licence invalid"                                        (on-prem guard)
 *   403  "Workflow listing <id> is a listing that needs an add-on and tenant has
 *         no active entitlement"                                  (GG.1 guard)
 *
 * The run POST returns 202 first; the guard fires inside dispatch, and
 * `start_workflow_execution` records `f"{type(e).__name__}: {e}"` on
 * `WorkflowRun.error_message`, which the run progress endpoint
 * (/road/workflow-runs/{id}/progress) and the NodeRun rows surface.
 * Starlette renders a string detail through `json.dumps`, so the message may
 * arrive double-quoted (also possibly after a `402 Payment Required: `
 * prefix); the parser is tolerant of every observed variant.
 */

export interface BlockedRunReason {
  code: "node_paywall" | "workflow_paid";
  /** Human label for the blocked item ("Node 'X'", "Workflow listing 42"). */
  label: string;
  /** Store /store/{kind}/{id} segment. "node" for node refusals; the
   *  listing id for workflow refusals; null when the kind is unknown. */
  kind: string | null;
  /** Store id. Node refusal messages carry the definition id as a trailing
   *  " (id <n>)" segment (GG.UI.6b) when the API knew it — Buy then links
   *  to /store/node/{id}; null falls back to the /store/node listing. */
  id: number | null;
  /** True only when the payload names a trial as available. The guard
   *  messages never do today, so this stays false and the Try action is
   *  hidden. */
  trialAvailable: boolean;
  /** Fallback message to show when the item can't be named. */
  detail: string;
}

const NAME_RE = /'([^']+)'/;

/** GG.UI.6b — dispatch-time refusal strings now carry the definition id as
 *  a trailing " (id <n>)" segment (API services/entitlement_guard.py and
 *  routers/worker_tasks.py), so failed-run parsing can link the Buy CTA
 *  straight to /store/node/{id}. */
const ID_SUFFIX_RE = /\s*\(id (\d+)\)/;

function named(text: string, re: RegExp): string | null {
  const m = text.match(re);
  return m ? m[1] : null;
}

function idSuffix(text: string): number | null {
  const m = text.match(ID_SUFFIX_RE);
  return m ? parseInt(m[1], 10) : null;
}

/**
 * Parse a run/node failure `error_message` into a blocked-run reason.
 * Returns null for anything that isn't a paid-item entitlement refusal —
 * callers then keep the generic failure display.
 */
export function parseBlockedRunError(msg: string | null | undefined): BlockedRunReason | null {
  if (!msg || typeof msg !== "string") return null;
  const text = msg.trim();

  // 402 paywall: "Node '<kind>' must be purchased before it can run"
  if (/must be purchased before it can run/i.test(text)) {
    const name = named(text, /Node\s+'([^']+)'\s+must be purchased/i)
      ?? named(text, /Node\s+"([^"]+)"\s+must be purchased/i)
      ?? named(text, /Node\s+(\S+)\s+must be purchased/i);
    return {
      code: "node_paywall",
      label: name ? `Node '${name}'` : "A node",
      kind: "node",
      id: idSuffix(text),
      trialAvailable: /trial/i.test(text),
      detail: text,
    };
  }

  // 403 GG.1: "Workflow listing <id> is a listing that needs an add-on and tenant has no
  // active entitlement"
  const wl = text.match(/Workflow listing (\d+)/);
  if (wl && /(?:paid listing|listing that needs an add-on)/i.test(text)) {
    return {
      code: "workflow_paid",
      label: `Workflow listing ${wl[1]}`,
      kind: "workflow",
      id: parseInt(wl[1], 10),
      trialAvailable: /trial/i.test(text),
      detail: text,
    };
  }

  // 403 SaaS marketplace guard: "Node '<name or kind>' is a paid marketplace
  // node and tenant has no active entitlement"
  if (/paid marketplace node/i.test(text)) {
    const name = named(text, /Node\s+'([^']+)'\s+is a paid marketplace node/i)
      ?? named(text, /Node\s+"([^"]+)"\s+is a paid marketplace node/i)
      ?? named(text, /Node\s+(\S+)\s+is a paid marketplace node/i);
    return {
      code: "node_paywall",
      label: name ? `Node '${name}'` : "A node",
      kind: "node",
      id: idSuffix(text),
      trialAvailable: /trial/i.test(text),
      detail: text,
    };
  }

  return null;
}

/**
 * GG.UI.6b — map the structured 402 `detail.items` returned by
 * `POST /workflow-versions/{id}/run` when the entitlement pre-flight finds
 * paid items the tenant doesn't own (code "purchase_required") into blocked-run
 * reasons. Node items carry the definition id, so Buy links straight to
 * /store/node/{id}; a missing id falls back to the bare node listing.
 */
export interface PreflightItem {
  kind: string;
  id: number | null;
  name?: string | null;
  node_kind?: string | null;
  trial_available?: boolean;
}

export function mapPreflightItems(items: PreflightItem[] | null | undefined): BlockedRunReason[] {
  if (!Array.isArray(items)) return [];
  const out: BlockedRunReason[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const isNode = item.kind === "node";
    const id = typeof item.id === "number" ? item.id : null;
    const label = isNode
      ? (item.name ? `Node '${item.name}'` : "A node")
      : (id != null ? `Workflow listing ${id}` : "A workflow");
    out.push({
      code: isNode ? "node_paywall" : "workflow_paid",
      label,
      kind: item.kind && typeof item.kind === "string" ? item.kind : null,
      id,
      trialAvailable: Boolean(item.trial_available),
      detail: label,
    });
  }
  return out;
}

/**
 * Store link for a blocked item. Node refusals carry the definition id as a
 * trailing " (id <n>)" segment when the API knew it (GG.UI.6b) and link
 * straight to /store/node/{id}; legacy messages without the suffix fall back
 * to the node listing. Workflow refusals carry the listing id and link
 * straight to the detail page.
 */
export function blockedRunStoreHref(reason: BlockedRunReason): string {
  if (reason.kind && reason.id != null) return `/store/${reason.kind}/${reason.id}`;
  if (reason.kind) return `/store/${reason.kind}`;
  return "/store/node";
}
