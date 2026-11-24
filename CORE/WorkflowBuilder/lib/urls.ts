import { getRuntimeConfig, normalizeUrl, ensureApiPathSegment } from "@/lib/runtime-config";

export function getApiUrl(): string {
  // GT.B1 — runtime config (window.__MOD_RUNTIME_CONFIG__ / container env)
  // first; host-relative derivation below stays as the last-resort fallback.
  // F22 — every return path routes through ensureApiPathSegment (same idempotent
  // /v1 normalisation as runtime-config.ts apiUrl()/apiUrlOrNull()), so WB API
  // calls built from getApiUrl() always carry the /v1 prefix (CE compose passes
  // .../v1; paid/dev stacks pass the bare origin).
  const cfg = getRuntimeConfig();
  if (cfg.apiUrl) return ensureApiPathSegment(normalizeUrl(cfg.apiUrl));
  if (typeof window === "undefined") return ensureApiPathSegment("http://api.localhost");
  const host = window.location.hostname;
  const proto = window.location.protocol;
  const apiHost = host.replace(/^([a-z0-9]+-)?workflows/, "$1api");
  if (apiHost === host) return ensureApiPathSegment(`${proto}//api.${host.split(".").slice(1).join(".")}`);
  return ensureApiPathSegment(`${proto}//${apiHost}`);
}

export function getDashboardUrl(): string {
  const cfg = getRuntimeConfig();
  if (cfg.dashboardUrl) return normalizeUrl(cfg.dashboardUrl);
  if (typeof window === "undefined") return "http://dashboard.localhost";
  const host = window.location.hostname;
  const proto = window.location.protocol;
  const dashHost = host.replace(/^([a-z0-9]+-)?workflows/, "$1dashboard");
  if (dashHost === host) return `${proto}//dashboard.${host.split(".").slice(1).join(".")}`;
  return `${proto}//${dashHost}`;
}
