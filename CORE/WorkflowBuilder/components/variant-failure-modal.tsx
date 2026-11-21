"use client";

import { useState } from "react";
import { X, AlertCircle, Wrench, FileText, Copy, Check } from "lucide-react";
import { CopyLogButton } from "@/components/ui/copy-log-button";

/**
 * P9o.3 — "Why did this fail?" modal.
 *
 * Rendered when a user clicks a red variant badge on the build-queue-row.
 * Shows the structured failure explanation the API produced at PATCH
 * time (LLM-written or pattern-matched from utils/port_failure_explain).
 * Provides a "Copy as issue" button that formats everything as markdown
 * ready to paste into a GitHub / Linear issue.
 *
 * The explanation source (llm / pattern:X / default) is surfaced at the
 * top so the user knows whether they're reading AI analysis or a
 * template match — useful for calibrating trust.
 */

type FailureExplanation = {
  reason: string;
  manual_fix: string;
  log_excerpt: string;
  source: string;
  original_reason?: string;
};

type Props = {
  open: boolean;
  onClose: () => void;
  variant: string;
  explanation: FailureExplanation | null;
  nodeName: string;
};

function sourceLabel(source: string): { text: string; color: string } {
  if (source === "llm") {
    return { text: "LLM analysis", color: "bg-indigo-100 text-indigo-800" };
  }
  if (source === "cache") {
    return { text: "Cached explanation", color: "bg-slate-100 text-slate-700" };
  }
  if (source.startsWith("pattern:")) {
    return {
      text: `Pattern match (${source.slice(8)})`,
      color: "bg-amber-100 text-amber-800",
    };
  }
  return {
    text: "Generic — no specific pattern matched",
    color: "bg-slate-100 text-slate-600",
  };
}

function buildMarkdownReport(
  variant: string,
  nodeName: string,
  explanation: FailureExplanation,
): string {
  return [
    `## Port failure: \`${variant}\` variant of \`${nodeName}\``,
    "",
    `**Source:** ${sourceLabel(explanation.source).text}`,
    "",
    "### What the platform thinks happened",
    "",
    explanation.reason,
    "",
    "### Suggested manual fix",
    "",
    explanation.manual_fix,
    "",
    "### Log excerpt",
    "",
    "```",
    explanation.log_excerpt || "(no excerpt available)",
    "```",
    "",
    ...(explanation.original_reason
      ? [
        "### Original error (from ImageBuilder)",
        "",
        "```",
        explanation.original_reason,
        "```",
        "",
      ]
      : []),
    "---",
    "*Filed from the MOD wizard build-queue-row failure modal.*",
  ].join("\n");
}

export function VariantFailureModal({ open, onClose, variant, explanation, nodeName }: Props) {
  const [copied, setCopied] = useState(false);

  if (!open || !explanation) return null;

  const sl = sourceLabel(explanation.source);

  const copyAsIssue = async () => {
    const md = buildMarkdownReport(variant, nodeName, explanation);
    try {
      await navigator.clipboard.writeText(md);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback: select a hidden textarea + document.execCommand. Skip
      // for now — modern browsers support navigator.clipboard.
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white w-full max-w-3xl rounded-lg shadow-2xl flex flex-col max-h-[85vh]">
        <div className="flex items-center justify-between border-b px-6 py-4">
          <div className="flex items-center gap-3">
            <AlertCircle className="w-5 h-5 text-red-600 shrink-0" />
            <div>
              <h2 className="text-lg font-semibold text-gray-800">
                <span className="uppercase font-mono">{variant}</span> failed
              </h2>
              <p className="text-xs text-gray-500 mt-0.5">
                <span className={`inline-block px-1.5 py-0.5 text-[10px] font-medium rounded ${sl.color}`}>
                  {sl.text}
                </span>
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 p-1"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-5">
          {/* Reason */}
          <section>
            <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-2">
              <AlertCircle className="w-4 h-4" />
              What happened
            </h3>
            <p className="text-sm text-gray-800 leading-relaxed">
              {explanation.reason}
            </p>
          </section>

          {/* Manual fix */}
          <section className="bg-green-50 border border-green-200 rounded-lg p-4">
            <h3 className="text-sm font-semibold text-green-800 mb-2 flex items-center gap-2">
              <Wrench className="w-4 h-4" />
              Suggested fix
            </h3>
            <p className="text-sm text-green-900 leading-relaxed">
              {explanation.manual_fix}
            </p>
          </section>

          {/* Log excerpt */}
          {explanation.log_excerpt && (
            <section>
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
                  <FileText className="w-4 h-4" />
                  Log excerpt
                </h3>
                <CopyLogButton
                  getText={() => explanation.log_excerpt || ""}
                  title="Copy the raw log excerpt to the clipboard"
                />
              </div>
              <pre className="text-[11px] font-mono bg-slate-900 text-slate-100 rounded p-3 overflow-x-auto max-h-64 overflow-y-auto whitespace-pre">
                {explanation.log_excerpt}
              </pre>
            </section>
          )}

          {/* Original reason — collapsed, for reference */}
          {explanation.original_reason && (
            <section>
              <details className="text-xs">
                <summary className="cursor-pointer text-gray-500 hover:text-gray-700">
                  Original error (from ImageBuilder)
                </summary>
                <pre className="mt-2 text-[11px] font-mono bg-gray-50 border border-gray-200 rounded p-2 whitespace-pre-wrap">
                  {explanation.original_reason}
                </pre>
              </details>
            </section>
          )}
        </div>

        <div className="border-t px-6 py-3 flex items-center justify-between">
          <div className="text-[11px] text-gray-400">
            Filed against node <span className="font-mono">{nodeName}</span>
          </div>
          <div className="flex gap-2">
            <button
              onClick={copyAsIssue}
              className="flex items-center gap-2 px-3 py-1.5 text-xs font-medium rounded border border-gray-300 bg-white hover:bg-gray-50 text-gray-700"
            >
              {copied ? (
                <>
                  <Check className="w-3.5 h-3.5 text-green-600" />
                  Copied
                </>
              ) : (
                <>
                  <Copy className="w-3.5 h-3.5" />
                  Copy as issue
                </>
              )}
            </button>
            <button
              onClick={onClose}
              className="px-4 py-1.5 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
