"use client";

/**
 * LDF-3 §3 — Canvas node info chips.
 *
 * Two chips that appear on every canvas node card and inside the
 * Node Library hover tooltip + the right-rail node-details panel.
 *
 *  - <CostChip>: credit cost. `<n> credit` for n=1, `<n> credits`
 *    for any other n, including 0.
 *  - <ProcessingChip>: derived from `gpu_variants` keys —
 *      empty / `{cpu}` only      → "CPU"
 *      single accelerator key    → variant display name (CUDA / ROCm / …)
 *      2+ accelerator keys       → "Multi" with hover tooltip listing variants
 *
 * The chip pair refreshes on next render when the underlying
 * NodeDefinition changes (no per-instance cached copy).
 */

import React from "react";

export const VARIANT_DISPLAY_NAMES: Record<string, string> = {
    cpu: "CPU",
    cuda: "CUDA",
    rocm: "ROCm",
    metal: "Metal",
    npu: "NPU",
    intel: "Intel",
    vulkan: "Vulkan",
};

const ACCELERATOR_KEYS = new Set([
    "cuda", "rocm", "metal", "npu", "intel", "vulkan",
]);

function variantLabel(key: string): string {
    return VARIANT_DISPLAY_NAMES[key.toLowerCase()] ?? key.toUpperCase();
}

/** tcp 8.1 — server `cost_credits_by_tier` payload. Any shape is accepted
 *  because the field is optional on old nodes and may arrive half-filled
 *  from a wizard mid-calibration. */
export interface CostTiers {
    small: number;
    medium: number;
    large: number;
}

/** null unless all three tiers are present, finite and non-negative. */
export function normalizeCostTiers(value: unknown): CostTiers | null {
    if (!value || typeof value !== "object") return null;
    const t = value as Record<string, unknown>;
    const small = t.small, medium = t.medium, large = t.large;
    const nums = [small, medium, large];
    if (nums.some(v => typeof v !== "number" || !Number.isFinite(v) || v < 0)) return null;
    return { small: small as number, medium: medium as number, large: large as number };
}

interface CostChipProps {
    cost: number | null | undefined;
    /** tcp 8.1 — three-tier pricing. When present and valid the chip shows
     *  "1 / 3 / 12 credits" (small / medium / large input) instead of the
     *  single legacy number; absent or malformed → legacy `cost` unchanged. */
    costTiers?: CostTiers | Record<string, unknown> | null;
    /** Optional className for tighter / wider variants on the canvas
     *  vs the right rail. */
    className?: string;
}

export const CostChip: React.FC<CostChipProps> = ({ cost, costTiers, className }) => {
    const tiers = normalizeCostTiers(costTiers);

    let label: string | null = null;
    let tooltip = "";

    if (tiers) {
        if (tiers.small === 0 && tiers.medium === 0 && tiers.large === 0) {
            // Free node — keep the legacy free rendering exactly.
            label = "0 credits";
            tooltip = "Costs 0 credits per run";
        } else {
            label = `${tiers.small} / ${tiers.medium} / ${tiers.large} credits`;
            tooltip = "small / medium / large input";
        }
    } else if (cost !== null && cost !== undefined) {
        const n = Math.max(0, Math.floor(cost));
        label = n === 1 ? "1 credit" : `${n} credits`;
        tooltip = `Costs ${label} per run`;
    }

    if (label === null) return null;
    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md bg-amber-50 text-amber-700 border border-amber-200 " +
                "whitespace-nowrap " + (className || "")
            }
            title={tooltip}
        >
            {label}
        </span>
    );
};

interface ProcessingChipProps {
    gpuVariants: Record<string, unknown> | null | undefined;
    className?: string;
}

