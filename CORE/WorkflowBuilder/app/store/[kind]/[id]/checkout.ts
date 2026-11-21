/**
 * Checkout seam for the store listing detail page (IMPL-CE-ANS60-BUILDER).
 *
 * Owner answer 60 hides the Buy CTAs in Community Edition, so the jest tests
 * can no longer reach the Buy flow by rendering the page and clicking
 * `store-action-buy` — the button does not exist in the CE tree. The quote
 * fetch and the pay-with-credits POST therefore live in this sibling module
 * (same reason `nav.ts` is a sibling module: Next's generated app-router type
 * guard for `page.tsx` only allows the entry-shape names to be exported from
 * the page module itself, so nothing else may be `export`ed there).
 *
 * Behaviour is unchanged: `page.tsx` keeps its `openCheckout` / `payWithCredits`
 * consts and delegates here with the page's own state setters, so the rendered
 * UI — including the CE Buy-button visibility — is identical.
 */

// The page passes its own state setters and helpers here, so the seam mutates
// exactly the state the page renders. Loose signatures on the setters keep the
// seam independent of the page's useState literal types.
// 2400GX-storeB 30.5.3 — the CE API answers every paid action with 402
// "Pro feature: <x> is not available in MOD Community Edition." The paid
// "insufficient credits" 402 has a different detail, so it stays an error.
// IMPL-CE-ANS60-BUILDER — the guard lives here (not in page.tsx) so the
// checkout tests can drive the seam with the real guard, and page.tsx imports
// it for its other paid-action paths.
export const isProFeature402 = (status: number, body: any): boolean => {
  if (status !== 402) return false;
  const d = typeof body?.detail === "string" ? body.detail : (typeof body === "string" ? body : "");
  return /pro feature|community edition/i.test(d);
};

type CheckoutDeps = {
  apiUrl: () => string;
  authHeaders: () => Record<string, string>;
  isProFeature402: (status: number, body: any) => boolean;
  contactPublisherMessage: (dependency?: string | null) => string;
  setCheckoutOpen: (value: any) => void;
  setCheckoutError: (value: any) => void;
  setCheckoutBusy: (value: any) => void;
  setQuote: (value: any) => void;
  setProFeature: (value: any) => void;
  setMsg: (value: any) => void;
  idempotencyKeyRef: { current: string | null };
  reload: () => Promise<any>;
};

// --- Buy flow (GG.UI.3) -------------------------------------------------

export const startCheckout = async (kind: string, id: string, deps: CheckoutDeps) => {
  const {
    apiUrl, authHeaders, isProFeature402, contactPublisherMessage,
    setCheckoutOpen, setCheckoutError, setQuote, setProFeature, idempotencyKeyRef,
  } = deps;
  setCheckoutOpen(true);
  setCheckoutError(null);
  setQuote(null);
  idempotencyKeyRef.current = crypto.randomUUID();
  try {
    const r = await fetch(`${apiUrl()}/catalog/${kind}/${id}/quote`, {
      method: "POST", headers: authHeaders(),
    });
    const body = (await r.json().catch(() => null)) as { [k: string]: any } | null;
    if (isProFeature402(r.status, body)) {
      setProFeature(true); setCheckoutOpen(false); return;
    }
    if (!r.ok) {
      if (r.status === 409 && body?.action === "contact_publisher") {
        throw new Error(contactPublisherMessage(body.dependency));
      }
      throw new Error(body?.detail || `HTTP ${r.status}`);
    }
    setQuote(body);
  } catch (e) { setCheckoutError((e as Error).message); }
};

export const submitCredits = async (kind: string, id: string, deps: CheckoutDeps) => {
  const {
    apiUrl, authHeaders, isProFeature402, idempotencyKeyRef,
    setCheckoutBusy, setCheckoutError, setCheckoutOpen, setProFeature, setMsg, reload,
  } = deps;
  const key = idempotencyKeyRef.current ?? (idempotencyKeyRef.current = crypto.randomUUID());
  setCheckoutBusy("credits");
  setCheckoutError(null);
  try {
    const r = await fetch(`${apiUrl()}/catalog/${kind}/${id}/buy-with-credits`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ idempotency_key: key }),
    });
    const body = (await r.json().catch(() => ({}))) as { detail?: string; duplicate?: boolean };
    if (isProFeature402(r.status, body)) {
      setProFeature(true); setCheckoutOpen(false); return;
    }
    if (r.status === 402) throw new Error("Insufficient credits to complete this purchase.");
    if (r.status === 409) {
      // Refunded-key replay guard — a fresh key is required.
      idempotencyKeyRef.current = crypto.randomUUID();
      throw new Error(body?.detail || "Payment state conflict — please try again.");
    }
    if (!r.ok) throw new Error(body?.detail || `HTTP ${r.status}`);
    setCheckoutOpen(false);
    setMsg("Purchased with credits — added to your library.");
    await reload();
  } catch (e) { setCheckoutError((e as Error).message); }
  finally { setCheckoutBusy(null); }
};
