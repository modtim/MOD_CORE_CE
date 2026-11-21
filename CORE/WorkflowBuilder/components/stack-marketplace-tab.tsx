"use client";

// GF.17a (launch-completion-megaplan) — dedicated "Stacks" marketplace tab.
//
// Richer view than the generic catalog kind-tab (GG.UI.1, inside
// The store dialog's Browse feed): that one only has the plain
// name/price/owned fields GET /catalog?kind=stack returns for every kind.
// Here we ALSO surface GPU needs + licence class, which only live on the
// per-item detail (`GET /catalog/stack/{id}` -> catalog_service
// `_stack_detail_fields`: `gpu_requirements`, `license_class`, `licence` off
// the pinned StackPackage) — so those are fetched lazily on row expansion,
// not for the whole list up front.
//
// Buy/Get/Install flows are NOT re-implemented here — the full page
// (`/store/stack/{id}`, GG.UI.2/2b) owns quote/checkout/install. This tab
// only deep-links to it. "Open in Persistent Stacks" reuses the Dashboard's
// own deep link (`openPersistentStacks` in Dashboard/js/library.js:
// `?tab=stacks&stack=<id>#system`).
//
// `installed_stack_id` is NOT emitted by the catalog API today (it only
// lives on the tenant's TenantStackEntitlement row server-side) — read
// defensively in case a future revision inlines it, and fall back to a
// plain "Owned" badge otherwise.

import { useCallback, useEffect, useState } from "react";
import { Loader2, Search, ExternalLink, Cpu, ShieldCheck } from "lucide-react";
import { getApiUrl, getDashboardUrl } from "@/lib/urls";
import { getAccessToken } from "@/lib/api-client";
// GSEC FX.95 p4b — one Verified component for every marketplace surface
// (store cards, store detail page, this tab).
import { VerifiedBadge } from "@/components/node-chips";

export interface StackListingRow {
    kind: "stack";
    id: number;
    name: string;
    description?: string | null;
    price_cents?: number | null;
    price_tier?: string | null;
    owned: boolean;
    included_in?: string | null;
    status?: string | null;
    visibility?: string | null;
    // Forward-compat only — see file header note.
    installed_stack_id?: number | null;
    publisher?: string | null;
    // GSEC FX.95 p4a/p4b — Verified-badge source on GET /catalog rows
    // (schemas.CatalogItem): the publisher tenant has a 'verified'
    // PublisherVerification record. NOT MOD-built (p5c gates gov tenants on
    // MOD-built listings only).
    publisher_verified?: boolean | null;
}

export interface StackListingDetail extends StackListingRow {
    version?: string | number | null;
    gpu_requirements?: Record<string, unknown> | string | null;
    license_class?: string | null;
    licence?: string | null;
    actions?: string[];
}

interface StackMarketplaceTabProps {
    accessToken: string | null;
    /**
     * ANS-76 (FIX-CE-WB-MARKETPLACE-HIDE-PRICED-ITEMS-ENTIRELY) — positive
     * Community Edition signal from the caller (the store dialog's `ceEdition`,
     * resolved from /platform/features at :170).
     * OWNER DECISION 11-14 (Tim): FAIL CLOSED for the marketplace edition
     * signal. The caller (the store dialog :171) now derives `ceEdition`
     * fail-closed: unresolved / loading / errored entitlements pass `ce={true}`
     * (CE behaviour, priced rows hidden); only a RESOLVED non-CE edition passes
     * an explicit `ce={false}` and keeps the priced rows. The default here stays
     * `false` (a direct caller with no edition signal at all is a paid render —
     * unchanged for the existing tab tests); the fail-closed rule lives in the
     * dialog, the only product caller of this tab.
     */
    ce?: boolean;
}

function authHeaders(accessToken: string | null): Record<string, string> {
    const token = accessToken || getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
}

export function formatPrice(cents?: number | null): string {
    if (!cents || cents <= 0) return "Free";
    return `$${(cents / 100).toFixed(2)}`;
}

// `gpu_requirements` is a free-form JSONB column on StackPackage — no fixed
// schema is pinned, so render best-effort ("2× CUDA 48 GB" shape) and fall
// back to a raw dump rather than assume a structure that isn't guaranteed.
// `_gpu_requirements` (stack_packages.py) returns ``{service: {kind,
// min_vram_gb, count}}`` — detect the per-service-map shape and format
// each service; keep the old flat-object path working for legacy payloads.
export function formatGpuNeeds(req: StackListingDetail["gpu_requirements"]): string {
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
    try {
        return JSON.stringify(req);
    } catch {
        return String(req);
    }
}