export const ProcessingChip: React.FC<ProcessingChipProps> = ({ gpuVariants, className }) => {
    const keys = gpuVariants && typeof gpuVariants === "object"
        ? Object.keys(gpuVariants).map(k => k.toLowerCase())
        : [];

    let label: string;
    let tooltip: string;
    const accelerators = keys.filter(k => ACCELERATOR_KEYS.has(k));

    if (accelerators.length === 0) {
        // Empty or cpu-only → CPU
        label = "CPU";
        tooltip = keys.length === 0
            ? "Runs on CPU"
            : `Runs on ${keys.map(variantLabel).join(", ")}`;
    } else if (accelerators.length === 1 && keys.length <= 1) {
        label = variantLabel(accelerators[0]);
        tooltip = `Runs on ${label}`;
    } else if (accelerators.length === 1 && keys.length > 1) {
        // Single accelerator + cpu fallback → still show the accelerator label
        label = variantLabel(accelerators[0]);
        tooltip = `Runs on ${keys.map(variantLabel).join(", ")}`;
    } else {
        label = "Multi";
        tooltip = `Runs on ${keys.map(variantLabel).join(", ")}`;
    }

    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md bg-sky-50 text-sky-700 border border-sky-200 " +
                "whitespace-nowrap " + (className || "")
            }
            title={tooltip}
        >
            {label}
        </span>
    );
};

interface NodeChipsProps {
    cost: number | null | undefined;
    /** tcp 8.1 — three-tier pricing; forwarded to <CostChip>. Absent →
     *  legacy single `cost` rendering, so existing call sites are untouched. */
    costTiers?: CostTiers | Record<string, unknown> | null;
    gpuVariants: Record<string, unknown> | null | undefined;
    className?: string;
}

/** Both chips together. Use on the canvas node card (positioned
 *  bottom-right via absolute container) and in the Library hover
 *  tooltip + right-rail Pricing section (inline). */
export const NodeChips: React.FC<NodeChipsProps> = ({ cost, costTiers, gpuVariants, className }) => (
    <span className={"inline-flex items-center gap-1 " + (className || "")}>
        <CostChip cost={cost} costTiers={costTiers} />
        <ProcessingChip gpuVariants={gpuVariants} />
    </span>
);

/**
 * GO.9 — honest per-accelerator labels.
 *
 * One badge per accelerator variant a node reports, colored by its
 * server-derived status (utils.queue_selector.compute_variant_status):
 *   verified        → green  — smoke-tested, routing prefers it
 *   unverified      → gray   — not yet smoke-tested, tried once optimistically
 *   incompatible    → red    — known broken build/port, never routed to
 *   not_recommended → amber  — builds+runs but flagged slow/divergent,
 *                               only routed to as a last resort
 * Shows the perf ratio vs the CUDA reference when known.
 */
export type VariantStatusEntry = {
    status: string;
    perf_ratio?: number | null;
};

export type VariantStatusMap = Record<string, VariantStatusEntry>;

export const VARIANT_STATUS_LABELS: Record<string, string> = {
    verified: "Verified",
    unverified: "Unverified",
    incompatible: "Incompatible",
    not_recommended: "Not recommended",
};

const VARIANT_STATUS_STYLES: Record<string, string> = {
    verified: "bg-emerald-50 text-emerald-700 border-emerald-200",
    unverified: "bg-slate-50 text-slate-600 border-slate-200",
    incompatible: "bg-red-50 text-red-700 border-red-200",
    not_recommended: "bg-amber-50 text-amber-700 border-amber-200",
};

export function variantStatusLabel(status: string | null | undefined): string {
    if (!status) return "Unverified";
    return VARIANT_STATUS_LABELS[status] ?? status;
}

export function variantStatusStyle(status: string | null | undefined): string {
    return VARIANT_STATUS_STYLES[status ?? ""] ?? VARIANT_STATUS_STYLES.unverified;
}

interface VariantStatusBadgeProps {
    variant: string;
    entry: VariantStatusEntry;
    className?: string;
}

