import { apiUrl } from "@/lib/runtime-config";
/**
 * Shared client for the API's PUBLIC /platform/features endpoint.
 *
 * This is the single backend signal that decides whether credits are
 * metered on this deployment. The Dashboard reads the SAME endpoint
 * (CORE/Dashboard/js/api.js getPlatformFeatures + ui.js
 * renderHeaderCredits) to show "Unlimited" on-prem vs a numeric balance
 * on SaaS. The WorkflowBuilder must resolve credit UI from this exact
 * signal so the two surfaces cannot drift apart (a raw "2100 credits" in
 * the builder while the Dashboard says "Unlimited" for the same user).
 *
 * /platform/features is a PUBLIC (no-auth) endpoint whose payload only
 * changes on a server redeploy, so we coalesce + cache it client-side.
 * Plain fetch (NOT authenticatedFetch): the endpoint needs no token and
 * must never trip the 401 -> silent-refresh path.
 */

const API_URL = apiUrl();

export interface PlatformFeatures {
  deployment_mode?: "saas" | "onprem";
  credits_enabled?: boolean;
  billing_enabled?: boolean;
  [key: string]: unknown;
}

const _featuresCache: {
  data: PlatformFeatures | null;
  fetchedAt: number;
  inFlight: Promise<PlatformFeatures> | null;
} = { data: null, fetchedAt: 0, inFlight: null };

const FEATURES_TTL_MS = 30 * 1000;

export function getPlatformFeatures(): Promise<PlatformFeatures> {
  const now = Date.now();
  if (_featuresCache.data && now - _featuresCache.fetchedAt < FEATURES_TTL_MS) {
    return Promise.resolve(_featuresCache.data);
  }
  if (_featuresCache.inFlight) return _featuresCache.inFlight;

  _featuresCache.inFlight = fetch(`${API_URL}/platform/features`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((data: PlatformFeatures) => {
      _featuresCache.data = data;
      _featuresCache.fetchedAt = Date.now();
      _featuresCache.inFlight = null;
      return data;
    })
    .catch((err) => {
      _featuresCache.inFlight = null;
      throw err;
    });

  return _featuresCache.inFlight;
}

/**
 * True only when credits are actively metered on this deployment
 * (SaaS with CREDITS_ENABLED on). On-prem — and SaaS with credits
 * disabled — this is false, meaning credits are effectively unlimited
 * and no numeric balance/cost should be shown.
 *
 * Mirrors the Dashboard's `features.credits_enabled !== true` check.
 */
export function areCreditsEnabled(features: PlatformFeatures | null | undefined): boolean {
  return features?.credits_enabled === true;
}

/** Test seam: reset the module-level cache between tests. */
export function _resetPlatformFeaturesCache(): void {
  _featuresCache.data = null;
  _featuresCache.fetchedAt = 0;
  _featuresCache.inFlight = null;
}
