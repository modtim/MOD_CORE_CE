"use client";

import React, { useMemo, useState } from "react";
import {
  Check, AlertTriangle, Ban, Cpu, SkipForward,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Shapes — match the Containerizer's /generate-artifacts response
// ---------------------------------------------------------------------------

export interface SourceChange {
  before: string;
  after: string;
  line?: number;
  reason?: string;
}

export interface SourceWarning {
  severity: "info" | "warn" | "fatal";
  message: string;
}

// Per (variant, filename) diagnostic block.
export interface FileAdaptation {
  changes: SourceChange[];
  warnings: SourceWarning[];
  language: string;
  incompatible: boolean;
  incompatible_reason?: string;
  llm_used: boolean;
}

// Whole-variant diagnostic. Contains one FileAdaptation per source
// file the wizard sent.
export interface VariantAdaptation {
  compat: "native" | "limited" | "unsupported" | string;
  incompatible: boolean;
  incompatible_reason?: string;
  files: Record<string, FileAdaptation>;
}

export interface HeroSourceFile {
  filename: string;
  body: string;
  language?: string;
  is_entrypoint?: boolean;
}

interface Props {
  // {variant: {filename: adapted_body}} — from Containerizer response.
  source_variants: Record<string, Record<string, string>>;
  // {variant: VariantAdaptation}.
  variant_adaptations: Record<string, VariantAdaptation>;
  // Hero source files (one per filename) — used as the "before" side
  // of each diff.
  hero_files: HeroSourceFile[];
  // Set of variant names the user has toggled "Skip" on. Controlled.
  skipped: Set<string>;
  onSkippedChange: (next: Set<string>) => void;
}

// ---------------------------------------------------------------------------
// Minimal unified diff renderer (no external dep)
// ---------------------------------------------------------------------------
//
// react-diff-viewer-continued is 15 KB gz and would be nicer, but
// adding a new npm dep to the wizard bundle is a separate PR. For v1
// we compute a line-level longest-common-subsequence diff in-house
// and render "- / + " lines, which is visually sufficient for the
// adapter's typical output (a handful of `.to("cuda")` rewrites).
export function renderDiffLines(before: string, after: string) {
  const a = before.split("\n");
  const b = after.split("\n");
  const rows: { kind: "same" | "add" | "del"; text: string }[] = [];
  // LCS via O(N*M) DP — source files are short enough that this is fine.
  const m = a.length;
  const n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    Array(n + 1).fill(0),
  );
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      rows.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: "del", text: a[i] });
      i++;
    } else {
      rows.push({ kind: "add", text: b[j] });
      j++;
    }
  }
  while (i < m) rows.push({ kind: "del", text: a[i++] });
  while (j < n) rows.push({ kind: "add", text: b[j++] });
  return rows;
}