export const VariantStatusBadge: React.FC<VariantStatusBadgeProps> = ({ variant, entry, className }) => {
    const name = variantLabel(variant);
    const statusLabel = variantStatusLabel(entry.status);
    const hasRatio = typeof entry.perf_ratio === "number" && Number.isFinite(entry.perf_ratio);
    const ratioText = hasRatio ? `${entry.perf_ratio!.toFixed(2)}x` : null;
    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md border whitespace-nowrap " +
                variantStatusStyle(entry.status) + " " + (className || "")
            }
            title={`${name}: ${statusLabel}` + (ratioText ? ` (${ratioText} of CUDA reference)` : "")}
            data-variant={variant}
            data-variant-status={entry.status}
        >
            {name}: {statusLabel}{ratioText ? ` · ${ratioText}` : ""}
        </span>
    );
};

interface VariantStatusBadgesProps {
    variantStatus: VariantStatusMap | null | undefined;
    className?: string;
}

/** All per-variant status badges for a node. Renders nothing when the node
 *  has no reported variant_status (e.g. legacy nodes not yet enriched, or a
 *  node with no gpu_variants at all). */
export const VariantStatusBadges: React.FC<VariantStatusBadgesProps> = ({ variantStatus, className }) => {
    if (!variantStatus || typeof variantStatus !== "object") return null;
    const entries = Object.entries(variantStatus);
    if (entries.length === 0) return null;
    return (
        <span className={"inline-flex items-center flex-wrap gap-1 " + (className || "")}>
            {entries.map(([variant, entry]) => (
                <VariantStatusBadge key={variant} variant={variant} entry={entry} />
            ))}
        </span>
    );
};

/**
 * FX.73 p4c — CE-edition badge.
 *
 * One source of truth per node for "included in the CE (open-source)
 * edition": the API's `ce_included` flag (node_definitions.ce_included,
 * stamped at CE seed time from open-core/node-seed-keeplist.yaml) arrives on
 * the palette payload as `node.ce_included`. Renders nothing when the flag is
 * absent/false — the badge never claims CE for a node the edition does not
 * ship. Same badge shape as VariantStatusBadge (10px, rounded, bordered).
 */
export const CE_BADGE_STYLE = "bg-sky-50 text-sky-700 border-sky-200";

interface CeBadgeProps {
    ceIncluded: boolean | null | undefined;
    className?: string;
}

export const CeBadge: React.FC<CeBadgeProps> = ({ ceIncluded, className }) => {
    if (!ceIncluded) return null;
    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md border whitespace-nowrap " +
                CE_BADGE_STYLE + " " + (className || "")
            }
            title="Included in the CE (open-source) edition"
            data-ce-included="true"
        >
            CE
        </span>
    );
};

/* --------------------------------------------------------------------- */
/**
 * FX.103 p4 — node deprecation / retirement lifecycle badge.
 *
 * FX.103 (owner decision Q89=A) gives every NodeDefinition a lifecycle
 * `status` ('active' | 'deprecated' | 'retired') plus `deprecated_at` and
 * `retire_after`. The palette API (GET /v1/nodes) returns the part-1 names
 * AND the part-2 aliases the UI reads (`lifecycle_state` + `retire_at`), so
 * this badge accepts either spelling.
 *
 *  - 'active' / absent / unknown  -> renders NOTHING (pre-FX.103 rows must
 *    look exactly as before; the badge never invents a lifecycle).
 *  - 'deprecated' -> amber "Deprecated" + tooltip naming the effective
 *    retirement date when the payload carries one.
 *  - 'retired'    -> grey "Retired" (the palette hides these by default;
 *    the badge is what the "Show retired" view renders).
 *
 * Same badge shape as CeBadge / VariantStatusBadge (10px, rounded,
 * bordered) so no new UI vocabulary is introduced.
 */
export const LIFECYCLE_BADGE_STYLES: Record<string, string> = {
    deprecated: "bg-amber-100 text-amber-800 border-amber-300",
    retired: "bg-gray-100 text-gray-500 border-gray-300",
};

/** The one lifecycle spelling the UI reads. Prefers the part-2 alias
 *  (`lifecycle_state`) and falls back to the part-1 `status`, so a client
 *  built before part 2 still badges. Anything other than the two warn
 *  states collapses to 'active' (renders nothing). Palette payloads only —
 *  the canvas NodeData.status is a run state, not a lifecycle. */
