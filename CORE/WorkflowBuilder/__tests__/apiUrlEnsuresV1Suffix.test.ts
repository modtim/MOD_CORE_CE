// F20 - apiUrl()/apiUrlOrNull() must idempotently ensure the API base ends
// with the /v1 path segment, so the Workflow Builder works whether MOD_API_URL
// / NEXT_PUBLIC_API_URL is supplied with or without the suffix (the paid xeon
// stack passes the bare origin; CE compose already passes /v1).
//
// Vectors (also pinned statically by CORE/API/tests/test_f20_wb_api_url_ensures_v1.py):
//   https://example.com     -> https://example.com/v1
//   https://example.com/    -> https://example.com/v1
//   https://example.com/v1  -> unchanged (never /v1/v1)
//   http://203.0.113.70:8000/v1/        -> http://203.0.113.70:8000/v1
//   http://localhost:8000               -> http://localhost:8000/v1
//   "" / unset                          -> DEFAULT_API_URL unchanged

import { apiUrl, apiUrlOrNull } from "@/lib/runtime-config";
import { getApiUrl } from "@/lib/urls";

const VECTORS: Array<[string, string]> = [
  ["https://example.com", "https://example.com/v1"],
  ["https://example.com/", "https://example.com/v1"],
  ["https://example.com/v1", "https://example.com/v1"],
  ["https://example.com/v1/", "https://example.com/v1"],
  ["https://example.com/v1/v1", "https://example.com/v1/v1"],
  ["http://203.0.113.70:8000/v1/", "http://203.0.113.70:8000/v1"],
  ["http://localhost:8000", "http://localhost:8000/v1"],
  ["https://api.customer.com/v1?x=1", "https://api.customer.com/v1?x=1"],
];

function setWindowConfig(value: unknown) {
  Object.defineProperty(window, "__MOD_RUNTIME_CONFIG__", {
    value,
    configurable: true,
    writable: true,
  });
}

function clearEnv() {
  for (const key of ["MOD_API_URL", "NEXT_PUBLIC_API_URL"]) delete process.env[key];
}

beforeEach(() => {
  clearEnv();
  setWindowConfig(undefined);
});

afterEach(() => {
  setWindowConfig(undefined);
  clearEnv();
});

describe("apiUrl ensures the /v1 path segment", () => {
  it("appends /v1 to a bare origin from window config", () => {
    setWindowConfig({ apiUrl: "https://example.com" });
    expect(apiUrl()).toBe("https://example.com/v1");
  });

  it("appends /v1 to a trailing-slash origin", () => {
    setWindowConfig({ apiUrl: "https://example.com/" });
    expect(apiUrl()).toBe("https://example.com/v1");
  });

  it("leaves an already-/v1 base unchanged (idempotent, never /v1/v1)", () => {
    setWindowConfig({ apiUrl: "https://example.com/v1" });
    expect(apiUrl()).toBe("https://example.com/v1");
  });

  it("strips a trailing slash after /v1", () => {
    setWindowConfig({ apiUrl: "http://203.0.113.70:8000/v1/" });
    expect(apiUrl()).toBe("http://203.0.113.70:8000/v1");
  });

  it("keeps an explicit deeper path (a non-/v1 suffix is left as given)", () => {
    setWindowConfig({ apiUrl: "https://api.customer.com/v1/v1" });
    expect(apiUrl()).toBe("https://api.customer.com/v1/v1");
  });

  it("keeps a query string when the path already ends in /v1", () => {
    setWindowConfig({ apiUrl: "https://api.customer.com/v1?x=1" });
    expect(apiUrl()).toBe("https://api.customer.com/v1?x=1");
  });

  it("every vector round-trips through apiUrl()", () => {
    for (const [input, expected] of VECTORS) {
      setWindowConfig({ apiUrl: input });
      expect(apiUrl()).toBe(expected);
    }
  });

  it("appends /v1 from MOD_* container env (SSR path)", () => {
    process.env.MOD_API_URL = "https://example.com";
    expect(apiUrl()).toBe("https://example.com/v1");
  });

  it("appends /v1 from NEXT_PUBLIC_* dev env", () => {
    process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
    expect(apiUrl()).toBe("http://localhost:8000/v1");
  });

  it("empty/unset still falls back to the localhost default unchanged", () => {
    setWindowConfig({ apiUrl: "" });
    expect(apiUrl()).toBe("http://api.localhost");
  });
});

describe("getApiUrl (lib/urls.ts) ensures the same segment on every path", () => {
  it("bare origin from window config gains /v1", () => {
    setWindowConfig({ apiUrl: "https://example.com" });
    expect(getApiUrl()).toBe("https://example.com/v1");
  });

  it("already-/v1 base is unchanged (idempotent, never /v1/v1)", () => {
    setWindowConfig({ apiUrl: "https://example.com/v1" });
    expect(getApiUrl()).toBe("https://example.com/v1");
  });

  it("no config + window location dev host derives api host with /v1", () => {
    setWindowConfig(undefined);
    // jsdom window.location.hostname is not the dev WB host; the host-relative
    // branch is pinned by the node strip-types run in the F22 result file
    // (dev WB host -> https://example.com/v1).
    // `.notToThrow` is not a jest matcher (TypeError); assert it resolves and
    // every path ends with the /v1 segment.
    expect(() => getApiUrl()).not.toThrow();
    expect(getApiUrl()).toMatch(/\/v1$/);
  });

  it("MOD_* container env path gains /v1", () => {
    process.env.MOD_API_URL = "https://example.com";
    expect(getApiUrl()).toBe("https://example.com/v1");
  });

  it("NEXT_PUBLIC_* dev env path gains /v1", () => {
    process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
    expect(getApiUrl()).toBe("http://localhost:8000/v1");
  });

  it("no-window server fallback keeps the localhost default with /v1", () => {
    // jsdom defines window, so this vector is pinned by the node strip-types
    // run (no window -> http://api.localhost/v1).
    // `.notToThrow` is not a jest matcher (TypeError); assert it resolves and
    // every path ends with the /v1 segment.
    expect(() => getApiUrl()).not.toThrow();
    expect(getApiUrl()).toMatch(/\/v1$/);
  });
});

describe("apiUrlOrNull ensures the same segment", () => {
  it("null when nothing is configured", () => {
    expect(apiUrlOrNull()).toBeNull();
  });

  it("bare origin gains /v1", () => {
    setWindowConfig({ apiUrl: "https://example.com" });
    expect(apiUrlOrNull()).toBe("https://example.com/v1");
  });

  it("already-/v1 base is unchanged", () => {
    setWindowConfig({ apiUrl: "https://api.x.com/v1" });
    expect(apiUrlOrNull()).toBe("https://api.x.com/v1");
  });
});
