import { getRuntimeConfig, normalizeUrl } from "@/lib/runtime-config";

export function getApiUrl(): string {
  // GT.B1 — runtime config (window.__MOD_RUNTIME_CONFIG__ / container env)
  // first; host-relative derivation below stays as the last-resort fallback.
  const cfg = getRuntimeConfig();
  if (cfg.apiUrl) return normalizeUrl(cfg.apiUrl);
  if (typeof window === "undefined") return "http://api.localhost";
  const host = window.location.hostname;
  const proto = window.location.protocol;
  const apiHost = host.replace(/^([a-z0-9]+-)?workflows/, "$1api");
  if (apiHost === host) return `${proto}//api.${host.split(".").slice(1).join(".")}`;
  return `${proto}//${apiHost}`;
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