export function paletteLifecycleState(node: any): string {
    const raw = node?.lifecycle_state ?? node?.status ?? "active";
    const s = String(raw ?? "active").toLowerCase();
    return (s === "deprecated" || s === "retired") ? s : "active";
}

/** 'YYYY-MM-DD' from the API's ISO-8601 datetime (2027-01-01T00:00:00Z),
 *  or null when no date is carried. Deterministic (no locale formatting)
 *  so the tooltip reads the same in every browser and in tests. */
export function lifecycleDateText(value: any): string | null {
    if (value === null || value === undefined || value === "") return null;
    const m = /^(\d{4}-\d{2}-\d{2})/.exec(String(value));
    if (m) return m[1];
    try {
        const d = new Date(value as any);
        if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
    } catch { /* unparseable -> no date in the tooltip */ }
    return null;
}

interface LifecycleBadgeProps {
    lifecycleState: string | null | undefined;
    retireAt?: any;
    className?: string;
}

export const LifecycleBadge: React.FC<LifecycleBadgeProps> = ({ lifecycleState, retireAt, className }) => {
    const state = paletteLifecycleState({ lifecycle_state: lifecycleState });
    if (state === "active") return null;
    const dateText = lifecycleDateText(retireAt);
    const title = state === "deprecated"
        ? (dateText ? `Retires on ${dateText}` : "Deprecated — still usable; retirement date not set")
        : (dateText ? `Retired on ${dateText} — cannot run` : "Retired — cannot run");
    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md border whitespace-nowrap " +
                LIFECYCLE_BADGE_STYLES[state] + " " + (className || "")
            }
            title={title}
            data-lifecycle-state={state}
            data-retire-at={retireAt ?? undefined}
        >
            {state === "deprecated" ? "Deprecated" : "Retired"}
        </span>
    );
};

/* --------------------------------------------------------------------- */
/**
 * GSEC FX.95 p4b — publisher "Verified" badge on marketplace listings.
 *
 * The single source of truth is the API's `publisher_verified` flag on
 * every catalog row (services/catalog_service.py + schemas.CatalogItem,
 * FX.95 p4a: True when the listing's publisher tenant has a 'verified'
 * PublisherVerification record — services/publisher_verification.py
 * `publisher_verified()` / `verified_tenant_ids()`, one batched read per
 * page, never re-derived here). Absent / false renders NOTHING, so a
 * pre-FX.95 payload looks exactly as before.
 *
 * IMPORTANT — Verified is NOT MOD-built. FX.95 p5c (services/gov_mod_built_
 * only.py) makes gov compliance profiles (CMMC / fedramp_high_il5) accept
 * ONLY MOD-built listings (system nodes / platform tenant / official
 * packages). A verified THIRD-PARTY publisher is still third-party, so the
 * tooltip says what was verified and explicitly says it does not make the
 * listing allowed for gov tenants. Never reword it toward "approved" /
 * "allowed".
 *
 * Same badge shape as CeBadge / LifecycleBadge (10px, rounded, bordered)
 * so no new UI vocabulary is introduced. The check mark is an inline SVG
 * (node-chips stays dependency-free — no lucide import on the canvas chip
 * module).
 */
export const VERIFIED_BADGE_STYLE = "bg-emerald-50 text-emerald-800 border-emerald-200";

/** The one tooltip sentence pair. Split so tests can pin each half. */
export const VERIFIED_BADGE_TOOLTIP =
    "Publisher identity verified by MOD — " +
    "does not mean MOD-built: gov compliance profiles only accept MOD-built listings";

interface VerifiedBadgeProps {
    publisherVerified: boolean | null | undefined;
    className?: string;
}

