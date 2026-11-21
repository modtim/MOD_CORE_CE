"use client";
import { apiUrl } from "@/lib/runtime-config";
// 2400UI41 — CE ships no /catalog router (CE main.py _CATALOG_ROUTERS=[]), so
// opening /store/node/{id} in CE must not even fetch there. Same fail-open
// gating as 14cbda7e5 (assistant-launcher): skip ONLY on a positive edition
// "ce"; an unresolved/errored entitlements block leaves edition null.

// GG.UI.2 — generalized store listing detail page (node|workflow|stack|bundle).
// Data comes from the no-auth-visible GET /catalog/{kind}/{id}; action
// buttons (get/buy/redeem/owned/trial/plan) are driven ONLY by the `actions`
// array the API derives, so this page never re-implements pricing logic.
//
// GG.UI.3 — Buy flow: confirm modal with the live /quote closure summary,
// pay-by-card (Stripe redirect, pending-checkout round-trip handled on return)
// or pay-with-credits. The post-Stripe-redirect ownership poll goes through
// the shared usePollingBackoff hook (2s base, 10s cap, 2 min window).

import { useCallback, useEffect, useRef, useState } from "react";
import { pollErrorForResponse } from "@/lib/poller";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { getAccessToken } from "@/lib/api-client";
// GSEC FX.95 p4b — Verified publisher badge on the listing detail page. One
// component with the store cards (node-chips, same vocabulary as CeBadge).
import { VerifiedBadge } from "@/components/node-chips";
// 2400UI41/p73b3 — the CE degraded state carries the same upgrade CTA chip the
// rest of the WB uses (PaidLock.tsx LockedBadge: "Not part of this edition" + upgrade
// link to `${dashboardUrl}/#billing`).
import { LockedBadge } from "@/components/PaidLock";
// 2400UI41/p73b3 — edition helper (lib/entitlements.ts:22 `edition?: "ce"|"pro"|"saas"`).
// Never throws: fetch failure / unreadable block -> null -> edition unresolved.
import { loadEntitlements } from "@/lib/entitlements";
// 23.1a.s4 — the node detail page is this page (app/nodes/[id] redirects here),
// so the variant verdict list + per-variant code editor live on it for kind=node.
import VariantVerdictList from "@/components/variant-verdict-list";
import VariantCodeEditor from "@/components/variant-code-editor";
// Navigation seam (jsdom cannot intercept location.href) — see nav.ts.
import { nav } from "./nav";
// Checkout seam (IMPL-CE-ANS60-BUILDER) — the Buy-flow fetches live in a
// sibling module so the jest tests can drive them without clicking the Buy
// button, which owner answer 60 hides in CE. See checkout.ts.
import { isProFeature402, startCheckout, submitCredits } from "./checkout";

const PENDING_CHECKOUT_KEY = "mod_store_pending_checkout";
const PENDING_TIMEOUT_MS = 120_000;
const PENDING_BASE_MS = 2_000;
const PENDING_MAX_MS = 10_000;

interface CatalogVersion {
  id: number;
  version: number | string;
  price_cents?: number | null;
  status?: string;
  // stack versions only (`_stack_versions`): slug + license_class.
  slug?: string | null;
  license_class?: string | null;
}

interface ClosureItem {
  kind: string;
  ref_id: number;
  name?: string;
  price_cents?: number | null;
}

interface CatalogDetail {
  kind: string;
  id: number;
  name: string;
  description?: string | null;
  price_cents?: number | null;
  price_tier?: string | null;
  status?: string | null;
  visibility?: string | null;
  owned: boolean;
  owned_via?: string | null;
  versions: CatalogVersion[] | null;
  closure: { owned: ClosureItem[]; covered_by_bundle: ClosureItem[]; to_buy: ClosureItem[] } | null;
  actions: string[];
  // GSEC FX.95 p4a/p4b — Verified-badge source (schemas.CatalogItem): the
  // listing's publisher tenant has a 'verified' PublisherVerification record.
  // GET /catalog/{kind}/{id} reuses the list row, so this is present on every
  // kind's detail payload (node rows report the CALLER tenant's own state,
  // matching /catalog/mine). Absent on pre-FX.95 payloads -> no badge. NOT
  // the same as MOD-built: gov compliance profiles (p5c) accept MOD-built
  // listings only, so a Verified badge never means "allowed for gov".
  publisher_verified?: boolean | null;
  // Stack-only detail fields (GF.10a, `_stack_detail_fields`); absent for
  // every other kind.
  version?: string | number | null;
  gpu_requirements?: Record<string, unknown> | string | null;
  license_class?: string | null;
  licence?: string | null;
  // 13.5f — the tenant secrets the pinned package declares (stack detail
  // only, names/kinds/descriptions — never values).
  required_secrets?: {
    name: string;
    kind: string;
    services: string[];
    required: boolean;
    description?: string | null;
  }[] | null;
}

// POST /catalog/{kind}/{id}/quote — what this tenant still needs to buy.
interface QuoteLine {
  kind: string;
  id: number;
  name?: string | null;
  price_cents?: number | null;
  status: string; // "to_buy" | "owned" | "covered_by_bundle"
}

interface Quote {
  kind?: string;
  id?: number;
  total: number;
  currency?: string;
  unit?: string;
  lines: QuoteLine[];
}

