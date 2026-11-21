"use client";
// sz-4.9c — bind an OAuth connection to a node's secret slot.
//
// Node definitions declare slots like
//   { env_var: "OAUTH_ACCESS_TOKEN", oauth_providers: ["google"] }.
// For such a slot the panel renders THIS picker instead of the named-secret
// dropdown. It lists the tenant's live OAuth connections (GET
// {api}/integrations/oauth/connections, Bearer token) filtered to
// provider ∈ providers; ACTIVE ones are selectable, labelled
// "<provider> · <external_account_id || #id>", non-active ones are listed
// disabled with "(reconnect needed)". Selecting calls onChange(connectionId)
// — the panel stores {env_var, oauth_connection_id} in secret_refs; the
// token itself never reaches the browser (the server resolves the ref at
// run time). A 403 shows the admin hint (no crash, no retry loop); an
// empty list shows the "Connect <provider> in Settings → Integrations"
// hint (dashboard hash-link when the dashboard URL is configured).

import { apiUrl, dashboardUrlOrNull } from "@/lib/runtime-config";

import { useEffect, useState } from "react";

export const OAuthConnectionPicker = ({
  providers,
  envVar,
  value,
  onChange,
  accessToken,
}: {
  providers: string[];
  envVar: string;
  value: number | null;
  onChange: (connectionId: number | null) => void;
  accessToken: string | null;
}) => {
  const [connections, setConnections] = useState<
    { id: number; provider: string; external_account_id?: string | null; status?: string }[]
  >([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Fetch once per picker mount (i.e. once per Secrets-section expand), only
  // while authed; AbortController cancels the request if the panel unmounts
  // mid-flight. Never retried on error — the user re-opens the section.
  useEffect(() => {
    if (!accessToken) return;
    const controller = new AbortController();
    const fetchConnections = async () => {
      try {
        const resp = await fetch(
          `${apiUrl()}/integrations/oauth/connections`,
          {
            headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
            signal: controller.signal,
          },
        );
        if (resp.status === 403) {
          // Tenant member without connect rights — hint, don't crash/retry.
          setLoadError(
            `Ask a tenant admin to connect ${providers.join("/")} in Settings → Integrations`,
          );
          return;
        }
        if (!resp.ok) {
          setLoadError("Could not load OAuth connections");
          return;
        }
        const rows = await resp.json();
        if (Array.isArray(rows)) {
          setConnections(
            rows
              .filter((c: any) => c && typeof c.id === "number" && providers.includes(c.provider))
              .map((c: any) => ({
                id: c.id as number,
                provider: c.provider as string,
                external_account_id: c.external_account_id as string | null,
                status: c.status as string | undefined,
              })),
          );
        }
      } catch (e) {
        if ((e as any)?.name === "AbortError") return; // unmounted mid-flight
        console.error("Failed to fetch OAuth connections:", e);
      }
    };
    fetchConnections();
    return () => controller.abort();
  }, [accessToken, providers.join(",")]);

  const label = (c: any) => `${c.provider} · ${c.external_account_id || `#${c.id}`}`;
  const active = connections.filter((c) => c.status === "active");
  const inactive = connections.filter((c) => c.status !== "active");
  const dashboardBase = dashboardUrlOrNull();
  const hint = `Connect ${providers.join("/")} in Settings → Integrations`;

  return (
    <div className="space-y-1">
      <select
        data-testid={`oauth-connection-select-${envVar}`}
        className="w-full text-xs rounded-md border bg-transparent px-2 py-1"
        value={value != null ? String(value) : ""}
        onChange={(e) => {
          const raw = (e.target as any).value;
          onChange(raw ? Number(raw) : null);
        }}
      >
        <option value="">None</option>
        {active.map((c) => (
          <option key={c.id} value={String(c.id)}>
            {label(c)}
          </option>
        ))}
        {/* Keep a stale binding visible so it doesn't silently revert. */}
        {value != null && !active.some((c) => c.id === value) && (
          <option value={String(value)}>(unavailable)</option>
        )}
      </select>
      {inactive.map((c) => (
        <p key={c.id} className="text-[10px] text-gray-400">
          {label(c)} (reconnect needed)
        </p>
      ))}
      {loadError ? (
        <p className="text-[10px] text-amber-700">{loadError}</p>
      ) : active.length === 0 && inactive.length === 0 ? (
        dashboardBase ? (
          <a
            className="text-xs text-primary hover:underline"
            href={`${dashboardBase}/?tab=integrations#settings`}
          >
            {hint}
          </a>
        ) : (
          <p className="text-xs text-muted-foreground">{hint}</p>
        )
      ) : null}
    </div>
  );
};