export const VerifiedBadge: React.FC<VerifiedBadgeProps> = ({ publisherVerified, className }) => {
    if (!publisherVerified) return null;
    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md border whitespace-nowrap " +
                VERIFIED_BADGE_STYLE + " " + (className || "")
            }
            title={VERIFIED_BADGE_TOOLTIP}
            data-publisher-verified="true"
        >
            <svg
                viewBox="0 0 12 12"
                width="10"
                height="10"
                aria-hidden="true"
                className="inline-block mr-0.5"
            >
                <path
                    d="M2.5 6.5 L5 9 L9.5 3"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                />
            </svg>
            Verified
        </span>
    );
};

/* --------------------------------------------------------------------- */
/**
 * FX.149 p3 — palette badge NAMING THE PROVIDER on external-AI nodes.
 *
 * The single source of truth is the `external-ai` soft tag on the palette
 * payload's `custom_metadata.tags` (services/external_ai_node_gate.py
 * EXTERNAL_AI_TAG, seeded by
 * migrations/20261006v_fx149_external_ai_node_tags.py — same pattern as the
 * FX.132 prohibited-use tags). Absent tag => renders NOTHING, so a
 * pre-FX.149 payload looks exactly as before.
 *
 * The provider name is read from `custom_metadata.external_ai_provider` when
 * the payload carries it; otherwise it is derived from `node_kind` (the
 * seeded `nim_*_cloud` kinds are NVIDIA NIM hosted inference). When neither
 * is available the badge says only "External AI" — it NEVER invents a
 * provider it cannot see.
 *
 * Wording rule (docs/security-audit/external-ai-provider-inventory.md):
 * "designed to send tenant data off-box"; no compliance/certification
 * claims, and the tooltip does not say the run is allowed — the gate
 * (services/external_ai_node_gate.py check_run_external_ai) decides that at
 * run start.
 *
 * Same badge shape as CeBadge / LifecycleBadge / VerifiedBadge (10px,
 * rounded, bordered) so no new UI vocabulary is introduced.
 */
export const EXTERNAL_AI_TAG = "external-ai";
export const EXTERNAL_AI_BADGE_STYLE = "bg-violet-50 text-violet-700 border-violet-200";

/** Provider name for a tagged node, or null when the payload cannot say. */
export function externalAiProvider(node: any): string | null {
    const explicit = node?.custom_metadata?.external_ai_provider;
    if (typeof explicit === "string" && explicit.trim()) return explicit.trim();
    const kind = String(node?.node_kind ?? "").toLowerCase();
    // The FX.149 p2b seed tags exactly the "cloud" NIM kinds; the *_local
    // kinds run on-box and are never tagged, so they never badge here.
    if (kind.startsWith("nim_") && kind.endsWith("_cloud")) return "NVIDIA NIM";
    return null;
}

/** True when the palette payload carries the external-AI tag. */
export function isExternalAiTagged(node: any): boolean {
    const tags = node?.custom_metadata?.tags;
    return Array.isArray(tags) && tags.some((t) => typeof t === "string" && t === EXTERNAL_AI_TAG);
}

interface ExternalAiBadgeProps {
    /** The node payload (palette NodeDefinition or the panel's live def). */
    node: any;
    className?: string;
}

export const ExternalAiBadge: React.FC<ExternalAiBadgeProps> = ({ node, className }) => {
    if (!isExternalAiTagged(node)) return null;
    const provider = externalAiProvider(node);
    const label = provider ? `External AI · ${provider}` : "External AI";
    const title = provider
        ? `Tagged external-AI: designed to send tenant data to ${provider}. Runs are refused unless the tenant allows external-LLM egress.`
        : "Tagged external-AI: designed to send tenant data to an external AI provider. Runs are refused unless the tenant allows external-LLM egress.";
    return (
        <span
            className={
                "inline-flex items-center text-[10px] font-medium px-1.5 py-0.5 " +
                "rounded-md border whitespace-nowrap " +
                EXTERNAL_AI_BADGE_STYLE + " " + (className || "")
            }
            title={title}
            data-external-ai="true"
            data-external-ai-provider={provider ?? undefined}
        >
            {label}
        </span>
    );
};
