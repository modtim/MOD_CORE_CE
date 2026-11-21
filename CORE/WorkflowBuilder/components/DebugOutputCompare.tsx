"use client";

// DebugOutputCompare — side-by-side display of original vs. new (fix-verified)
// workflow-run output.  Rows are aligned by label; a "changed" badge marks
// rows whose values differ.  Missing-side placeholders show "—".

export interface OutputItem {
    label: string;
    kind: "text" | "json" | "image" | "url";
    value: string;
}

interface DebugOutputCompareProps {
    original: OutputItem[];
    candidate: OutputItem[];
    title?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Merge two output arrays, preserving insertion order of labels. */
function sortedLabels(original: OutputItem[], candidate: OutputItem[]): string[] {
    const seen = new Set<string>();
    const order: string[] = [];
    for (const item of [...original, ...candidate]) {
        if (!seen.has(item.label)) {
            seen.add(item.label);
            order.push(item.label);
        }
    }
    return order;
}

function byLabel(arr: OutputItem[], label: string): string | undefined {
    return arr.find((i) => i.label === label)?.value;
}

function prettyJson(raw: string): string {
    try {
        const obj = JSON.parse(raw);
        return JSON.stringify(obj, null, 2);
    } catch {
        return raw;
    }
}

// ---------------------------------------------------------------------------
// Cell renderer
// ---------------------------------------------------------------------------

function OutputCell({ item }: { item: OutputItem }) {
    const { kind, value, label } = item;

    switch (kind) {
        case "json":
            return (
                <pre className="whitespace-pre-wrap break-all text-xs font-mono bg-slate-50 dark:bg-slate-900 border rounded px-2 py-1 text-slate-800 dark:text-slate-200 max-h-64 overflow-auto">
                    {prettyJson(value)}
                </pre>
            );
        case "image":
            return (
                <img
                    src={value}
                    alt={label}
                    className="max-h-48 rounded border"
                />
            );
        case "url":
            return (
                <a
                    href={value}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-600 underline text-xs break-all"
                >
                    {value}
                </a>
            );
        default: // "text"
            return (
                <pre className="whitespace-pre-wrap break-all text-xs bg-slate-50 dark:bg-slate-900 border rounded px-2 py-1 text-slate-800 dark:text-slate-200 max-h-64 overflow-auto">
                    {value}
                </pre>
            );
    }
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function DebugOutputCompare({
    original,
    candidate,
    title = "Debug output comparison",
}: DebugOutputCompareProps) {
    if (original.length === 0 && candidate.length === 0) {
        return (
            <div className="rounded-lg border border-slate-200 p-4 text-center text-sm text-slate-500">
                No outputs to compare
            </div>
        );
    }

    const labels = sortedLabels(original, candidate);

    return (
        <div className="space-y-2">
            {title && (
                <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
                    {title}
                </h3>
            )}

            <div className="grid grid-cols-2 gap-4">
                {/* Original column */}
                <div className="space-y-2">
                    <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                        Original
                    </div>
                    {labels.map((label) => {
                        const orig = byLabel(original, label);
                        const cand = byLabel(candidate, label);
                        const changed =
                            orig !== undefined &&
                            cand !== undefined &&
                            orig !== cand;

                        return (
                            <div key={label} className="space-y-1">
                                <div className="flex items-center gap-2">
                                    <span className="text-xs font-medium text-slate-600 dark:text-slate-300 truncate">
                                        {label}
                                    </span>
                                    {changed && (
                                        <span className="inline-flex items-center rounded-full border border-amber-300 bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
                                            changed
                                        </span>
                                    )}
                                </div>
                                {orig !== undefined ? (
                                    <OutputCell item={original.find((i) => i.label === label)!} />
                                ) : (
                                    <div className="text-xs text-slate-400">—</div>
                                )}
                            </div>
                        );
                    })}
                </div>

                {/* After fix column */}
                <div className="space-y-2">
                    <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                        After fix
                    </div>
                    {labels.map((label) => {
                        const orig = byLabel(original, label);
                        const cand = byLabel(candidate, label);
                        const changed =
                            orig !== undefined &&
                            cand !== undefined &&
                            orig !== cand;

                        return (
                            <div key={`fix-${label}`} className="space-y-1">
                                <div className="flex items-center gap-2">
                                    <span className="text-xs font-medium text-slate-600 dark:text-slate-300 truncate">
                                        {label}
                                    </span>
                                    {changed && (
                                        <span className="inline-flex items-center rounded-full border border-amber-300 bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
                                            changed
                                        </span>
                                    )}
                                </div>
                                {cand !== undefined ? (
                                    <OutputCell item={candidate.find((i) => i.label === label)!} />
                                ) : (
                                    <div className="text-xs text-slate-400">—</div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>
        </div>
    );
}