export function DiffView({ before, after }: { before: string; after: string }) {
  const rows = useMemo(() => renderDiffLines(before, after), [before, after]);
  if (rows.every((r) => r.kind === "same")) {
    return (
      <div className="px-3 py-6 text-center text-xs text-gray-500 italic">
        No changes — adapter left this file identical to the hero.
      </div>
    );
  }
  return (
    <div className="font-mono text-[11px] leading-snug max-h-[320px] overflow-auto border rounded">
      {rows.map((r, idx) => {
        const cls =
          r.kind === "add"
            ? "bg-emerald-50 text-emerald-900"
            : r.kind === "del"
              ? "bg-rose-50 text-rose-900"
              : "text-gray-600";
        const prefix = r.kind === "add" ? "+" : r.kind === "del" ? "-" : " ";
        return (
          <div
            key={idx}
            className={`whitespace-pre-wrap break-all px-2 py-[1px] ${cls}`}
          >
            <span className="inline-block w-4 select-none opacity-60">
              {prefix}
            </span>
            {r.text || "\u00A0"}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-variant tile
// ---------------------------------------------------------------------------

function CompatBadge({ compat }: { compat: string }) {
  const styles =
    compat === "native"
      ? "bg-emerald-100 text-emerald-800"
      : compat === "limited"
        ? "bg-amber-100 text-amber-800"
        : "bg-rose-100 text-rose-800";
  return (
    <span
      className={`text-[10px] font-semibold uppercase tracking-wide px-2 py-0.5 rounded ${styles}`}
    >
      {compat || "unknown"}
    </span>
  );
}

function WarningChip({ w }: { w: SourceWarning }) {
  const colour =
    w.severity === "fatal"
      ? "border-rose-300 bg-rose-50 text-rose-800"
      : w.severity === "warn"
        ? "border-amber-300 bg-amber-50 text-amber-800"
        : "border-slate-300 bg-slate-50 text-slate-800";
  return (
    <span
      className={`text-[11px] px-2 py-0.5 rounded border ${colour}`}
      title={w.message}
    >
      {w.message.length > 80 ? w.message.slice(0, 80) + "…" : w.message}
    </span>
  );
}

function VariantTile({
  variant,
  adaptation,
  sources,
  hero_files,
  skipped,
  onToggleSkip,
}: {
  variant: string;
  adaptation: VariantAdaptation;
  sources: Record<string, string>;
  hero_files: HeroSourceFile[];
  skipped: boolean;
  onToggleSkip: () => void;
}) {
  const filenames = Object.keys(sources);
  const [activeFile, setActiveFile] = useState<string>(
    filenames[0] || "",
  );
  const heroMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const f of hero_files) m[f.filename] = f.body;
    return m;
  }, [hero_files]);

  const fileAdapt: FileAdaptation | undefined =
    adaptation.files?.[activeFile];
  const totalChanges = Object.values(adaptation.files || {}).reduce(
    (n, f) => n + (f.changes?.length || 0),
    0,
  );
  const incompat = adaptation.incompatible;

  return (
    <div
      className={[
        "border rounded-lg shadow-sm bg-white flex flex-col",
        skipped ? "opacity-50" : "",
        incompat ? "border-rose-300" : "",
      ].join(" ")}
      data-variant={variant}
    >
      {/* Header */}
      <div className="px-4 py-3 border-b flex items-center gap-2">
        <Cpu className="w-4 h-4 text-gray-500" />
        <span className="text-sm font-semibold uppercase tracking-wide flex-1">
          {variant}
        </span>
        <CompatBadge compat={adaptation.compat} />
        {incompat && (
          <span
            title={adaptation.incompatible_reason}
            className="text-[10px] px-2 py-0.5 rounded bg-rose-100 text-rose-800 uppercase font-semibold flex items-center gap-1"
          >
            <Ban className="w-3 h-3" /> incompatible
          </span>
        )}
        <button
          onClick={onToggleSkip}
          disabled={incompat}
          title={
            incompat
              ? "Variant is already excluded due to incompatibility"
              : skipped
                ? "Include this variant in the build"
                : "Skip this variant — don't build or register"
          }
          className={[
            "text-[11px] flex items-center gap-1 px-2 py-0.5 rounded border",
            skipped
              ? "bg-slate-100 border-slate-300 text-slate-700"
              : "border-slate-300 text-slate-600 hover:bg-slate-50",
            incompat ? "opacity-40 cursor-not-allowed" : "",
          ].join(" ")}
        >
          <SkipForward className="w-3 h-3" /> {skipped ? "Skipped" : "Skip"}
        </button>
      </div>

      {/* Incompatible reason — surfaces why the tile is greyed out. */}
      {incompat && adaptation.incompatible_reason && (
        <div className="px-4 py-2 text-xs text-rose-700 bg-rose-50 border-b">
          {adaptation.incompatible_reason}
        </div>
      )}

      {/* File tabs */}
      <div className="flex border-b bg-slate-50 text-xs overflow-x-auto">
        {filenames.map((fn) => {
          const warnCount =
            adaptation.files?.[fn]?.warnings?.filter(
              (w) => w.severity !== "info",
            ).length || 0;
          const changeCount =
            adaptation.files?.[fn]?.changes?.length || 0;
          const active = fn === activeFile;
          return (
            <button
              key={fn}
              onClick={() => setActiveFile(fn)}
              className={[
                "px-3 py-1.5 flex items-center gap-1 border-r",
                active
                  ? "bg-white text-gray-900 font-medium"
                  : "text-gray-600 hover:bg-slate-100",
              ].join(" ")}
              title={fn}
            >
              <span className="truncate max-w-[140px]">{fn}</span>
              {changeCount > 0 && (
                <span className="text-[9px] px-1 rounded bg-emerald-100 text-emerald-700">
                  {changeCount}
                </span>
              )}
              {warnCount > 0 && (
                <span className="text-[9px] px-1 rounded bg-amber-100 text-amber-700 flex items-center">
                  <AlertTriangle className="w-2.5 h-2.5 mr-0.5" />
                  {warnCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* File warnings + diff */}
      <div className="p-3 space-y-2">
        {fileAdapt?.warnings?.length ? (
          <div className="flex flex-wrap gap-1">
            {fileAdapt.warnings.map((w, i) => (
              <WarningChip key={i} w={w} />
            ))}
          </div>
        ) : null}
        <DiffView
          before={heroMap[activeFile] ?? ""}
          after={sources[activeFile] ?? ""}
        />
      </div>

      {/* Footer summary */}
      <div className="px-4 py-2 border-t bg-slate-50 text-[11px] text-gray-600 flex items-center gap-3">
        <span className="flex items-center gap-1">
          <Check className="w-3 h-3" /> {totalChanges} change(s)
        </span>
        <span>
          {Object.keys(sources).length} file(s)
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-level pane
// ---------------------------------------------------------------------------

export default function BuildVariantPreview({
  source_variants,
  variant_adaptations,
  hero_files,
  skipped,
  onSkippedChange,
}: Props) {
  const variants = useMemo(() => {
    const all = new Set<string>([
      ...Object.keys(source_variants || {}),
      ...Object.keys(variant_adaptations || {}),
    ]);
    return Array.from(all).sort();
  }, [source_variants, variant_adaptations]);

  if (variants.length === 0) {
    return (
      <div className="text-center py-8 text-sm text-gray-500">
        No GPU variants emitted — this node will ship as a single CPU
        image (no routing constraints).
      </div>
    );
  }

  const toggle = (v: string) => {
    const next = new Set(skipped);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    onSkippedChange(next);
  };

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
      {variants.map((v) => (
        <VariantTile
          key={v}
          variant={v}
          adaptation={variant_adaptations[v] || {
            compat: "limited",
            incompatible: false,
            files: {},
          }}
          sources={source_variants[v] || {}}
          hero_files={hero_files}
          skipped={skipped.has(v)}
          onToggleSkip={() => toggle(v)}
        />
      ))}
    </div>
  );
}
