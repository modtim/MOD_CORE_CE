// GT.B1 — runtime frontend configuration (replaces build-time NEXT_PUBLIC_*).
//
// The customer domain is no longer baked into the client bundle. Instead the
// container entrypoint (docker-entrypoint.sh) renders public/runtime-config.js
// from the container env (MOD_API_URL / MOD_DASHBOARD_URL) at container start,
// and the root layout loads it with <Script strategy="beforeInteractive"> so
// window.__MOD_RUNTIME_CONFIG__ is set before any app code runs.
//
// Resolution order for each URL:
//   1. window.__MOD_RUNTIME_CONFIG__       (served /runtime-config.js, browser)
//   2. process.env.MOD_*                    (server-side SSR: MOD_* is NOT a
//                                            NEXT_PUBLIC_ var, so Next.js does
//                                            NOT inline it — the SSR Node
//                                            process reads the real container
//                                            value per request, keeping server
//                                            HTML in sync with the client and
//                                            avoiding a hydration mismatch)
//   3. process.env.NEXT_PUBLIC_*           (local dev: runtime-read under
//                                            `next dev`, so dev keeps working)
//   4. null                                (callers fall back to the localhost
//                                            defaults in apiUrl()/dashboardUrl()
//                                            or the host-relative logic in urls.ts)
//
// A trailing "/" is stripped from both URLs so call sites can always do
// `${apiUrl()}/...` without double slashes.

export interface RuntimeConfig {
  apiUrl?: string;
  dashboardUrl?: string;
}

declare global {
  interface Window {
    __MOD_RUNTIME_CONFIG__?: RuntimeConfig;
  }
}

const DEFAULT_API_URL = "http://api.localhost";
const DEFAULT_DASHBOARD_URL = "http://dashboard.localhost";

export function normalizeUrl(url: string | null | undefined): string {
  const trimmed = (url || "").trim();
  if (!trimmed) return "";
  return trimmed.replace(/\/+$/, "");
}

// F20 - the Workflow Builder appends API paths to apiUrl() (`${apiUrl()}/me`,
// `${apiUrl()}/harbor/status`, `${apiUrl()}/assistant/config`, ...), and those
// routes exist only under the /v1 prefix. Container envs are allowed to pass
// MOD_API_URL / NEXT_PUBLIC_API_URL with OR without the suffix (CE compose
// passes .../v1; paid/dev stacks pass the bare origin), so apiUrl() and
// apiUrlOrNull() idempotently ensure the base ends with the /v1 path segment:
// a value already ending in /v1 is returned unchanged (never /v1/v1). The
// localhost DEFAULT_API_URL keeps its existing value (tests pin it).
const API_PATH_SEGMENT = "/v1";

export function ensureApiPathSegment(url: string): string {
  if (!url) return url;
  try {
    const u = new URL(url);
    const path = u.pathname.replace(/\/+$/, "");
    if (path.endsWith(API_PATH_SEGMENT)) return url;
    u.pathname = path + API_PATH_SEGMENT;
    return u.toString();
  } catch {
    // Relative or malformed value: plain trailing-segment check.
    return url.endsWith(API_PATH_SEGMENT) ? url : url + API_PATH_SEGMENT;
  }
}

// Safe read of the ambient env without ever throwing (no `process` in some
// runtimes; non-string values ignored). Never throws.
function readEnv(name: string): string | undefined {
  try {
    if (typeof process !== "undefined" && process && process.env) {
      const value = process.env[name];
      if (typeof value === "string") return value;
    }
  } catch {
    // fall through
  }
  return undefined;
}

// Read the effective config without ever throwing (missing window, missing
// object, non-string values, etc.). Never throws.
export function getRuntimeConfig(): RuntimeConfig {
  const config: RuntimeConfig = {};

  // 1. Browser: served /runtime-config.js (win when present).
  try {
    if (typeof window !== "undefined") {
      const injected = window.__MOD_RUNTIME_CONFIG__;
      if (injected && typeof injected === "object") {
        if (typeof injected.apiUrl === "string") config.apiUrl = injected.apiUrl;
        if (typeof injected.dashboardUrl === "string") {
          config.dashboardUrl = injected.dashboardUrl;
        }
      }
    }
  } catch {
    // fall through to env below
  }

  // 2 + 3. Environment. MOD_* first (server runtime value), then the dev
  // NEXT_PUBLIC_* fallback. Skipped entirely in the browser when the served
  // config already provided the value.
  if (!config.apiUrl) {
    const v = readEnv("MOD_API_URL") ?? readEnv("NEXT_PUBLIC_API_URL");
    if (v) config.apiUrl = v;
  }
  if (!config.dashboardUrl) {
    const v =
      readEnv("MOD_DASHBOARD_URL") ?? readEnv("NEXT_PUBLIC_DASHBOARD_URL");
    if (v) config.dashboardUrl = v;
  }

  return config;
}

// Resolved URL with the localhost default applied (never empty). The /v1
// segment is ensured idempotently (see ensureApiPathSegment); the default
// keeps its existing value.
export function apiUrl(): string {
  return ensureApiPathSegment(normalizeUrl(getRuntimeConfig().apiUrl)) ||
    DEFAULT_API_URL;
}

export function dashboardUrl(): string {
  return normalizeUrl(getRuntimeConfig().dashboardUrl) || DEFAULT_DASHBOARD_URL;
}

// Nullable variants for callers that must distinguish "not configured" from a
// value (e.g. assistant navigation skips when the dashboard URL is unset)
// instead of silently falling back to the localhost default.
export function apiUrlOrNull(): string | null {
  const v = normalizeUrl(getRuntimeConfig().apiUrl);
  return v ? ensureApiPathSegment(v) : null;
}

export function dashboardUrlOrNull(): string | null {
  const v = normalizeUrl(getRuntimeConfig().dashboardUrl);
  return v || null;
}

export default getRuntimeConfig;