interface PendingCheckout {
  kind: string;
  id: string;
  session_id?: string | null;
}

type CheckoutStatus = "processing" | "success" | "canceled" | "timeout";

function dollars(cents?: number | null): string {
  return cents ? `$${(cents / 100).toFixed(2)}` : "Free";
}

// Stack-only: `gpu_requirements` is a free-form JSONB on StackPackage —
// render best-effort ("2× CUDA 48 GB" shape), same rules as the stack
// marketplace tab.
function formatGpuNeeds(req: CatalogDetail["gpu_requirements"]): string {
  if (!req) return "No GPU required";
  if (typeof req === "string") return req;
  const r = req as Record<string, unknown>;
  // An empty object means the same thing as null: nothing required.
  if (!Object.keys(r).length) return "No GPU required";
  // Detect per-service map: top-level values are all objects. A service value
  // of null means "no GPU for that service"; arrays and flat scalar values
  // (number/string) are not service entries, so they stay on the legacy path.
  const isMap = Object.keys(r).length > 0 && Object.values(r).every((v: unknown) =>
    v === null || v === undefined || (typeof v === "object" && !Array.isArray(v)));
  if (isMap) {
    // Prefix service names only when there is more than one service, so a
    // single-service payload renders bare ("2× cuda 48 GB").
    const multi = Object.keys(r).length > 1;
    const parts: string[] = [];
    for (const [svc, v] of Object.entries(r)) {
      if (v === null || v === undefined) continue; // that service needs no GPU
      const entry = v as Record<string, unknown>;
      const count = entry.count ?? entry.gpu_count ?? entry.quantity;
      const accel = entry.accelerator ?? entry.kind ?? entry.type;
      const vram = entry.vram_gb ?? entry.min_vram_gb ?? entry.vram;
      const svcParts: string[] = [];
      if (count && accel) svcParts.push(`${count}× ${accel}`);
      else if (accel) svcParts.push(String(accel));
      if (vram) svcParts.push(`${vram} GB`);
      const text = svcParts.join(" ");
      if (!text) continue;
      parts.push(multi ? `${svc}: ${text}` : text);
    }
    if (!parts.length) return "No GPU required";
    return parts.join(", ");
  }
  // legacy flat-object path: keep working for non-map shapes.
  const count = r.count ?? r.gpu_count ?? r.quantity;
  const accel = r.accelerator ?? r.kind ?? r.type;
  const vram = r.vram_gb ?? r.min_vram_gb ?? r.vram;
  const parts: string[] = [];
  if (count && accel) parts.push(`${count}× ${accel}`);
  else if (accel) parts.push(String(accel));
  if (vram) parts.push(`${vram} GB`);
  if (parts.length) return parts.join(" ");
  try { return JSON.stringify(req); } catch { return String(req); }
}

function token(): string | null {
  try { return getAccessToken(); } catch { return null; }
}