export function StackMarketplaceTab({ accessToken, ce = false }: StackMarketplaceTabProps) {
    const [query, setQuery] = useState("");
    const [rows, setRows] = useState<StackListingRow[] | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [expandedId, setExpandedId] = useState<number | null>(null);
    const [details, setDetails] = useState<Record<number, StackListingDetail>>({});
    const [detailLoading, setDetailLoading] = useState<number | null>(null);
    const [detailError, setDetailError] = useState<Record<number, string>>({});
    //  (11-16, Tim:
    // "store api routes are Deny!!!"): the store deny p1 landed (85c1e76b9c) —
    // open-core/manifest.yaml:1901-1905 now DENIES CORE/API/routers/catalog.py,
    // services/catalog_service.py, crud/node_marketplace_paid.py,
    // services/publisher_verification.py and services/stack_packages.py — so
    // GET /catalog?kind=stack and GET /catalog/stack/{id} 404 in CE. The old
    // 2400GX-storeB 30.5.3 premise ("/catalog is a CE route: no edition gate
    // or CE skip") is STALE: the tab must never fetch on CE at all, or it
    // renders a hard "Failed to load stacks: catalog fetch failed (404)" error.
    // The CE signal is the caller's fail-closed `ceEdition`
    // (the store dialog:176, `!(resolved && edition && edition !== "ce")`),
    // passed here as `ce`; unresolved/errored entitlements pass ce={true}
    // (fail closed, OWNER DECISION 11-14 Tim). A resolved non-CE (paid)
    // edition passes ce={false} and keeps this fetch path byte-identical.
    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        // CE (or unresolved): no fetch at all — the denied route is never
        // called, and the tab renders its plain "not available" line instead.
        if (ce) {
            setLoading(false);
            return;
        }
        try {
            const res = await fetch(`${getApiUrl()}/catalog?kind=stack`, {
                headers: authHeaders(accessToken),
            });
            if (!res.ok) throw new Error(`catalog fetch failed (${res.status})`);
            const data = await res.json();
            setRows(Array.isArray(data) ? data : []);
        } catch (err) {
            setError((err as Error)?.message || "failed to load stacks");
            setRows(null);
        } finally {
            setLoading(false);
        }
    }, [accessToken, ce]);

    useEffect(() => {
        load();
    }, [load]);

    const toggleExpand = useCallback(
        async (row: StackListingRow) => {
            const next = expandedId === row.id ? null : row.id;
            setExpandedId(next);
            // FIX-CE-WB-STORE-ENTRY-POINTS-...-D71 (11-16): the per-row detail
            // leg hits GET /catalog/stack/{id}, denied in CE (85c1e76b9c) —
            // never fetch it on CE (or while the edition is unresolved).
            if (next !== null && !ce && !details[row.id]) {
                setDetailLoading(row.id);
                setDetailError((prev) => ({ ...prev, [row.id]: "" }));
                try {
                    const res = await fetch(`${getApiUrl()}/catalog/stack/${row.id}`, {
                        headers: authHeaders(accessToken),
                    });
                    if (!res.ok) throw new Error(`detail fetch failed (${res.status})`);
                    const data = await res.json();
                    setDetails((prev) => ({ ...prev, [row.id]: data }));
                } catch (err) {
                    setDetailError((prev) => ({ ...prev, [row.id]: (err as Error)?.message || "failed to load details" }));
                } finally {
                    setDetailLoading(null);
                }
            }
        },
        [accessToken, details, expandedId]
    );

    // ANS-76 (FIX-CE-WB-MARKETPLACE-HIDE-PRICED-ITEMS-ENTIRELY) — CE lists
    // FREE listings only: a priced row (price_cents > 0) never appears in the
    // Community Edition marketplace at all, so no Buy CTA and no price chip can
    // leak. Free rows keep their Get link untouched (the render below still
    // picks "Get" for them). Paid/unresolved editions keep every row.
    const filtered = (rows || []).filter((r) => {
        if (ce && (r.price_cents ?? 0) > 0) return false;
        if (!query.trim()) return true;
        const q = query.toLowerCase();
        return r.name.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q);
    });

    return (
        <div data-testid="stack-marketplace-tab" className="flex flex-col gap-3">
            {/* FIX-CE-WB-STORE-ENTRY-POINTS-...-D71 (11-16, Tim: "CE has no
                store scope"; store deny p1 85c1e76b9c): on CE (or while the
                edition is unresolved — fail closed) the whole store surface
                degrades to ONE plain line: no search box, no rows, no price
                chips, no Buy/Get CTAs, and no /catalog request at all.
                Supersedes the ANS-76 "CE lists FREE listings only" posture:
                with /catalog denied there is no CE listing to filter. */}
            {ce && (
                <div
                    data-testid="stack-marketplace-ce-unavailable"
                    className="text-sm text-gray-500 py-6 text-center"
                >
                    Stacks are not available in MOD Community Edition.
                </div>
            )}
            {!ce && (
                <>
            <div className="relative">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" aria-hidden="true" />
                <input
                    data-testid="stack-marketplace-search"
                    className="w-full pl-8 pr-3 py-1.5 text-sm border border-gray-200 rounded-md"
                    placeholder="Search stacks..."
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                />
            </div>

            {loading && (
                <div data-testid="stack-marketplace-loading" className="flex items-center gap-2 text-sm text-gray-500 py-6 justify-center">
                    <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> Loading stacks...
                </div>
            )}

            {!loading && error && (
                <div data-testid="stack-marketplace-error" className="text-sm text-red-600 py-4 text-center">
                    Failed to load stacks: {error}
                    <button data-testid="stack-marketplace-retry" onClick={load} className="ml-2 underline text-primary">
                        Retry
                    </button>
                </div>
            )}

            {!loading && !error && filtered.length === 0 && (
                <div data-testid="stack-marketplace-empty" className="text-sm text-gray-500 py-6 text-center">
                    {rows && rows.length === 0 ? "No stacks are listed yet." : "No stacks match your search."}
                </div>
            )}

            {!loading &&
                !error &&
                filtered.map((row) => {
                    const detail = details[row.id];
                    const isExpanded = expandedId === row.id;
                    const installedStackId = detail?.installed_stack_id ?? row.installed_stack_id ?? null;
                    const isInstalled = row.owned && installedStackId != null;
                    return (
                        <div key={row.id} data-testid={`stack-row-${row.id}`} className="border border-gray-200 rounded-md">
                            <button
                                type="button"
                                data-testid={`stack-row-toggle-${row.id}`}
                                onClick={() => toggleExpand(row)}
                                className="w-full flex items-center justify-between gap-3 px-3 py-2 text-left"
                            >
                                <div className="min-w-0">
                                    <div className="text-sm font-medium truncate">{row.name}</div>
                                    <div className="text-xs text-gray-500 truncate">{row.publisher || "MOD Marketplace"}</div>
                                </div>
                                <div className="flex items-center gap-3 flex-shrink-0">
                                    <span className="text-xs text-gray-600">{formatPrice(row.price_cents)}</span>
                                    <VerifiedBadge publisherVerified={row.publisher_verified} />
                                    {row.owned ? (
                                        <span
                                            data-testid={`stack-badge-${row.id}`}
                                            className="text-xs px-2 py-0.5 rounded-full bg-green-100 text-green-700"
                                        >
                                            {isInstalled ? "Installed" : "Owned"}
                                        </span>
                                    ) : null}
                                </div>
                            </button>

                            {isExpanded && (
                                <div data-testid={`stack-detail-${row.id}`} className="px-3 pb-3 border-t border-gray-100 pt-2 text-sm">
                                    {detailLoading === row.id && (
                                        <div className="flex items-center gap-2 text-gray-500 text-xs">
                                            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" /> Loading details...
                                        </div>
                                    )}
                                    {detailError[row.id] && <div className="text-red-600 text-xs">{detailError[row.id]}</div>}
                                    {detail && (
                                        <div className="flex flex-col gap-1.5">
                                            {row.description && <p className="text-gray-600 text-xs">{row.description}</p>}
                                            <div className="flex items-center gap-1.5 text-xs text-gray-700">
                                                <Cpu className="h-3.5 w-3.5" aria-hidden="true" />
                                                <span data-testid={`stack-gpu-${row.id}`}>{formatGpuNeeds(detail.gpu_requirements)}</span>
                                            </div>
                                            <div className="flex items-center gap-1.5 text-xs text-gray-700">
                                                <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                                                <span data-testid={`stack-licence-${row.id}`}>{detail.license_class || detail.licence || "open"}</span>
                                            </div>
                                            <div className="flex items-center gap-2 pt-1">
                                                {isInstalled ? (
                                                    <a
                                                        data-testid={`stack-open-installed-${row.id}`}
                                                        href={`${getDashboardUrl()}/?tab=stacks&stack=${installedStackId}#system`}
                                                        target="_blank"
                                                        rel="noreferrer"
                                                        className="inline-flex items-center gap-1 text-xs text-primary underline"
                                                    >
                                                        Open in Persistent Stacks <ExternalLink className="h-3 w-3" aria-hidden="true" />
                                                    </a>
                                                ) : (
                                                    <a
                                                        data-testid={`stack-open-full-${row.id}`}
                                                        href={`/store/stack/${row.id}`}
                                                        className="inline-flex items-center gap-1 text-xs text-primary underline"
                                                    >
                                                        {row.owned ? "Open" : false ? "Buy" : "Get"}{" "}
                                                        <ExternalLink className="h-3 w-3" aria-hidden="true" />
                                                    </a>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
                </>
            )}
        </div>
    );
}

export default StackMarketplaceTab;
