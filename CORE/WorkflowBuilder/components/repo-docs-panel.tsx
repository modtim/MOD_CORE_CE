"use client";

/**
 * wizard-rebuild §10b — read-only View Reference Docs panel.
 *
 * Renders the chunks the §3c repo-docs extractor produced for a
 * Git-mode submission, grouped by intent. Each chunk shows the
 * source file + line range + intent badges + a "View on GitHub"
 * deep link so users can verify the wizard pulled the right
 * passages from the repo's README / setup.py / pyproject.toml.
 *
 * Collapsed by default — users open this only when they want to
 * understand why the wizard inferred X. Empty list (no chunks)
 * → panel renders nothing.
 */
import { useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";


type RepoDocsChunk = {
  source_file: string;
  start_line?: number;
  end_line?: number;
  intents?: string[];
  heading?: string;
  url?: string;            // GitHub deep link, when known
  text_excerpt?: string;   // first ~200 chars of the chunk body
};


const INTENT_COLOR: Record<string, string> = {
  install: "bg-sky-100 text-sky-800 border-sky-200",
  entrypoint: "bg-emerald-100 text-emerald-800 border-emerald-200",
  env: "bg-amber-100 text-amber-800 border-amber-200",
  deps: "bg-violet-100 text-violet-800 border-violet-200",
  hardware: "bg-red-100 text-red-800 border-red-200",
  ports: "bg-indigo-100 text-indigo-800 border-indigo-200",
  example: "bg-gray-100 text-gray-700 border-gray-200",
};


export function RepoDocsPanel({
  chunks,
  defaultOpen = false,
}: {
  chunks: RepoDocsChunk[];
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (!chunks || chunks.length === 0) return null;

  // Group by intent for the rendered hierarchy. A chunk with
  // multiple intents shows up under each.
  const groups: Record<string, RepoDocsChunk[]> = {};
  for (const c of chunks) {
    const intents = c.intents && c.intents.length > 0 ? c.intents : ["other"];
    for (const intent of intents) {
      (groups[intent] ||= []).push(c);
    }
  }

  return (
    <div className="border rounded-md bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 text-sm font-semibold hover:bg-muted/50"
      >
        <span>
          📖 Reference docs · {chunks.length} chunk{chunks.length === 1 ? "" : "s"}
        </span>
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3">
          {Object.entries(groups).map(([intent, items]) => (
            <div key={intent}>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                {intent}
              </div>
              <ul className="space-y-1.5">
                {items.map((c, i) => {
                  const range = c.start_line
                    ? `:${c.start_line}${c.end_line && c.end_line !== c.start_line ? `-${c.end_line}` : ""}`
                    : "";
                  return (
                    <li
                      key={`${intent}-${i}`}
                      className="text-xs border rounded px-2 py-1 bg-background"
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[11px]">
                          {c.source_file}
                          {range}
                        </span>
                        {(c.intents || []).map((it) => (
                          <span
                            key={it}
                            className={`text-[10px] px-1.5 py-0.5 rounded border ${INTENT_COLOR[it] || INTENT_COLOR.example}`}
                          >
                            {it}
                          </span>
                        ))}
                        {c.url && (
                          <a
                            href={c.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="ml-auto text-[10px] text-sky-700 hover:underline inline-flex items-center gap-0.5"
                          >
                            View on GitHub
                            <ExternalLink className="h-3 w-3" />
                          </a>
                        )}
                      </div>
                      {c.heading && (
                        <div className="text-[11px] text-muted-foreground mt-0.5">
                          {c.heading}
                        </div>
                      )}
                      {c.text_excerpt && (
                        <div className="text-[10px] text-muted-foreground mt-1 font-mono whitespace-pre-wrap break-words line-clamp-3">
                          {c.text_excerpt}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
