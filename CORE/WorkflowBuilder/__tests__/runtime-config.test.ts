// GT.B1 — unit tests for lib/runtime-config.ts.
//
// Covers: window.__MOD_RUNTIME_CONFIG__ wins over env; env fallback when no
// window config; missing config falls back to defaults without throwing;
// trailing-slash normalisation.

import {
  getRuntimeConfig,
  apiUrl,
  dashboardUrl,
  apiUrlOrNull,
  dashboardUrlOrNull,
  normalizeUrl,
} from "@/lib/runtime-config";

type Mod = typeof import("@/lib/runtime-config");

let mod: Mod;

const ENV_KEYS = [
  "NEXT_PUBLIC_API_URL",
  "NEXT_PUBLIC_DASHBOARD_URL",
  "MOD_API_URL",
  "MOD_DASHBOARD_URL",
] as const;

function clearEnv() {
  for (const key of ENV_KEYS) delete process.env[key];
}

function setWindowConfig(value: unknown) {
  Object.defineProperty(window, "__MOD_RUNTIME_CONFIG__", {
    value,
    configurable: true,
    writable: true,
  });
}

beforeEach(async () => {
  clearEnv();
  setWindowConfig(undefined);
  jest.resetModules();
  mod = await import("@/lib/runtime-config");
});

afterEach(() => {
  setWindowConfig(undefined);
  clearEnv();
});

describe("getRuntimeConfig", () => {
  it("reads window.__MOD_RUNTIME_CONFIG__", () => {
    setWindowConfig({
      apiUrl: "https://api.customer.com/v1",
      dashboardUrl: "https://dash.customer.com",
    });
    expect(mod.getRuntimeConfig()).toEqual({
      apiUrl: "https://api.customer.com/v1",
      dashboardUrl: "https://dash.customer.com",
    });
  });

  it("window config wins over NEXT_PUBLIC_* env", () => {
    setWindowConfig({ apiUrl: "https://api.window.com" });
    process.env.NEXT_PUBLIC_API_URL = "http://api.env.com";
    process.env.NEXT_PUBLIC_DASHBOARD_URL = "http://dash.env.com";
    expect(mod.getRuntimeConfig()).toEqual({
      apiUrl: "https://api.window.com",
      dashboardUrl: "http://dash.env.com",
    });
  });

  it("falls back to NEXT_PUBLIC_* env when no window config", () => {
    process.env.NEXT_PUBLIC_API_URL = "http://api.env.com/";
    process.env.NEXT_PUBLIC_DASHBOARD_URL = "http://dash.env.com";
    expect(mod.getRuntimeConfig()).toEqual({
      apiUrl: "http://api.env.com/",
      dashboardUrl: "http://dash.env.com",
    });
  });

  it("reads MOD_* container env (server SSR path) when no window config", () => {
    process.env.MOD_API_URL = "https://api.customer.com/v1/";
    process.env.MOD_DASHBOARD_URL = "https://dash.customer.com";
    expect(mod.getRuntimeConfig()).toEqual({
      apiUrl: "https://api.customer.com/v1/",
      dashboardUrl: "https://dash.customer.com",
    });
  });

  it("MOD_* env wins over NEXT_PUBLIC_* env (container value is authoritative)", () => {
    process.env.NEXT_PUBLIC_API_URL = "http://api.env.com";
    process.env.MOD_API_URL = "https://api.customer.com/v1";
    expect(mod.getRuntimeConfig().apiUrl).toBe("https://api.customer.com/v1");
  });

  it("window config still wins over MOD_* env", () => {
    setWindowConfig({ apiUrl: "https://api.window.com" });
    process.env.MOD_API_URL = "https://api.mod.com";
    expect(mod.apiUrl()).toBe("https://api.window.com/v1");
  });

  it("returns empty config without throwing when nothing is set", () => {
    expect(() => mod.getRuntimeConfig()).not.toThrow();
    expect(mod.getRuntimeConfig()).toEqual({});
  });

  it("ignores malformed window config without throwing", () => {
    setWindowConfig("not-an-object");
    expect(() => mod.getRuntimeConfig()).not.toThrow();
    expect(mod.getRuntimeConfig()).toEqual({});

    setWindowConfig({ apiUrl: 42, dashboardUrl: ["x"] });
    expect(mod.getRuntimeConfig()).toEqual({});
  });
});

describe("apiUrl / dashboardUrl", () => {
  // apiUrl() always ends in /v1 (F20)
  it("normalises trailing slashes from window config", () => {
    setWindowConfig({ apiUrl: "https://api.customer.com///", dashboardUrl: "https://dash.customer.com/" });
    expect(mod.apiUrl()).toBe("https://api.customer.com/v1");
    expect(mod.dashboardUrl()).toBe("https://dash.customer.com");
  });

  it("normalises trailing slashes from env fallback", () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.env.com/";
    expect(mod.apiUrl()).toBe("https://api.env.com/v1");
  });

  it("falls back to localhost defaults without throwing when nothing is set", () => {
    expect(mod.apiUrl()).toBe("http://api.localhost");
    expect(mod.dashboardUrl()).toBe("http://dashboard.localhost");
  });

  it("empty-string values fall back to defaults", () => {
    setWindowConfig({ apiUrl: "", dashboardUrl: "" });
    process.env.NEXT_PUBLIC_API_URL = "";
    expect(mod.apiUrl()).toBe("http://api.localhost");
    expect(mod.dashboardUrl()).toBe("http://dashboard.localhost");
  });
});

describe("apiUrlOrNull / dashboardUrlOrNull", () => {
  it("returns null when nothing is configured", () => {
    expect(mod.apiUrlOrNull()).toBeNull();
    expect(mod.dashboardUrlOrNull()).toBeNull();
  });

  it("returns the value (normalised) when configured", () => {
    setWindowConfig({ apiUrl: "https://api.x.com/", dashboardUrl: "https://d.x.com" });
    expect(mod.apiUrlOrNull()).toBe("https://api.x.com/v1");
    expect(mod.dashboardUrlOrNull()).toBe("https://d.x.com");
  });
});

describe("normalizeUrl", () => {
  it("strips trailing slashes and whitespace", () => {
    expect(normalizeUrl("https://x.y/a/b/// ")).toBe("https://x.y/a/b");
  });

  it("returns empty string for null/undefined/blank", () => {
    expect(normalizeUrl(null)).toBe("");
    expect(normalizeUrl(undefined)).toBe("");
    expect(normalizeUrl("   ")).toBe("");
  });

  it("keeps a bare-slash URL as empty (no scheme to preserve)", () => {
    expect(normalizeUrl("/")).toBe("");
  });
});
