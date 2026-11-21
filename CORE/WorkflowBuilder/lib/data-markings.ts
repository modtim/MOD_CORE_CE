// FX.79 p3a — CUI/ITAR data-marking banner logic (WorkflowBuilder side).
//
// The label ranking and banner wording MIRROR `CORE/API/services/
// marking_stamps.py` (FX.79 p3b — the canonical stamp every export path
// uses): the worst-ranked label wins when an item carries several marks,
// the dissemination addendum joins as `LABEL/DISSEMINATION`, and the UI
// banner is exactly `CONTROLLED: <HEADER> — handle per policy` so screen
// and exported MARKING.txt sidecar agree word-for-word.
//
// If you change `LABEL_RANK` / the banner sentence in marking_stamps.py,
// mirror it here and keep __tests__/data-marking-banner.test.tsx green
// (that test table is the cross-check that the two rankings match).

export interface DataMarkingRow {
  /** Marking label code (e.g. "CUI", "ITAR", "CUI//SP-EXPT", or a
   *  tenant-defined MarkingLabelDef.code). */
  label?: string | null;
  /** Optional handling addendum (e.g. "NOFORN", "FED ONLY"). */
  dissemination?: string | null;
}

export interface MarkingStamp {
  label: string;
  dissemination: string | null;
  /** `LABEL` or `LABEL/DISSEMINATION` (DoD convention). */
  header: string;
  /** Every live label on the resource, sorted (for reference). */
  labels: string[];
}

// Same values as marking_stamps.LABEL_RANK (keep in lock-step).
export const LABEL_RANK: Record<string, number> = {
  ITAR: 90,
  "CUI//SP-EXPT": 80,
  CUI: 70,
  EXPORT_CONTROLLED: 60,
  EAR99: 55,
  PROPRIETARY: 50,
};
export const CUSTOM_RANK = 40;

export function rankLabel(code: string): number {
  return LABEL_RANK[code.toUpperCase()] ?? CUSTOM_RANK;
}

/**
 * Fold a resource's live marking rows into ONE stamp — the worst-ranked
 * label (mirrors marking_stamps.export_stamp: sort by (-rank, label code),
 * winner is first). Returns null when the resource carries no usable mark.
 */
export function worstMarking(rows: DataMarkingRow[] | null | undefined): MarkingStamp | null {
  const ranked: Array<[number, string, string]> = [];
  for (const row of rows ?? []) {
    const code = ((row?.label as string) ?? "").trim();
    if (!code) continue;
    ranked.push([rankLabel(code), code, ((row?.dissemination as string) ?? "").trim()]);
  }
  if (ranked.length === 0) return null;
  ranked.sort((a, b) => (b[0] - a[0]) || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  const [, label, dissemination] = ranked[0];
  const header = dissemination ? `${label}/${dissemination}` : label;
  return {
    label,
    dissemination: dissemination || null,
    header,
    labels: [...new Set(ranked.map((t) => t[1]))].sort(),
  };
}

/** The canonical UI banner sentence (word-for-word marking_stamps.py). */
export function markingBannerText(stamp: MarkingStamp): string {
  return `CONTROLLED: ${stamp.header} — handle per policy`;
}
