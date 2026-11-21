"use client";
import { apiUrl } from "@/lib/runtime-config";

/**
 * wizard-rebuild §8 — Describe-mode cascade UI surface.
 *
 * Calls `POST /woodsman/describe-cascade` with a natural-
 * language description and renders the ranked candidates from all
 * 4 cascade steps (container → repo → library → codegen). Each
 * candidate row has a "Use" button that hands the `submission` dict
 * back to the wizard's existing build-and-register-node flow.
 *
 * Per-backend rate-limit status is surfaced as a chip strip below
 * the search box. A backend that's currently rate-limited shows a
 * red chip with the reset window so users know which backend is
 * blocking results (not just "search failed").
 */
import { useState } from "react";
import { Loader2, Sparkles, CheckCircle2, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";


type CandidateKind = "container" | "repo" | "library" | "codegen";

interface CascadeCandidate {
  kind: CandidateKind;
  label: string;
  submission: Record<string, unknown>;
  confidence: number;
  backend: string;
  eta_seconds: number | null;
  likely_variants: string[];
  reason: string;
}

interface RateLimitStatus {
  limit?: number;
  remaining: number;
  reset_seconds: number;
  allowed?: boolean;
  backend?: string;
}

interface CascadeResponse {
  description: string;
  confidence_floor: number;
  candidates: CascadeCandidate[];
  per_step: Array<{
    step: "A" | "B" | "C" | "D";
    candidates: CascadeCandidate[];
    rate_limited: Record<string, RateLimitStatus>;
    errors: Record<string, string>;
    short_circuited: boolean;
  }>;
  rate_limited: Record<string, RateLimitStatus>;
  errors: Record<string, string>;
  stopped_at_step: "A" | "B" | "C" | "D" | null;
  reason: string;
}


const KIND_LABEL: Record<CandidateKind, string> = {
  container: "Container",
  repo: "Git repo",
  library: "Library",
  codegen: "LLM codegen",
};

const KIND_COLOR: Record<CandidateKind, string> = {
  container: "bg-emerald-100 text-emerald-800 border-emerald-200",
  repo: "bg-sky-100 text-sky-800 border-sky-200",
  library: "bg-violet-100 text-violet-800 border-violet-200",
  codegen: "bg-amber-100 text-amber-800 border-amber-200",
};


export function DescribeCascadePanel({
  accessToken,
  onPickCandidate,
}: {
  accessToken: string | null;
  onPickCandidate?: (submission: Record<string, unknown>) => void;
}) {
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<CascadeResponse | null>(null);
  const [error, setError] = useState<string>("");

  const onSearch = async () => {
    setError("");
    if (!description.trim()) {
      setError("Please describe what you want the node to do.");
      return;
    }
    if (!accessToken) {
      setError("Not authenticated.");
      return;
    }
    setLoading(true);
    try {
      const API_URL = apiUrl();
      const resp = await fetch(`${API_URL}/woodsman/describe-cascade`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ description }),
      });
      if (!resp.ok) {
        const t = await resp.text();
        setError(`Cascade failed (${resp.status}): ${t.slice(0, 200)}`);
        return;
      }
      setResult((await resp.json()) as CascadeResponse);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4 p-4 border rounded-lg bg-card">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" />
        <Label className="font-semibold">Describe what you want</Label>
      </div>
      <Input
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="e.g. transcribe an audio file to text using whisper"
        disabled={loading}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSearch();
        }}
      />

      <Button
        onClick={onSearch}
        disabled={loading || !description.trim()}
        className="w-full"
      >
        {loading ? (
          <><Loader2 className="h-4 w-4 mr-2 animate-spin" /> Searching…</>
        ) : (
          <>Search cascade</>
        )}
      </Button>

      {error && (
        <div className="text-sm text-red-600 flex items-center gap-1">
          <AlertTriangle className="h-3 w-3" /> {error}
        </div>
      )}

      {result && (
        <>
          {/* Per-backend rate-limit chip strip — shows current
              remaining for every known backend so users know
              what's available. */}
          <div className="flex flex-wrap gap-1.5 text-xs">
            {Object.entries(result.rate_limited).map(([backend, status]) => {
              const isBlocked = status.allowed === false || status.remaining === 0;
              return (
                <span
                  key={backend}
                  className={
                    "inline-flex items-center gap-1 px-2 py-0.5 rounded border " +
                    (isBlocked
                      ? "bg-red-50 text-red-700 border-red-200"
                      : "bg-gray-50 text-gray-700 border-gray-200")
                  }
                  title={
                    isBlocked && status.reset_seconds > 0
                      ? `rate-limited; resets in ${status.reset_seconds}s`
                      : `${status.remaining}/${status.limit ?? "?"} remaining`
                  }
                >
                  {backend}: {isBlocked ? "rate-limited" : `${status.remaining} left`}
                </span>
              );
            })}
          </div>

          {/* Reason banner — short-circuited / exhausted /
              fallback-codegen. */}
          <div className="text-xs text-muted-foreground">
            {result.stopped_at_step
              ? `Cascade short-circuited at step ${result.stopped_at_step} (`
              : "Cascade "}
            {result.reason}
            {result.stopped_at_step ? ")" : ""}
          </div>

          {/* Candidate list — ranked by confidence. */}
          {result.candidates.length === 0 ? (
            <div className="text-sm text-muted-foreground italic">
              No candidates found. Try a more specific description.
            </div>
          ) : (
            <ul className="space-y-2">
              {result.candidates.map((c, i) => (
                <li
                  key={`${c.kind}-${c.backend}-${i}`}
                  className="flex items-start gap-2 p-2 border rounded-md bg-background"
                >
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded border ${KIND_COLOR[c.kind]} whitespace-nowrap mt-0.5`}
                  >
                    {KIND_LABEL[c.kind]}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm truncate">{c.label}</span>
                      <span className="text-xs text-muted-foreground">
                        {(c.confidence * 100).toFixed(0)}%
                      </span>
                      {c.eta_seconds && (
                        <span className="text-xs text-muted-foreground">
                          · ETA ~{Math.round(c.eta_seconds / 60)}m
                        </span>
                      )}
                    </div>
                    {c.reason && (
                      <div className="text-xs text-muted-foreground truncate mt-0.5">
                        {c.reason}
                      </div>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => onPickCandidate?.(c.submission)}
                  >
                    <CheckCircle2 className="h-3 w-3 mr-1" /> Use #{i + 1}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
