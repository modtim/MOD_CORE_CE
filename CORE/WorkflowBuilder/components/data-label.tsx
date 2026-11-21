"use client";

/**
 * GY.C7 — visible data-label markings.
 *
 * One shared chip + one shared colour map for the data-classification
 * labels that live server-side on ``cogs.data_classification``
 * (taxonomy: public < internal < confidential < restricted < CUI <
 * EAR < ITAR, plus the GY.V8 ``biometric`` handling label and free-form
 * custom labels). ``public``/``internal`` are the non-controlled
 * defaults — the chip renders them subtly (or not at all, when
 * ``subtleDefault={false}``), controlled labels (CUI/ITAR/EAR/PII/
 * PRE-RELEASE/confidential/restricted/biometric/custom) render loudly.
 *
 * The banner follows the CUI marking convention:
 *   "CONTROLLED: <LABEL> — handle per policy"
 * shown at the top of the builder when the open workflow or any
 * selected asset is controlled.
 */
import React from "react";

export type LabelTone = "public" | "internal" | "controlled" | "export" | "biometric";

export interface LabelStyle {
  /** Tailwind classes for the chip body. */
  chip: string;
  /** Tone category used by the banner to pick its own styling. */
  tone: LabelTone;
  /** Human label override (e.g. canonicalise case). */
  label: string;
}

const CONTROLLED_BASE = "border font-semibold";

export const LABEL_STYLES: Record<string, LabelStyle> = {
  // Non-controlled defaults — subtle.
  public: { chip: "border-neutral-300 text-neutral-400 bg-neutral-100", tone: "public", label: "Public" },
  internal: { chip: "border-neutral-300 text-neutral-500 bg-neutral-100", tone: "internal", label: "Internal" },
  // Internal handling tiers — amber.
  confidential: { chip: `${CONTROLLED_BASE} border-amber-500 text-amber-700 bg-amber-50`, tone: "controlled", label: "Confidential" },
  restricted: { chip: `${CONTROLLED_BASE} border-orange-600 text-orange-700 bg-orange-50`, tone: "controlled", label: "Restricted" },
  // Federal / export controlled — red family, escalating.
  CUI: { chip: `${CONTROLLED_BASE} border-red-700 text-red-800 bg-red-50`, tone: "export", label: "CUI" },
  EAR: { chip: `${CONTROLLED_BASE} border-red-800 text-red-900 bg-red-100`, tone: "export", label: "EAR" },
  ITAR: { chip: `${CONTROLLED_BASE} border-red-900 text-red-950 bg-red-200`, tone: "export", label: "ITAR" },
  // GY.V8 biometric handling flag.
  biometric: { chip: `${CONTROLLED_BASE} border-purple-600 text-purple-700 bg-purple-50`, tone: "biometric", label: "BIOMETRIC" },
  // Task-specified extra handled labels.
  pii: { chip: `${CONTROLLED_BASE} border-purple-500 text-purple-700 bg-purple-50`, tone: "controlled", label: "PII" },
  "pre-release": { chip: `${CONTROLLED_BASE} border-blue-600 text-blue-700 bg-blue-50`, tone: "controlled", label: "PRE-RELEASE" },
};

/** Fallback style for unrecognised custom labels — must still stand out. */
const CUSTOM_STYLE: LabelStyle = {
  chip: `${CONTROLLED_BASE} border-slate-500 text-slate-700 bg-slate-50`,
  tone: "controlled",
  label: "",
};

export function normalizeLabel(label: unknown): string {
  if (label == null) return "internal";
  const s = String(label).trim();
  if (!s) return "internal";
  return s;
}

export function styleForLabel(label: unknown): LabelStyle {
  const raw = normalizeLabel(label);
  const key = raw.toUpperCase();
  if (LABEL_STYLES[key]) return LABEL_STYLES[key];
  const lcKey = raw.toLowerCase();
  if (LABEL_STYLES[lcKey]) return LABEL_STYLES[lcKey];
  return { ...CUSTOM_STYLE, label: raw.toUpperCase() };
}

/** True when the label requires the loud chip + banner treatment. */
export function isControlledLabel(label: unknown): boolean {
  const style = styleForLabel(label);
  return style.tone !== "public" && style.tone !== "internal";
}

interface DataLabelChipProps {
  label: unknown;
  /**
   * When true (default), non-controlled labels (public/internal) are not
   * rendered — most assets are internal and a wall of grey chips is noise.
   */
  subtleDefault?: boolean;
  className?: string;
}

/** Small coloured label chip: "CUI", "ITAR", "EAR", "PRE-RELEASE", "PII", custom. */
export function DataLabelChip({ label, subtleDefault = true, className = "" }: DataLabelChipProps) {
  const raw = normalizeLabel(label);
  if (subtleDefault && styleForLabel(raw).tone === "public") return null;
  const style = styleForLabel(raw);
  return (
    <span
      data-testid="data-label-chip"
      data-label={style.label}
      title={`Data classification: ${style.label}`}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] leading-none tracking-wide ${style.chip} ${className}`}
    >
      {style.label}
    </span>
  );
}

interface ControlledBannerProps {
  /** Worst controlled label in scope; null/undefined = no banner. */
  label: unknown;
  className?: string;
}

/**
 * Top banner in the CUI marking convention:
 * "CONTROLLED: <LABEL> — handle per policy".
 */
export function ControlledBanner({ label, className = "" }: ControlledBannerProps) {
  if (!isControlledLabel(label)) return null;
  const style = styleForLabel(label);
  const exportTone = style.tone === "export" || style.tone === "biometric";
  return (
    <div
      data-testid="controlled-banner"
      role="alert"
      className={`flex items-center gap-2 border-b px-3 py-1.5 text-xs font-semibold tracking-wide ${
        exportTone
          ? "border-red-800 bg-red-100 text-red-900"
          : "border-amber-600 bg-amber-50 text-amber-900"
      } ${className}`}
    >
      <span aria-hidden="true">⚠</span>
      <span>
        CONTROLLED: {style.label} — handle per policy
      </span>
    </div>
  );
}

/**
 * Pick the worst controlled label across a set of labels.
 * Non-controlled labels are skipped; unknown labels fail LOUD (returned
 * as-is) — a corrupt label on screen is a data-marking problem, not a
 * rendering one.
 */
const LEVEL_RANK: Record<string, number> = {
  PUBLIC: 0,
  INTERNAL: 1,
  CONFIDENTIAL: 2,
  RESTRICTED: 3,
  CUI: 4,
  EAR: 5,
  ITAR: 6,
  PII: 4,
  "PRE-RELEASE": 4,
  BIOMETRIC: 4,
};

export function worstLabel(labels: Iterable<unknown>): string | null {
  let worst: string | null = null;
  let worstRank = -1;
  for (const raw of labels) {
    const label = normalizeLabel(raw);
    if (!isControlledLabel(label)) continue;
    const rank = LEVEL_RANK[label.toUpperCase()] ?? 4; // custom = controlled rank
    if (rank > worstRank) {
      worstRank = rank;
      worst = label;
    }
  }
  return worst;
}

export default DataLabelChip;
