"use client";
import { apiUrl } from "@/lib/runtime-config";

import { useEffect, useState } from "react";
import { DollarSign, AlertTriangle, Info, Infinity as InfinityIcon } from "lucide-react";
import { authenticatedFetch } from "@/lib/api-client";
import { getPlatformFeatures, areCreditsEnabled } from "@/lib/platform-features";

interface CostPrediction {
  total_credits: number;
  total_usd: number | null;
  billing_mode: "prepaid" | "postpaid";
  subscription_info?: {
    plan_name: string;
    credits_per_month: number;
    price_in_cents: number;
    credits_used_this_period: number;
  };
  sufficient_credits: boolean;
  user_balance: number;
  deficit: number;
  warnings: Array<{
    type: string;
    message: string;
    severity: "info" | "warning" | "error";
  }>;
}

interface WorkflowCostDisplayProps {
  nodes: any[];
  edges: any[];
  accessToken: string | null;
  fileCount?: number;
}

export const WorkflowCostDisplay = ({ nodes, edges, accessToken, fileCount = 1 }: WorkflowCostDisplayProps) => {
  // Stale-while-revalidate: we keep the LAST successful costData visible
  // while the next poll is in flight, so the pill doesn't flash or
  // disappear every time nodes/edges change. Combined with the
  // fixed min-width on the wrapper below, this is what stops the
  // topbar reflow/twitch the user was seeing.
  const [costData, setCostData] = useState<CostPrediction | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Whether credits are metered on THIS deployment. Resolved from the
  // shared /platform/features.credits_enabled signal — the exact same
  // field the Dashboard header reads (renderHeaderCredits) — so the two
  // surfaces cannot disagree. `null` = not yet resolved (render a neutral
  // spacer, never a misleading number); `false` = on-prem / credits off
  // (show "Unlimited"); `true` = SaaS metered (numeric cost path).
  const [creditsEnabled, setCreditsEnabled] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPlatformFeatures()
      .then((features) => {
        if (!cancelled) setCreditsEnabled(areCreditsEnabled(features));
      })
      .catch(() => {
        // Features endpoint unreachable: leave unresolved so we render a
        // neutral spacer rather than guessing (never show a wrong number
        // to SaaS, never a stray balance on-prem).
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // Credits not metered (on-prem or SaaS with credits disabled): never
    // fetch or display a numeric cost/balance. The render below shows an
    // "Unlimited" pill instead, matching the Dashboard.
    if (creditsEnabled !== true) {
      setCostData(null);
      return;
    }

    // Only fetch if we have nodes and access token
    if (!accessToken || nodes.length === 0) {
      setCostData(null);
      return;
    }

    const fetchCost = async () => {
      setLoading(true);
      setError(null);

      try {
        const apiUrlBase = apiUrl();

        const response = await authenticatedFetch(`${apiUrlBase}/road/predict-cost`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            nodes: nodes,
            edges: edges,
            file_count: fileCount,
          }),
        });

        if (!response.ok) {
          // Handle 401 silently — token may have expired. Don't clear
          // costData: we keep the last-good pill visible instead of
          // flashing blank, which was the other source of topbar twitch.
          if (response.status === 401) {
            return;
          }
          throw new Error(`Failed to fetch cost: ${response.statusText}`);
        }

        const data = await response.json();
        setCostData(data);
      } catch (err) {
        // Only log non-auth errors
        if (!(err instanceof Error && err.message.includes('401'))) {
          console.error("Error fetching cost prediction:", err);
        }
        setError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setLoading(false);
      }
    };

    // Debounce the fetch - wait 500ms after last change
    const timer = setTimeout(fetchCost, 500);
    return () => clearTimeout(timer);
  }, [nodes, edges, accessToken, fileCount, creditsEnabled]);

  // Don't render if no nodes
  if (nodes.length === 0) {
    return null;
  }

  // Credits not metered on this deployment (on-prem, or SaaS with
  // CREDITS_ENABLED=false). Show "Unlimited" — identical semantics and
  // wording to the Dashboard header — instead of a misleading credit
  // cost/balance. Reflects reality: run_workflow skips the credit
  // charge entirely when credits_enabled is false.
  if (creditsEnabled === false) {
    return (
      <div
        className="flex items-center gap-2 px-3 py-1.5 bg-gray-100 rounded-md text-sm min-w-[9rem] h-[2.25rem]"
        title="Credits: unlimited on this deployment"
      >
        <InfinityIcon className="h-4 w-4 text-gray-500" />
        <span className="font-semibold text-gray-700">Unlimited</span>
      </div>
    );
  }

  // Credits signal not yet resolved: render a neutral, fixed-size spacer
  // so the topbar doesn't shift, and we never flash a numeric value
  // before we know whether it should be shown.
  if (creditsEnabled === null) {
    return <div className="min-w-[9rem] h-[2.25rem]" aria-hidden="true" />;
  }

  // Initial-load state (first fetch hasn't completed yet, no prior data
  // to show). Use a fixed min-width + fixed-height placeholder so the
  // topbar doesn't bounce when the pill swaps from "Calculating..." to
  // the real cost. Wrapper dimensions match the loaded state below.
  if (!costData && loading) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-100 rounded-md text-sm min-w-[9rem] h-[2.25rem]">
        <DollarSign className="h-4 w-4 text-gray-500 animate-pulse" />
        <span className="text-gray-600">Calculating...</span>
      </div>
    );
  }

  // Error state only if we have no prior successful read to fall back
  // on. If we do have costData, prefer to keep showing the stale value
  // with a warning icon rather than flash "Cost unavailable" mid-poll.
  if (error && !costData) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-red-50 border border-red-200 rounded-md text-sm min-w-[9rem] h-[2.25rem]">
        <AlertTriangle className="h-4 w-4 text-red-600" />
        <span className="text-red-700">Cost unavailable</span>
      </div>
    );
  }

  // No data yet and not loading (edge: no token, zero-node state
  // already handled). Render a placeholder rather than null so layout
  // doesn't shift.
  if (!costData) {
    return <div className="min-w-[9rem] h-[2.25rem]" aria-hidden="true" />;
  }

  // Determine display style based on warnings
  const hasError = costData.warnings.some(w => w.severity === "error");
  const hasWarning = costData.warnings.some(w => w.severity === "warning");

  let bgColor = "bg-gray-100";
  let borderColor = "";
  let textColor = "text-gray-700";
  let iconColor = "text-gray-600";

  if (hasError) {
    bgColor = "bg-red-50";
    borderColor = "border border-red-200";
    textColor = "text-red-700";
    iconColor = "text-red-600";
  } else if (hasWarning) {
    bgColor = "bg-orange-50";
    borderColor = "border border-orange-200";
    textColor = "text-orange-700";
    iconColor = "text-orange-600";
  }

  // Format cost display based on billing mode
  let costDisplay = "";
  let subtitle = "";

  if (costData.billing_mode === "postpaid") {
    // Pay-as-you-go: show USD cost
    costDisplay = `$${costData.total_usd?.toFixed(2) || "0.00"}`;
    subtitle = `${costData.total_credits} credits`;
  } else {
    // Prepaid/Subscription: show credits
    costDisplay = `${costData.total_credits} credits`;
    if (costData.subscription_info) {
      subtitle = costData.subscription_info.plan_name;
    }
  }

  return (
    <div
      className={`flex items-center gap-2 px-3 py-1.5 ${bgColor} ${borderColor} rounded-md text-sm cursor-help group relative min-w-[9rem] h-[2.25rem] transition-opacity ${loading ? 'opacity-70' : 'opacity-100'}`}
      title={costData.warnings.map(w => w.message).join("\n") || "Estimated workflow cost"}
    >
      <DollarSign className={`h-4 w-4 ${iconColor} ${loading ? 'animate-pulse' : ''}`} />
      <div className="flex flex-col min-w-0">
        <span className={`font-semibold ${textColor} truncate`}>{costDisplay}</span>
        {subtitle && <span className="text-xs text-gray-500 truncate">{subtitle}</span>}
      </div>

      {/* Tooltip with full details */}
      <div className="absolute top-full left-0 mt-2 w-64 bg-white border border-gray-200 rounded-md shadow-lg p-3 hidden group-hover:block z-50">
        <div className="text-xs space-y-2">
          <div>
            <div className="font-semibold text-gray-700 mb-1">Cost Breakdown</div>
            <div className="space-y-1">
              <div className="flex justify-between">
                <span className="text-gray-600">Credits:</span>
                <span className="font-medium">{costData.total_credits}</span>
              </div>
              {costData.billing_mode === "postpaid" && costData.total_usd && (
                <div className="flex justify-between">
                  <span className="text-gray-600">USD Cost:</span>
                  <span className="font-medium">${costData.total_usd.toFixed(2)}</span>
                </div>
              )}
              <div className="flex justify-between">
                <span className="text-gray-600">Balance:</span>
                <span className="font-medium">{costData.user_balance.toLocaleString()}</span>
              </div>
            </div>
          </div>

          {costData.subscription_info && (
            <div className="border-t border-gray-200 pt-2">
              <div className="font-semibold text-gray-700 mb-1">Usage</div>
              <div className="space-y-1">
                <div className="flex justify-between">
                  <span className="text-gray-600">Plan:</span>
                  <span className="font-medium">{costData.subscription_info.plan_name}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Used:</span>
                  <span className="font-medium">
                    {costData.subscription_info.credits_used_this_period.toLocaleString()} / {costData.subscription_info.credits_per_month.toLocaleString()}
                  </span>
                </div>
              </div>
            </div>
          )}

          {costData.warnings.length > 0 && (
            <div className="border-t border-gray-200 pt-2">
              {costData.warnings.map((warning, idx) => (
                <div key={idx} className={`flex items-start gap-1 ${
                  warning.severity === "error" ? "text-red-700" :
                  warning.severity === "warning" ? "text-orange-700" :
                  "text-blue-700"
                }`}>
                  {warning.severity === "error" ? (
                    <AlertTriangle className="h-3 w-3 flex-shrink-0 mt-0.5" />
                  ) : (
                    <Info className="h-3 w-3 flex-shrink-0 mt-0.5" />
                  )}
                  <span className="text-xs">{warning.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
