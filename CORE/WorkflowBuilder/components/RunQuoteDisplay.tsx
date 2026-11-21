"use client";

import { useEffect, useState } from "react";
import { ChevronDown, Lock, AlertTriangle } from "lucide-react";
import { apiUrl } from "@/lib/runtime-config";
import { authenticatedFetch } from "@/lib/api-client";
import { Badge } from "@/components/ui/badge";
import {
    Collapsible,
    CollapsibleContent,
    CollapsibleTrigger,
} from "@/components/ui/collapsible";

// GX.sz-tcp.25 — owner decision 10-10: "each run is priced exactly from its
// input size and the user pays the LOCKED price shown before Run." This
// component fetches `POST /v1/road/{id}/quote` (sz-tcp.18) right in the
// Execute flow and renders the total + per-node breakdown + the "locked
// price" note. It is intentionally non-blocking: a failed quote fetch never
// prevents the user from running the workflow — it just falls back to a
// "standard rate" message so Execute keeps working.

interface RunQuoteNodeBreakdown {
    node_id: string | null;
    size_tier: string;
    credits: number;
    basis: string;
    pricing_source: string | null;
    predicted_runtime_seconds: number | null;
}

interface RunQuoteResponse {
    workflow_id: number;
    total_credits: number;
    breakdown: RunQuoteNodeBreakdown[];
    confidence: string;
}

interface RunQuoteDisplayProps {
    workflowId: number | null | undefined;
    accessToken: string | null;
}

const PRICING_SOURCE_LABEL: Record<string, string> = {
    exact: "exact",
    tier: "tier",
    manual: "manual",
    free: "free",
};

function PricingSourceBadge({ source }: { source: string | null }): JSX.Element | null {
    if (!source) return null;
    const label = PRICING_SOURCE_LABEL[source] || source;
    return (
        <Badge variant="outline" className="ml-2 text-[10px] px-1.5 py-0 font-normal">
            {label}
        </Badge>
    );
}

export const RunQuoteDisplay = ({ workflowId, accessToken }: RunQuoteDisplayProps): JSX.Element | null => {
    const [quote, setQuote] = useState<RunQuoteResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<boolean>(false);
    const [expanded, setExpanded] = useState(false);

    useEffect(() => {
        if (!workflowId || !accessToken) {
            setQuote(null);
            setError(false);
            return;
        }

        let cancelled = false;
        const fetchQuote = async () => {
            setLoading(true);
            setError(false);
            try {
                const response = await authenticatedFetch(`${apiUrl()}/road/${workflowId}/quote`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ node_inputs: {} }),
                });
                if (!response.ok) {
                    if (!cancelled) setError(true);
                    return;
                }
                const data: RunQuoteResponse = await response.json();
                if (!cancelled) setQuote(data);
            } catch {
                if (!cancelled) setError(true);
            } finally {
                if (!cancelled) setLoading(false);
            }
        };

        fetchQuote();
        return () => {
            cancelled = true;
        };
    }, [workflowId, accessToken]);

    if (!workflowId) {
        return null;
    }

    if (loading && !quote) {
        return (
            <div className="flex items-center gap-1.5 px-2 py-1 text-xs text-gray-500" data-testid="run-quote-loading">
                Calculating price...
            </div>
        );
    }

    if (error && !quote) {
        return (
            <div
                className="flex items-center gap-1.5 px-2 py-1 text-xs text-amber-700"
                data-testid="run-quote-error"
            >
                <AlertTriangle className="h-3.5 w-3.5" />
                Couldn&apos;t get a price — you can still run.
            </div>
        );
    }

    if (!quote) {
        return null;
    }

    const isFree = quote.total_credits === 0;

    return (
        <div className="flex flex-col items-start text-xs" data-testid="run-quote-display">
            <Collapsible open={expanded} onOpenChange={setExpanded}>
                <div className="flex items-center gap-1.5">
                    <Lock className="h-3.5 w-3.5 text-gray-500" />
                    <span className="font-semibold text-gray-800">
                        {isFree ? "Free" : `${quote.total_credits} credits`}
                    </span>
                    {quote.breakdown.length > 0 && (
                        <CollapsibleTrigger asChild>
                            <button
                                type="button"
                                className="flex items-center text-gray-500 hover:text-gray-700"
                                title="Per-node breakdown"
                            >
                                <ChevronDown
                                    className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-180" : ""}`}
                                />
                            </button>
                        </CollapsibleTrigger>
                    )}
                </div>
                <div className="text-gray-500">
                    Nothing is charged.
                </div>
                <CollapsibleContent>
                    <ul className="mt-1 space-y-0.5">
                        {quote.breakdown.map((row, idx) => (
                            <li key={row.node_id ?? idx} className="flex items-center text-gray-600">
                                <span>{row.node_id ?? "node"}</span>
                                <span className="mx-1">·</span>
                                <span>{row.size_tier}</span>
                                <span className="mx-1">·</span>
                                <span>{row.credits} cr</span>
                                <PricingSourceBadge source={row.pricing_source} />
                            </li>
                        ))}
                    </ul>
                </CollapsibleContent>
            </Collapsible>
        </div>
    );
};

export default RunQuoteDisplay;
