"use client";

import React from "react";
import { ChevronRight, ShieldAlert, ShieldCheck } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

// ---------------------------------------------------------------------------
// Types — mirrors CORE/API/services/egress_report.py's _clean() output shape.
// ---------------------------------------------------------------------------
export interface EgressDeniedEntry {
  host: string;
  port?: number;
  reason?: string;
  count?: number;
}

export interface Egress {
  mode?: string;
  allowed_hosts?: string[];
  denied?: EgressDeniedEntry[];
  denied_truncated?: boolean;
}

interface EgressBadgeProps {
  egress?: Egress | null;
}

// ---------------------------------------------------------------------------
// Mode → display helpers
// ---------------------------------------------------------------------------
const ENFORCED_LABELS: Record<string, string> = {
  proxy: "proxy",
  cilium: "Cilium",
};

const UNENFORCED_MODES = new Set(["unenforced", "advisory"]);

// ---------------------------------------------------------------------------
// EgressBadge — surfaces a worker task's per-task egress report in the node
// execution panel. Absent egress (old worker / no report) renders nothing.
// ---------------------------------------------------------------------------
export default function EgressBadge({ egress }: EgressBadgeProps) {
  if (!egress) return null;

  const mode = egress.mode ?? "";
  const denied = Array.isArray(egress.denied) ? egress.denied : [];
  const isEnforced = mode in ENFORCED_LABELS;
  const isUnenforced = UNENFORCED_MODES.has(mode);

  return (
    <div className="flex flex-col gap-2">
      {/* Enforced: green */}
      {isEnforced && (
        <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-green-50 border border-green-200 text-green-700 text-xs font-medium">
          <ShieldCheck className="h-3.5 w-3.5 shrink-0" />
          <span>Egress: enforced ({ENFORCED_LABELS[mode]})</span>
        </div>
      )}

      {/* Unenforced / advisory: amber + tooltip */}
      {isUnenforced && (
        <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-amber-50 border border-amber-200 text-amber-700 text-xs font-medium cursor-help group relative">
          <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
          <span>Egress not enforced</span>
          <div className="absolute top-full left-0 mt-2 w-64 bg-white border border-gray-200 rounded-md shadow-lg p-3 hidden group-hover:block z-50">
            <p className="text-xs text-gray-600">
              The allowlist is advisory on this worker or cluster; outbound
              connections are not actively blocked.
            </p>
          </div>
        </div>
      )}

      {/* Blocked connections: collapsible list */}
      {denied.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 [&[data-state=open]>svg]:rotate-90">
            <ChevronRight className="h-3.5 w-3.5 transition-transform duration-200" />
            <span>Blocked connections ({denied.length})</span>
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-1 ml-4 border-l pl-3 flex flex-col gap-1">
            {denied.map((d, i) => (
              <div key={`${d.host}-${d.port ?? ""}-${i}`} className="text-xs text-gray-600">
                <span className="font-mono">{d.host}:{d.port ?? "?"}</span>
                {d.reason ? <span className="text-gray-400"> — {d.reason}</span> : null}
                {d.count ? <span className="text-gray-400"> (×{d.count})</span> : null}
              </div>
            ))}
            {egress.denied_truncated && (
              <div className="text-xs italic text-gray-400">+ more</div>
            )}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FX.91 p2b [U] — the PALETTE half: what a node DECLARES it may reach
// ---------------------------------------------------------------------------
// EgressBadge above is the RUNTIME half (one worker task's egress report).
// This is the DECLARATION half, shown before the node is ever dropped on the
// canvas: the manifest's `egress_domains` (p2a) plus this tenant's approval
// state (p2c, carried on the palette payload as `egress_approval` from
// services/node_egress_approval.palette_egress_states) plus the p3a gov
// posture. Same 10px bordered chip vocabulary as CeBadge / LifecycleBadge /
// CostChip so the palette row stays one visual language.
//
//  - declares nothing (and the payload carried the declaration, i.e. the
//    node is default-deny / fail-closed) -> neutral "No network";
//  - declares N domains, approved          -> neutral "Network: N domains"
//    + tooltip listing them;
//  - declares N domains, no decision yet
//    or approval is stale (declaration changed since) -> amber, tooltip
//    says the decision is pending;
//  - the gov/DoD profile blocks a declared domain -> red, tooltip
//    "Blocked by government profile" (a tenant approval does NOT turn this
//    green — the install's allowlist, not the tenant, decides).
export const EGRESS_BADGE_STYLES: Record<string, string> = {
  neutral: "bg-slate-100 text-slate-700 border-slate-300",
  pending: "bg-amber-100 text-amber-800 border-amber-300",
  blocked: "bg-red-100 text-red-700 border-red-300",
};

/** The one approval spelling the UI reads. Absent (pre-p2b payload, or the
 *  enrichment could not run) => null, and the badge makes NO approval claim —
 *  it still shows the declared domains, just without a state colour. */
export function paletteEgressApproval(node: any): any {
  const a = node?.egress_approval;
  return a && typeof a === "object" ? a : null;
}

interface EgressStateBadgeProps {
  /** The node payload (palette NodeDefinition or the panel's live def). */
  node: any;
  className?: string;
}

export const EgressStateBadge: React.FC<EgressStateBadgeProps> = ({ node, className }) => {
  const raw = node?.egress_domains;
  // Absent (pre-p2a payload / unmigrated column) => render NOTHING. The
  // badge never claims "No network" for a node whose declaration it cannot
  // see — silence is honest, a wrong "No network" would be a safety lie.
  if (!Array.isArray(raw)) return null;

  const approval = paletteEgressApproval(node);
  const declared = approval?.declared?.length ? approval.declared : raw;
  const govBlocked = approval?.gov_blocked === true;
  const state = approval?.state;

  if (declared.length === 0) {
    return (
      <span
        className={
          "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
          "rounded-md border whitespace-nowrap " + EGRESS_BADGE_STYLES.neutral +
          " " + (className || "")
        }
        title="Declares no external hosts — its container gets the internal-only, zero-outbound network"
        data-egress-state="none"
        data-egress-domains={undefined}
      >
        No network
      </span>
    );
  }

  const list = declared.join(", ");
  let styleClass = EGRESS_BADGE_STYLES.neutral;
  let tooltip = `Declares ${declared.length} external host(s): ${list}`;
  let badgeState = "approved";

  if (govBlocked) {
    styleClass = EGRESS_BADGE_STYLES.blocked;
    badgeState = "gov_blocked";
    tooltip = `Blocked by government profile: ${approval?.gov_denied?.length ? approval.gov_denied.join(", ") : list} is not on the install's egress allowlist`;
  } else if (state === "required" || state === "stale") {
    styleClass = EGRESS_BADGE_STYLES.pending;
    badgeState = state;
    tooltip = state === "stale"
      ? `Approval is stale — the node now declares ${list}; a tenant admin must re-approve. ${tooltip}`
      : `Needs approval — a tenant admin must approve these hosts before outbound use is granted. ${tooltip}`;
  } else if (state === undefined || state === null) {
    // No approval state on the payload: state the declaration only, and say
    // nothing about whether it was approved.
    badgeState = "unknown";
  }

  return (
    <span
      className={
        "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
        "rounded-md border whitespace-nowrap cursor-help " + styleClass +
        " " + (className || "")
      }
      title={tooltip}
      data-egress-state={badgeState}
      data-egress-domains={list}
    >
      {`Network: ${declared.length} domain${declared.length === 1 ? "" : "s"}`}
    </span>
  );
};