function authHeaders(): Record<string, string> {
  const t = token();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function spValue(v: string | string[] | undefined): string | null {
  return Array.isArray(v) ? (v[0] ?? null) : (v ?? null);
}

// Navigation seam lives in the sibling ./nav module (see nav.ts): Next's
// generated app-router type guard for this page module only allows the entry
// shapes (default + the known config props) to be exported here.

function contactPublisherMessage(dependency?: string | null): string {
  return dependency
    ? `This action can't be completed because its dependency "${dependency}" is no longer available. Please contact the publisher for assistance.`
    : "This action can't be completed because a dependency is no longer available. Please contact the publisher for assistance.";
}

// Next 15's app-router passes `params`/`searchParams` as Promises, and the
// generated .next/types guard for this page requires the PageProps shape
// (params?: Promise<SegmentParams>, searchParams?: Promise<any>) — hence the
// loose `any` props below: a concrete `{ kind, id }` object does NOT satisfy
// that constraint, so the guard rejects the old plain-object signature. The
// jest tests render this component directly with plain values, so both shapes
// are accepted here; a Promise cannot be unwrapped synchronously, so the
// segment values fall back to the URL the browser is already on.
// IMPL-CE-ANS60-BUILDER — isProFeature402 moved to ./checkout.ts (single
// definition; the checkout tests drive it through the seam). Imported above.
function segmentParams(params: unknown): { kind: string; id: string } {
  if (params && !(params instanceof Promise)) {
    const p = params as Record<string, unknown>;
    if (typeof p.kind === "string" && typeof p.id === "string") {
      return { kind: p.kind, id: p.id };
    }
  }
  try {
    const m = /^\/store\/([^/]+)\/([^/?#]+)/.exec(window.location.pathname);
    if (m) return { kind: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]) };
  } catch { /* noop — SSR/no-window */ }
  return { kind: "", id: "" };
}

function queryParams(sp: unknown): Record<string, string | string[] | undefined> {
  if (sp && !(sp instanceof Promise)) return sp as Record<string, string | string[] | undefined>;
  return {};
}

export default function StoreDetailPage({
  params,
  searchParams,
}: {
  params: any;
  searchParams?: any;
}) {
  const { kind, id } = segmentParams(params);
  // 2400GX-storeB 30.5.3 — /catalog/{kind}/{id} is a CE route (browse, detail
  // and free claim run on Community Edition). A PAID action on CE gets the
  // API's 402 "Pro feature" detail and renders a CTA, not an error.
  const [proFeature, setProFeature] = useState(false);
  const [item, setItem] = useState<CatalogDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  // 2400UI41/p73b3 — CE degraded state. TRUE only when the entitlements block
  // POSITIVELY reports edition "ce" (CE ships no /catalog router, so the detail
  // payload never exists there). Unresolved/errored block -> edition null ->
  // stays false -> today's fetch/404 behaviour is untouched (fail-open).
  const [ceDegraded, setCeDegraded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [redeemOpen, setRedeemOpen] = useState(false);

  // 23.1a.s4 — variant verdicts + per-variant code editing (kind=node only).
  // The editor modal is opened from a verdict row; after a save the user
  // bumps variantReloadKey, which re-reads the verdict list so the row shows
  // the new revision, and then hits that row's "Re-verify this variant only".
  const [variantEdit, setVariantEdit] = useState<{ variant: string; filename: string } | null>(null);
  const [variantSaved, setVariantSaved] = useState<string | null>(null);
  const [variantReloadKey, setVariantReloadKey] = useState(0);
  // Filenames per variant, from GET /nodes/{id}/source — the code editor needs
  // a filename and the verdict list only knows the variant, so the page reads
  // the same source document the editor reads (node kind only).
  const [variantFiles, setVariantFiles] = useState<Record<string, string[]>>({});

  const loadVariantFiles = useCallback(async () => {
    try {
      const r = await fetch(`${apiUrl()}/nodes/${id}/source`, { headers: authHeaders() });
      if (!r.ok) { setVariantFiles({}); return; }
      const doc = (await r.json()) as Record<string, unknown>;
      const variants = (doc.variants ?? {}) as Record<string, unknown>;
      setVariantFiles(Object.fromEntries(
        Object.entries(variants).map(([name, raw]) => {
          const entry = (raw ?? {}) as Record<string, unknown>;
          const files = Array.isArray(entry.files) ? entry.files : [];
          return [
            name,
            files.map((f: unknown) => String((f as Record<string, unknown>).filename ?? "")),
          ];
        }),
      ));
    } catch {
      setVariantFiles({});
    }
  }, [id]);

  useEffect(() => {
    setVariantFiles({});
    if (kind === "node" && item) void loadVariantFiles();
  }, [kind, item, loadVariantFiles]);

  // 13.5f — "Secrets needed" install step: values the user supplies for
  // the package's declared secrets (masked inputs, cleared after submit)
  // plus the tenant's existing secret names (GET /secrets — names only).
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [existingSecretNames, setExistingSecretNames] = useState<Set<string> | null>(null);

  // Checkout modal state.
  const [checkoutOpen, setCheckoutOpen] = useState(false);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [checkoutBusy, setCheckoutBusy] = useState<"card" | "credits" | null>(null);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);

  // Stripe return-trip state.
  const [pending, setPending] = useState<PendingCheckout | null>(null);
  const [checkoutStatus, setCheckoutStatus] = useState<CheckoutStatus | null>(null);
  const pendingStartedAtRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      // 2400UI41/p73b3 — short-circuit BEFORE any fetch, ONLY on a positive
      // edition "ce" (same fail-open predicate as 14cbda7e5 assistant-launcher:
      // loadEntitlements() never throws; unresolved/errored -> null -> edition
      // null -> we do NOT skip, so a paid install keeps its /catalog fetch and
      // its 404 -> notFound path).
      const block = await loadEntitlements();
      if (block?.edition === "ce") {
        setCeDegraded(true);
        return;
      }
      const r = await fetch(`${apiUrl()}/catalog/${kind}/${id}`, { headers: authHeaders() });
      if (r.status === 404) { setNotFound(true); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setItem(await r.json());
    } catch (e) {
      setError((e as Error).message);
    }
  }, [kind, id]);

  useEffect(() => {
    setItem(null); setError(null); setNotFound(false); setCeDegraded(false); setMsg(null); setProFeature(false);
    setCode(""); setRedeemOpen(false);
    load();
  }, [load]);

  // --- Stripe return-trip: detect ?checkout=success|canceled or a stored
  // pending entry for THIS item, then poll until the webhook fulfils it. ---
  const clearPending = useCallback(() => {
    try { sessionStorage.removeItem(PENDING_CHECKOUT_KEY); } catch { /* noop */ }
    setPending(null);
  }, []);

  useEffect(() => {
    // Prefer the prop (Next re-renders with it on client-side navigation);
    // fall back to window.location.search only when the prop is empty.
    let query: Record<string, string | string[] | undefined> = { ...queryParams(searchParams) };
    try {
      if (typeof window !== "undefined" && Object.keys(query).length === 0) {
        query = Object.fromEntries(new URLSearchParams(window.location.search));
      }
    } catch { /* noop — URLSearchParams/SSR edge */ }
    const canceled = spValue(query["checkout"]) === "canceled" || spValue(query["canceled"]) === "true";
    const success = spValue(query["checkout"]) === "success";

    let entry: PendingCheckout | null = null;
    try {
      const raw = sessionStorage.getItem(PENDING_CHECKOUT_KEY);
      if (raw) {
        const p = JSON.parse(raw) as PendingCheckout | null;
        if (p && p.kind === kind && String(p.id) === String(id)) entry = p;
      }
    } catch { entry = null; }

    if (canceled) {
      clearPending();
      setCheckoutStatus("canceled");
      return;
    }
    if (success || entry) {
      pendingStartedAtRef.current = Date.now();
      setPending(entry ?? { kind, id, session_id: null });
      setCheckoutStatus("processing");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, id, searchParams]);

  useEffect(() => {
    pendingStartedAtRef.current = pending ? (pendingStartedAtRef.current ?? Date.now()) : null;
  }, [pending]);

  const pollForOwnership = useCallback(async () => {
    const started = pendingStartedAtRef.current;
    if (started !== null && Date.now() - started > PENDING_TIMEOUT_MS) {
      clearPending();
      setCheckoutStatus("timeout");
      return;
    }
    const r = await fetch(`${apiUrl()}/catalog/${kind}/${id}`, { headers: authHeaders() });
    const pollErr = pollErrorForResponse(r);
    if (pollErr) throw pollErr;
    const body = (await r.json()) as CatalogDetail;
    if (body && (body.owned || (body.actions || []).includes("owned"))) {
      setItem(body);
      clearPending();
      setCheckoutStatus("success");
    }
  }, [clearPending, kind, id]);

  usePollingBackoff(
    pollForOwnership,
    {
      baseMs: PENDING_BASE_MS,
      maxMs: PENDING_MAX_MS,
      jitter: false,
      // Never hard-stop during the 2-minute confirmation window; the timeout
      // above ends the poll. (The shared default of 5 would fire the global
      // connection-lost banner on a slow webhook fulfilment.)
      maxFailures: 99_999,
      enabled: checkoutStatus === "processing" && pending !== null,
    },
    [kind, id, pending, checkoutStatus],
  );

  // 13.5f — the package's REQUIRED tenant secrets (optional ones never
  // block the install; they just stay unresolved until the deploy).
  const requiredSecrets =
    kind === "stack" && Array.isArray(item?.required_secrets)
      ? (item!.required_secrets!.filter((s) => s.required))
      : [];

  // Tenant's existing secret names (GET /secrets — names only, never
  // values) so the "Secrets needed" step can offer "use existing".
  const requiredNamesKey = requiredSecrets.map((s) => s.name).join(",");
  useEffect(() => {
    setSecretValues({});
    setExistingSecretNames(null);
    if (requiredNamesKey === "") return;
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`${apiUrl()}/secrets/`, { headers: authHeaders() });
        if (!r.ok) return;
        const rows = (await r.json()) as { name?: string | null }[];
        if (alive) setExistingSecretNames(new Set(rows.map((x) => x.name ?? "")));
      } catch { /* names-only convenience — the inputs still work */ }
    })();
    return () => { alive = false; };
  }, [requiredNamesKey]);

  const secretSatisfied = (name: string): boolean =>
    Boolean((existingSecretNames?.has(name) ?? false) || secretValues[name]?.trim());
  const unsatisfiedSecrets = requiredSecrets.filter((s) => !secretSatisfied(s.name));

  // Post-purchase success actions: Open deep-links the WB with ?workflow=<id>
  // (useOpenWorkflowFromQuery on the main page); Install materializes an
  // owned stack into Persistent Stacks (GF.12). 13.5f — the install POST
  // carries the secrets the user supplied (existing ones are sent nothing:
  // the API uses the tenant's own rows as-is).
  const installOwnedStack = useCallback(async () => {
    if (kind !== "stack" || !item) return;
    const reqs = (Array.isArray(item.required_secrets) ? item.required_secrets : [])
      .filter((s) => s.required);
    const missing = reqs.filter(
      (s) => !((existingSecretNames?.has(s.name) ?? false) || secretValues[s.name]?.trim())
    ).map((s) => s.name);
    if (missing.length > 0) {
      setMsg(`Add a value for: ${missing.join(", ")}`);
      return;
    }
    setBusy(true);
    try {
      const secrets: Record<string, string> = {};
      for (const s of reqs) {
        if (!(existingSecretNames?.has(s.name) ?? false)) {
          const v = secretValues[s.name]?.trim();
          if (v) secrets[s.name] = v;
        }
      }
      const r = await fetch(`${apiUrl()}/catalog/stack/${id}/install`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(Object.keys(secrets).length > 0 ? { secrets } : {}),
      });
      const body = (await r.json().catch(() => ({}))) as {
        detail?: string | { missing_secrets?: string[] };
      };
      if (!r.ok) {
        const d = body?.detail;
        const names = d && typeof d === "object" && Array.isArray(d.missing_secrets)
          ? d.missing_secrets.join(", ")
          : null;
        throw new Error(
          names ? `Missing secrets: ${names}` : (typeof d === "string" ? d : `HTTP ${r.status}`));
      }
      setMsg("Stack installed to Persistent Stacks.");
    } catch (e) {
      setMsg(`Install failed: ${(e as Error).message}`);
    } finally {
      setSecretValues({});
      setBusy(false);
    }
  }, [kind, id, item, existingSecretNames, secretValues]);

  const openInBuilder = useCallback(() => {
    if (kind === "workflow") {
      nav.go(`/?workflow=${id}`);
      return;
    }
    if (kind === "bundle") {
      const closure = item?.closure;
      const workflowLeaf = closure
        ? [...closure.owned, ...closure.covered_by_bundle].find((c) => c.kind === "workflow")
        : undefined;
      nav.go(workflowLeaf ? `/?workflow=${workflowLeaf.ref_id}` : "/");
      return;
    }
    // kind === "node" — nothing to open as a workflow.
    nav.go("/");
  }, [kind, id, item]);

  const requireLogin = (): boolean => {
    if (token()) return true;
    setMsg("Please log in to complete this action.");
    return false;
  };

  const claim = async () => {
    if (!requireLogin()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch(`${apiUrl()}/catalog/${kind}/${id}/get`, {
        method: "POST", headers: authHeaders(),
      });
      const body = await r.json().catch(() => ({}));
      if (isProFeature402(r.status, body)) { setProFeature(true); return; }
      if (!r.ok) throw new Error(body?.detail || `HTTP ${r.status}`);
      setMsg("Claimed — you now own this listing.");
      await load();
    } catch (e) { setMsg(`Claim failed: ${(e as Error).message}`); }
    finally { setBusy(false); }
  };

  const redeem = async () => {
    if (!requireLogin() || !code.trim()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch(`${apiUrl()}/codes/redeem`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ code: code.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body?.detail || "Redemption failed");
      setCode(""); setRedeemOpen(false);
      setMsg("Code redeemed.");
      await load();
    } catch (e) { setMsg(`Redemption failed: ${(e as Error).message}`); }
    finally { setBusy(false); }
  };

  // --- Buy flow (GG.UI.3) -------------------------------------------------

  // IMPL-CE-ANS60-BUILDER — the body moved to checkout.ts:startCheckout so the
  // Pro/402 jest tests can drive the quote fetch without clicking the Buy
  // button (hidden in CE, owner answer 60). Identical behaviour: the page still
  // owns the login guard and passes its own setters.
  const checkoutDeps = {
    apiUrl, authHeaders, isProFeature402, contactPublisherMessage,
    setCheckoutOpen, setCheckoutError, setCheckoutBusy, setQuote, setProFeature,
    setMsg, idempotencyKeyRef, reload: () => load(),
  };

  const openCheckout = async () => {
    if (!requireLogin()) return;
    await startCheckout(kind, id, checkoutDeps);
  };

  const payWithCard = async () => {
    setCheckoutBusy("card");
    setCheckoutError(null);
    try {
      const r = await fetch(`${apiUrl()}/catalog/${kind}/${id}/checkout`, {
        method: "POST", headers: authHeaders(),
      });
      const body = (await r.json().catch(() => ({}))) as {
        checkout_url?: string | null;
        session_id?: string | null;
        amount_cents?: number | null;
        detail?: string;
        action?: string;
        dependency?: string | null;
      };
      if (isProFeature402(r.status, body)) {
        setProFeature(true); setCheckoutOpen(false); return;
      }
      if (r.status === 409) {
        // Unresolvable closure dependency (GF.10b) — refused before any
        // money moves; the user has to reach the publisher, not /get.
        if (body?.action === "contact_publisher") {
          throw new Error(contactPublisherMessage(body.dependency));
        }
        // Nothing to pay — the closure is fully owned/free; claim it as /get.
        const g = await fetch(`${apiUrl()}/catalog/${kind}/${id}/get`, {
          method: "POST", headers: authHeaders(),
        });
        if (!g.ok) {
          const gb = await g.json().catch(() => ({}));
          throw new Error((gb as { detail?: string })?.detail || "Could not add to your library");
        }
        setCheckoutOpen(false);
        setMsg("Added to your library");
        await load();
        return;
      }
      if (!r.ok) {
        if (r.status === 400) throw new Error("Checkout not available for this item yet.");
        if (r.status === 503) throw new Error("Checkout is currently unavailable. Try again, or pay with credits.");
        throw new Error(body?.detail || `HTTP ${r.status}`);
      }
      if (!body?.checkout_url) throw new Error("Checkout did not return a payment URL.");
      try {
        sessionStorage.setItem(
          PENDING_CHECKOUT_KEY,
          JSON.stringify({ kind, id, session_id: body.session_id ?? null }),
        );
      } catch { /* storage blocked — the return trip just re-polls */ }
      nav.go(body.checkout_url);
    } catch (e) { setCheckoutError((e as Error).message); }
    finally { setCheckoutBusy(null); }
  };

  // IMPL-CE-ANS60-BUILDER — body moved to checkout.ts:submitCredits (same seam
  // as startCheckout) so the credits-402 test can drive it without the Buy
  // button. Behaviour identical.
  const payWithCredits = async () => {
    await submitCredits(kind, id, checkoutDeps);
  };

  if (error) return (
    <main className="max-w-3xl mx-auto p-8">
      <div data-testid="store-detail" className="text-red-600">Something went wrong: {error}</div>
    </main>
  );
  // 2400UI41/p73b3 — CE degraded state: no /catalog router ships there, so the
  // name-bearing header is built from the {kind,id} path the visitor asked for,
  // plus the shared upgrade CTA. No payload, no fetch, no invented pricing.
  if (ceDegraded) return (
    <main className="max-w-3xl mx-auto p-8">
      <div data-testid="store-detail" className="text-gray-600">
        <div className="flex items-center gap-2 flex-wrap">
          <h1 className="text-3xl font-bold text-gray-900" data-testid="store-detail-name">
            {id ? `${kind} · ${id}` : kind}
          </h1>
          <LockedBadge label="Store listings" />
        </div>
        <p>
          Community Edition ships no catalog service, so this listing&apos;s details and
          purchase actions are unavailable here.
        </p>
      </div>
    </main>
  );
  if (notFound) return (
    <main className="max-w-3xl mx-auto p-8">
      <div data-testid="store-detail" className="text-gray-600">
        <h1 className="text-xl font-semibold text-gray-900 mb-2">Not found</h1>
        <p>Listing not found or not available to you.</p>
      </div>
    </main>
  );
  if (!item) return (
    <main className="max-w-3xl mx-auto p-8">
      <div data-testid="store-detail" className="text-gray-500">Loading…</div>
    </main>
  );

  const actions = item.actions || [];
  const owned = actions.includes("owned") || item.owned;
  const canGet = actions.includes("get");
  const canBuy = actions.includes("buy");
  const hasTrial = actions.includes("trial");
  const hasPlan = actions.includes("plan");

  const closure = item.closure;
  const closureOwned = closure ? closure.owned.length + closure.covered_by_bundle.length : 0;
  const closureToBuy = closure ? closure.to_buy.length : 0;
  const closureNames = closure
    ? closure.to_buy.map((i) => i.name).filter((n): n is string => Boolean(n)).join(", ")
    : "";

  return (
    <main className="max-w-3xl mx-auto p-8" data-testid="store-detail">
      {checkoutStatus === "processing" && (
        <div
          data-testid="store-checkout-pending"
          className="mb-6 border border-indigo-200 bg-indigo-50 text-indigo-800 rounded-md px-4 py-3 text-sm"
        >
          Processing… we'll confirm here.
        </div>
      )}
      {checkoutStatus === "success" && (
        <div
          data-testid="store-checkout-success"
          className="mb-6 border border-green-200 bg-green-50 text-green-800 rounded-md px-4 py-3 text-sm flex items-center justify-between gap-4 flex-wrap"
        >
          <span>Added — this item is in your library.</span>
          <span className="flex items-center gap-2">
            {kind === "stack" && (
              <button
                data-testid="store-success-install"
                onClick={installOwnedStack}
                disabled={busy || unsatisfiedSecrets.length > 0}
                className="px-4 py-1.5 bg-green-600 text-white rounded-md font-semibold hover:bg-green-700 disabled:opacity-50"
              >
                Install stack
              </button>
            )}
            {(kind === "workflow" || kind === "bundle" || kind === "node") && (
              <button
                data-testid="store-success-open"
                onClick={openInBuilder}
                className="px-4 py-1.5 border border-green-600 text-green-700 rounded-md font-semibold hover:bg-green-100"
              >
                Open in builder
              </button>
            )}
          </span>
        </div>
      )}
      {/* 13.5f — "Secrets needed": the package's required tenant secrets,
          satisfied by an existing tenant secret (names-only lookup) or a
          masked value the user types. Install is disabled until every one
          is satisfied; the inputs clear after submit. */}
      {kind === "stack" && requiredSecrets.length > 0 && (
        <div
          data-testid="store-secrets-needed"
          className="mb-6 border border-amber-200 bg-amber-50 rounded-md px-4 py-3 text-sm"
        >
          <div className="text-xs font-semibold text-amber-800 uppercase mb-2">
            Secrets needed
          </div>
          <ul className="space-y-2">
            {requiredSecrets.map((s) => (
              <li
                key={s.name}
                data-testid={`store-secret-${s.name}`}
                className="flex items-center gap-2 flex-wrap"
              >
                <span className="font-medium text-amber-900">{s.name}</span>
                {s.description && (
                  <span className="text-amber-700 text-xs">{s.description}</span>
                )}
                {secretSatisfied(s.name) && (
                  <span
                    data-testid={`store-secret-${s.name}-status`}
                    className="text-xs text-green-700"
                  >
                    {existingSecretNames?.has(s.name) ? "✓ existing secret will be used" : "✓"}
                  </span>
                )}
                {!(existingSecretNames?.has(s.name) ?? false) && (
                  <input
                    type="password"
                    data-testid={`store-secret-${s.name}-input`}
                    value={secretValues[s.name] ?? ""}
                    onChange={(e) =>
                      setSecretValues((prev) => ({ ...prev, [s.name]: e.target.value }))
                    }
                    placeholder="value"
                    autoComplete="off"
                    className="px-3 py-1 border border-amber-300 rounded-md text-sm bg-white w-56"
                  />
                )}
              </li>
            ))}
          </ul>
          {unsatisfiedSecrets.length > 0 && (
            <div className="mt-2 text-xs text-amber-800">
              Install stays disabled until every required secret is satisfied.
            </div>
          )}
        </div>
      )}
      {checkoutStatus === "canceled" && (
        <div
          data-testid="store-checkout-canceled"
          className="mb-6 border border-gray-200 bg-gray-50 text-gray-700 rounded-md px-4 py-3 text-sm"
        >
          Checkout canceled.
        </div>
      )}
      {checkoutStatus === "timeout" && (
        <div
          data-testid="store-checkout-timeout"
          className="mb-6 border border-amber-200 bg-amber-50 text-amber-800 rounded-md px-4 py-3 text-sm"
        >
          We couldn't confirm your payment yet. If it completed, refresh this page or check your library.
        </div>
      )}

      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-xs font-semibold text-gray-400 uppercase mb-1">
            {kind}{item.status ? ` · ${item.status}` : ""}{item.visibility ? ` · ${item.visibility}` : ""}
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-3xl font-bold text-gray-900" data-testid="store-detail-name">{item.name}</h1>
            {/* GSEC FX.95 p4b — Verified publisher badge (same component +
                tooltip as the store cards). Renders only when the API says
                the publisher tenant is verified; never implies MOD-built or
                gov-tenant eligibility (p5c gates those separately). */}
            <VerifiedBadge publisherVerified={item.publisher_verified} className="ml-2" />
          </div>
        </div>
        <div className="text-right">
          <div className="text-2xl font-bold text-gray-900">{dollars(item.price_cents)}</div>
          {item.price_tier && <div className="text-xs text-gray-500 uppercase">{item.price_tier}</div>}
        </div>
      </div>

      {item.description && <p className="mt-4 text-gray-700">{item.description}</p>}

      {kind === "stack" && (item.version !== undefined || item.gpu_requirements || item.license_class || item.licence) && (
        <div className="mt-6 grid grid-cols-2 gap-3 text-sm" data-testid="store-stack-details">
          {item.version !== undefined && (
            <div><span className="text-gray-400 uppercase text-xs">Version</span><div className="text-gray-800">{String(item.version)}</div></div>
          )}
          {item.license_class && (
            <div><span className="text-gray-400 uppercase text-xs">License class</span><div className="text-gray-800">{item.license_class}</div></div>
          )}
          {item.licence && (
            <div><span className="text-gray-400 uppercase text-xs">Licence</span><div className="text-gray-800">{item.licence}</div></div>
          )}
          {item.gpu_requirements && (
            <div className="col-span-2"><span className="text-gray-400 uppercase text-xs">GPU requirements</span><div className="text-gray-800">{formatGpuNeeds(item.gpu_requirements)}</div></div>
          )}
        </div>
      )}

      {item.versions && item.versions.length > 0 && (
        <div className="mt-6">
          <div className="text-xs font-semibold text-gray-500 uppercase mb-2">Versions</div>
          <ul className="border rounded-lg divide-y" data-testid="store-versions">
            {item.versions.map((v) => (
              <li key={v.id} className="px-4 py-2 text-sm text-gray-800 flex items-center justify-between">
                <span>
                  v{v.version}
                  {v.status ? <span className="text-gray-400"> · {v.status}</span> : null}
                  {v.slug ? <span className="text-gray-400"> · {v.slug}</span> : null}
                  {v.license_class ? <span className="text-gray-400"> · {v.license_class}</span> : null}
                </span>
                <span>{v.price_cents !== undefined ? dollars(v.price_cents) : ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {closure && (
        <div className="mt-6 border rounded-lg p-4" data-testid="store-closure">
          <div className="text-xs font-semibold text-gray-500 uppercase mb-2">Includes</div>
          <div className="text-sm text-gray-800">
            {closureOwned} item{closureOwned === 1 ? "" : "s"} you already own, {closureToBuy} to buy.
          </div>
          {closureNames && <div className="text-xs text-gray-500 mt-1">{closureNames}</div>}
        </div>
      )}

      {/* 23.1a.s4 — Variants panel (node kind only). The verdict list carries
          the per-row actions (repair / re-verify / edit code); "Edit code"
          opens the code editor modal for THAT variant, and after a save the
          list is re-read so the row shows the new revision before the user
          hits re-verify. */}
      {kind === "node" && (
        <div className="mt-6 border rounded-lg p-4" data-testid="store-variants-panel">
          <div className="text-xs font-semibold text-gray-500 uppercase mb-2">Variants</div>
          <VariantVerdictList
            nodeId={Number(id)}
            apiUrl={apiUrl()}
            reloadKey={String(variantReloadKey)}
            onEditCode={(variant) =>
              setVariantEdit({
                variant,
                filename: (variantFiles[variant] ?? [])[0] ?? "dockerfile",
              })
            }
          />
          <VariantCodeEditor
            nodeId={Number(id)}
            apiUrl={apiUrl()}
            variant={variantEdit?.variant ?? ""}
            filename={variantEdit?.filename ?? ""}
            open={variantEdit !== null}
            onClose={() => setVariantEdit(null)}
            onSaved={(revisionNo) => {
              setVariantSaved(variantEdit?.variant ?? null);
              setVariantReloadKey((k) => k + 1);
              void revisionNo;
            }}
          />
          {variantSaved && (
            <div data-testid="store-variants-saved" className="mt-2 text-sm text-gray-700">
              {variantSaved} code saved — re-verify it to get a fresh verdict.
            </div>
          )}
        </div>
      )}

      <div className="mt-8 flex items-center gap-3 flex-wrap">
        {owned && (
          <span
            data-testid="store-action-owned"
            className="px-5 py-2.5 bg-gray-100 text-gray-600 rounded-md font-semibold"
          >
            Owned{item.owned_via ? ` — via ${item.owned_via}` : ""}
          </span>
        )}
        {canGet && (
          <button
            data-testid="store-action-get"
            onClick={claim}
            disabled={busy}
            className="px-5 py-2.5 bg-indigo-600 text-white rounded-md font-semibold hover:bg-indigo-700 disabled:opacity-50"
          >
            Get (free)
          </button>
        )}
        {proFeature && (
          <span
            data-testid="store-pro-feature"
            className="px-5 py-2.5 bg-amber-100 text-amber-800 rounded-md font-semibold"
          >
            Purchases are not part of MOD Community Edition.
          </span>
        )}
        {false && (
          <button
            data-testid="store-action-buy"
            onClick={openCheckout}
            disabled={busy || checkoutOpen}
            className="px-5 py-2.5 bg-indigo-600 text-white rounded-md font-semibold hover:bg-indigo-700 disabled:opacity-50"
          >
            Buy {dollars(item.price_cents)}
          </button>
        )}
        {false && hasTrial && (
          <span className="px-5 py-2.5 bg-gray-100 text-gray-600 rounded-md font-semibold">
            Trial coming soon
          </span>
        )}
        {hasPlan && (
          <span className="px-5 py-2.5 bg-gray-100 text-gray-600 rounded-md font-semibold">
            Included in your plan
          </span>
        )}
      </div>

      {/* Redeem is backed by POST /codes/redeem (alias of /listing-codes/redeem)
          and is independent of the derived `actions` array: available for any
          non-owned item. */}
      {!owned && (
        <div className="mt-4" data-testid="store-redeem-disclosure">
          <button
            type="button"
            data-testid="store-redeem-toggle"
            onClick={() => setRedeemOpen(!redeemOpen)}
            className="text-sm text-gray-500 hover:text-gray-800 underline"
          >
            Have a code?
          </button>
          {redeemOpen && (
            <div className="mt-2 flex items-center gap-2">
              <input
                data-testid="store-action-redeem-input"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="Redemption code"
                className="px-3 py-2 border rounded-md text-sm"
              />
              <button
                data-testid="store-action-redeem-button"
                onClick={redeem}
                disabled={busy || !code.trim()}
                className="px-4 py-2 border border-indigo-600 text-indigo-600 rounded-md font-semibold hover:bg-indigo-50 disabled:opacity-50"
              >
                Redeem
              </button>
            </div>
          )}
        </div>
      )}

      {msg && <div data-testid="store-action-msg" className="mt-4 text-sm text-gray-700">{msg}</div>}

      {checkoutOpen && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
          <div
            role="dialog"
            aria-modal="true"
            data-testid="store-checkout-modal"
            className="bg-white rounded-lg shadow-xl w-full max-w-md p-6"
          >
            <h2 className="text-lg font-semibold text-gray-900 mb-1">Buy {item.name}</h2>
            <p className="text-xs text-gray-500 mb-4">
              You're charged once for everything below; items you already own are free.
            </p>

            {!quote && <div className="text-sm text-gray-500">Preparing price…</div>}
            {quote && (
              <div>
                <ul className="border rounded-md divide-y mb-3">
                  {quote.lines.map((l, i) => {
                    const included = l.status === "owned" || l.status === "covered_by_bundle";
                    return (
                      <li
                        key={`${l.kind}-${l.id}-${i}`}
                        data-testid="store-checkout-line"
                        className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
                      >
                        <span className="text-gray-900 truncate">{l.name || l.kind}</span>
                        <span className="flex items-center gap-2 shrink-0">
                          <span className="text-xs uppercase text-gray-400">{l.kind}</span>
                          {included ? (
                            <span className="text-gray-500">Owned</span>
                          ) : (
                            <span className="text-gray-900">{dollars(l.price_cents)}</span>
                          )}
                        </span>
                      </li>
                    );
                  })}
                </ul>
                <div
                  data-testid="store-checkout-total"
                  className="flex items-center justify-between text-sm font-semibold text-gray-900 mb-4"
                >
                  <span>Total due</span>
                  <span>{dollars(quote.total)}</span>
                </div>
              </div>
            )}

            <div className="flex flex-col gap-2">
              <button
                data-testid="store-checkout-pay-card"
                onClick={payWithCard}
                disabled={!quote || checkoutBusy !== null}
                className="px-4 py-2.5 bg-indigo-600 text-white rounded-md font-semibold hover:bg-indigo-700 disabled:opacity-50"
              >
                Pay by card
              </button>
              <button
                data-testid="store-checkout-pay-credits"
                onClick={payWithCredits}
                disabled={!quote || checkoutBusy !== null}
                className="px-4 py-2.5 border border-indigo-600 text-indigo-600 rounded-md font-semibold hover:bg-indigo-50 disabled:opacity-50"
              >
                Pay with credits
              </button>
              <button
                data-testid="store-checkout-cancel"
                onClick={() => setCheckoutOpen(false)}
                disabled={checkoutBusy !== null}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900 disabled:opacity-50"
              >
                Cancel
              </button>
            </div>

            {checkoutBusy === "card" && <div className="mt-3 text-sm text-gray-600">Contacting checkout…</div>}
            {checkoutBusy === "credits" && <div className="mt-3 text-sm text-gray-600">Charging credits…</div>}
            {checkoutError && (
              <div data-testid="store-checkout-error" className="mt-3 text-sm text-red-600">
                {checkoutError}
              </div>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
