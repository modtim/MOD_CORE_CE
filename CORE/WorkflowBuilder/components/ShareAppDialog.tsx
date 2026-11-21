"use client";

import { useEffect, useState } from "react";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogFooter,
    DialogTitle,
    DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Copy, Trash2, ExternalLink, Pencil, Check, X } from "lucide-react";

interface RunLink {
    id: number;
    label?: string;
    token: string;
    embed_url: string;
    api_base?: string;
    iframe: string;
    expires_at?: string | null;
    max_total_runs?: number | null;
    runs_used: number;
    password_protected?: boolean;
    max_total_credits?: number | null;
    credits_used: number;
    rate_limit_per_hour?: number | null;
    visit_count?: number;
    created_at?: string | null;
    revoked: boolean;
}

interface ShareAppDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    workflowId: number;
    workflowName?: string;
    /** The version currently loaded on the canvas. The app link is pinned to
     *  THIS version so it keeps running the exact graph the user was viewing,
     *  not whatever later becomes the workflow's latest version. When null the
     *  API falls back to the workflow's latest version. */
    versionId?: number | null;
    versionNumber?: number | null;
    accessToken?: string | null;
    apiUrl: string;
}

export function ShareAppDialog({
    open,
    onOpenChange,
    workflowId,
    workflowName,
    versionId,
    versionNumber,
    accessToken,
    apiUrl,
}: ShareAppDialogProps) {
    const [links, setLinks] = useState<RunLink[]>([]);
    const [loading, setLoading] = useState(false);
    const [creating, setCreating] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [copied, setCopied] = useState<string | null>(null);
    const [editingId, setEditingId] = useState<number | null>(null);
    const [edit, setEdit] = useState<any>({});

    // Cap form (blank = unlimited for that dimension)
    const [label, setLabel] = useState(workflowName || "");
    const [maxRuns, setMaxRuns] = useState<string>("");
    const [maxCredits, setMaxCredits] = useState<string>("");
    const [ratePerHour, setRatePerHour] = useState<string>("");
    const [expiresDays, setExpiresDays] = useState<string>("");
    const [password, setPassword] = useState<string>("");

    const authHeaders = (): HeadersInit => ({
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    });

    async function loadLinks() {
        setLoading(true);
        setError(null);
        try {
            // Links are version-pinned; scope the list to the version the user
            // is viewing so switching versions shows that version's links only.
            const qs = versionId != null ? `?version_id=${versionId}` : "";
            const r = await fetch(`${apiUrl}/road/${workflowId}/run-links${qs}`, {
                headers: authHeaders(),
            });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            setLinks(await r.json());
        } catch (e) {
            setError("Could not load existing app links.");
        } finally {
            setLoading(false);
        }
    }

    useEffect(() => {
        if (open) {
            setLabel(workflowName || "");
            loadLinks();
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, workflowId, versionId]);

    async function createLink() {
        setCreating(true);
        setError(null);
        try {
            const body: Record<string, unknown> = { label: label || undefined };
            // Pin the app to the version the user is viewing. Without this the
            // API defaults to the workflow's latest version, so the app would
            // silently track "latest" instead of the graph on screen.
            if (versionId != null) body.version_id = versionId;
            if (maxRuns) body.max_total_runs = Number(maxRuns);
            if (maxCredits) body.max_total_credits = Number(maxCredits);
            if (ratePerHour) body.rate_limit_per_hour = Number(ratePerHour);
            if (expiresDays) body.expires_in_days = Number(expiresDays);
            if (password.trim()) body.password = password.trim();
            const r = await fetch(`${apiUrl}/road/${workflowId}/run-links`, {
                method: "POST",
                headers: authHeaders(),
                body: JSON.stringify(body),
            });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            setMaxRuns(""); setMaxCredits(""); setRatePerHour(""); setExpiresDays(""); setPassword("");
            await loadLinks();
        } catch (e) {
            setError("Could not create the app link.");
        } finally {
            setCreating(false);
        }
    }

    async function revoke(id: number) {
        try {
            await fetch(`${apiUrl}/run-links/${id}/revoke`, { method: "POST", headers: authHeaders() });
            await loadLinks();
        } catch (e) {
            setError("Could not revoke that link.");
        }
    }

    function startEdit(l: RunLink) {
        setEditingId(l.id);
        const daysLeft = l.expires_at
            ? Math.max(1, Math.ceil((new Date(l.expires_at).getTime() - Date.now()) / 86400000))
            : "";
        setEdit({
            label: l.label || "",
            maxRuns: l.max_total_runs != null ? String(l.max_total_runs) : "",
            maxCredits: l.max_total_credits != null ? String(l.max_total_credits) : "",
            rate: l.rate_limit_per_hour != null ? String(l.rate_limit_per_hour) : "",
            expires: daysLeft === "" ? "" : String(daysLeft),
            password: "",
            removePw: false,
            wasProtected: !!l.password_protected,
        });
    }
    function cancelEdit() { setEditingId(null); setEdit({}); }
    async function saveLink(id: number) {
        const body: Record<string, unknown> = {
            label: (edit.label || "").trim(),
            max_total_runs: edit.maxRuns === "" ? null : Number(edit.maxRuns),
            max_total_credits: edit.maxCredits === "" ? null : Number(edit.maxCredits),
            rate_limit_per_hour: edit.rate === "" ? null : Number(edit.rate),
            expires_in_days: edit.expires === "" ? null : Number(edit.expires),
        };
        // Only touch the password when explicitly changing or removing it.
        if (edit.removePw) body.password = "";
        else if ((edit.password || "").trim()) body.password = edit.password.trim();
        try {
            await fetch(`${apiUrl}/run-links/${id}`, {
                method: "PATCH", headers: authHeaders(), body: JSON.stringify(body),
            });
            cancelEdit();
            await loadLinks();
        } catch (e) {
            setError("Could not update that link.");
        }
    }

    function copy(text: string, key: string) {
        navigator.clipboard?.writeText(text);
        setCopied(key);
        setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500);
    }

    const apiBaseOf = (l: RunLink) => l.api_base || `${apiUrl}/run-links/${l.token}`;

    // Full, copy-paste integration script. Auth = MOD's standard API-key flow:
    // exchange your long-lived key for a short-lived (~1h) access token, then
    // send it as a Bearer on every call. Needs bash + curl + jq.
    function apiScript(l: RunLink): string {
        const b = apiBaseOf(l);
        return [
            "# Integrate this workflow into your app. Auth = MOD API key.",
            "# Create a key under Account -> API keys, then:",
            'API_KEY="paste-your-api-key-here"',
            `API_ROOT="${apiUrl}"`,
            `APP="${b}"   # this workflow's endpoint`,
            "",
            "# 1) Exchange the API key for a short-lived access token (~1h).",
            "#    Do this once, reuse $TOKEN until it expires (then re-exchange).",
            'TOKEN=$(curl -s -X POST "$API_ROOT/api-keys/token" \\',
            '          -H "Content-Type: application/json" \\',
            '          -d "{\\"api_key\\":\\"$API_KEY\\"}" | jq -r .access_token)',
            "",
            "# 2) (optional) Discover input kind + the settings you can pass.",
            'curl -s "$APP" -H "Authorization: Bearer $TOKEN"   # -> { input_kind, settings: [...] }',
            "",
            "# 3) Upload your input file -> cog_id",
            'COG=$(curl -s -X POST "$APP/upload" \\',
            '        -H "Authorization: Bearer $TOKEN" \\',
            '        -F "file=@input.png" | jq -r .cog_id)',
            "",
            "# 4) Start the run. `settings` is OPTIONAL — pass any params the app",
            "#    exposes (from step 2); omit it to run with defaults.",
            'RUN=$(curl -s -X POST "$APP/run" \\',
            '        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\',
            '        -d "{\\"cog_ids\\":[$COG], \\"settings\\":{\\"texture_size\\":2048}}" | jq -r .run_id)',
            "",
            '# 5) Poll until status is "COMPLETED" (every few seconds)',
            'curl -s "$APP/runs/$RUN" -H "Authorization: Bearer $TOKEN"',
            "",
            "# 6) Fetch the result download URL(s)",
            'curl -s "$APP/runs/$RUN/outputs" -H "Authorization: Bearer $TOKEN"',
        ].join("\n");
    }

    // Per-step endpoint reference (readable list).
    function apiSteps(l: RunLink): { n: string; m: string; path: string; note: string }[] {
        return [
            { n: "1", m: "POST", path: "/api-keys/token", note: "exchange API key → access token (~1h)" },
            { n: "2", m: "GET", path: "{app}", note: "input kind + available settings" },
            { n: "3", m: "POST", path: "{app}/upload", note: "send your file (multipart) → cog_id" },
            { n: "4", m: "POST", path: "{app}/run", note: 'body {"cog_ids":[…], "settings":{…}} → run_id' },
            { n: "5", m: "GET", path: "{app}/runs/{run_id}", note: "poll → status / progress" },
            { n: "6", m: "GET", path: "{app}/runs/{run_id}/outputs", note: "→ result download URLs" },
        ];
    }

    const active = links.filter((l) => !l.revoked);

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
                <DialogHeader>
                    <DialogTitle>Share as App</DialogTitle>
                    <DialogDescription>
                        Turn this workflow into a lightweight page anyone can run — no login.
                        Drop it in an iframe on any site.
                    </DialogDescription>
                    {versionNumber != null && (
                        <p className="text-[12px] text-gray-600 mt-1">
                            New links will run <span className="font-medium">Version {versionNumber}</span>
                            {" "}— the version you have open. Open a different version before creating a link to pin it there.
                        </p>
                    )}
                </DialogHeader>

                {/* Create form */}
                <div className="space-y-3">
                    <div>
                        <Label htmlFor="sa-label">Name this link</Label>
                        <Input id="sa-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Public demo, Partner Acme, Internal test" />
                        <p className="text-[11px] text-gray-500 mt-1">For your reference — name it so you can tell your links apart below.</p>
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                        <div>
                            <Label htmlFor="sa-runs">Max total runs</Label>
                            <Input id="sa-runs" type="number" min={1} value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} placeholder="unlimited" />
                        </div>
                        <div>
                            <Label htmlFor="sa-credits">Credit budget</Label>
                            <Input id="sa-credits" type="number" min={1} value={maxCredits} onChange={(e) => setMaxCredits(e.target.value)} placeholder="unlimited" />
                        </div>
                        <div>
                            <Label htmlFor="sa-rate">Runs / hour / visitor</Label>
                            <Input id="sa-rate" type="number" min={1} value={ratePerHour} onChange={(e) => setRatePerHour(e.target.value)} placeholder="unlimited" />
                        </div>
                        <div>
                            <Label htmlFor="sa-exp">Expires in (days)</Label>
                            <Input id="sa-exp" type="number" min={1} value={expiresDays} onChange={(e) => setExpiresDays(e.target.value)} placeholder="never" />
                        </div>
                    </div>
                    <div>
                        <Label htmlFor="sa-pw">Password (optional)</Label>
                        <Input id="sa-pw" type="text" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="leave blank for no password" autoComplete="off" />
                        <p className="text-[11px] text-gray-500 mt-1">If set, visitors must enter it before they can run the app.</p>
                    </div>
                    <Button onClick={createLink} disabled={creating} className="w-full">
                        {creating ? "Creating…" : "Create app link"}
                    </Button>
                    {error && <p className="text-sm text-red-600">{error}</p>}
                </div>

                {/* Existing links */}
                <div className="mt-2 space-y-3">
                    <p className="text-sm font-medium text-gray-700">
                        Active links{versionNumber != null ? ` for Version ${versionNumber}` : ""} {loading ? "…" : `(${active.length})`}
                    </p>
                    {active.length === 0 && !loading && (
                        <p className="text-sm text-gray-500">
                            No active app links{versionNumber != null ? ` for Version ${versionNumber}` : ""} yet.
                        </p>
                    )}
                    {active.map((l) => (
                        <div key={l.id} className="border rounded-lg p-3 space-y-2">
                            {editingId === l.id ? (
                                /* ---- Edit-in-place: all settings ---- */
                                <div className="space-y-2">
                                    <div>
                                        <Label className="text-[11px] text-gray-500">Name</Label>
                                        <Input autoFocus value={edit.label} onChange={(e) => setEdit({ ...edit, label: e.target.value })} className="h-8 text-sm" />
                                    </div>
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <Label className="text-[11px] text-gray-500">Max total runs</Label>
                                            <Input type="number" min={1} value={edit.maxRuns} onChange={(e) => setEdit({ ...edit, maxRuns: e.target.value })} placeholder="unlimited" className="h-8 text-sm" />
                                        </div>
                                        <div>
                                            <Label className="text-[11px] text-gray-500">Credit budget</Label>
                                            <Input type="number" min={1} value={edit.maxCredits} onChange={(e) => setEdit({ ...edit, maxCredits: e.target.value })} placeholder="unlimited" className="h-8 text-sm" />
                                        </div>
                                        <div>
                                            <Label className="text-[11px] text-gray-500">Runs / hour / visitor</Label>
                                            <Input type="number" min={1} value={edit.rate} onChange={(e) => setEdit({ ...edit, rate: e.target.value })} placeholder="unlimited" className="h-8 text-sm" />
                                        </div>
                                        <div>
                                            <Label className="text-[11px] text-gray-500">Expires in (days)</Label>
                                            <Input type="number" min={1} value={edit.expires} onChange={(e) => setEdit({ ...edit, expires: e.target.value })} placeholder="never" className="h-8 text-sm" />
                                        </div>
                                    </div>
                                    <div>
                                        <Label className="text-[11px] text-gray-500">{edit.wasProtected ? "Change password" : "Set password"}</Label>
                                        <Input type="text" value={edit.password} disabled={edit.removePw} autoComplete="off"
                                            onChange={(e) => setEdit({ ...edit, password: e.target.value })}
                                            placeholder={edit.wasProtected ? "leave blank to keep current" : "leave blank for none"} className="h-8 text-sm" />
                                        {edit.wasProtected && (
                                            <label className="flex items-center gap-1.5 text-[11px] text-gray-500 mt-1">
                                                <input type="checkbox" checked={edit.removePw} onChange={(e) => setEdit({ ...edit, removePw: e.target.checked })} /> Remove password
                                            </label>
                                        )}
                                    </div>
                                    <div className="flex gap-2">
                                        <Button size="sm" onClick={() => saveLink(l.id)}><Check className="h-3 w-3 mr-1" />Save</Button>
                                        <Button size="sm" variant="outline" onClick={cancelEdit}><X className="h-3 w-3 mr-1" />Cancel</Button>
                                    </div>
                                </div>
                            ) : (
                                <>
                                    <div className="flex items-start justify-between gap-2">
                                        <div className="min-w-0 flex-1">
                                            <div className="font-medium text-sm truncate flex items-center gap-1.5">
                                                {l.label || "Untitled"}
                                                {l.password_protected && <span title="Password protected" className="text-[10px] font-normal text-gray-500 border border-gray-300 rounded px-1">🔒</span>}
                                                <button onClick={() => startEdit(l)} title="Edit settings" className="text-gray-400 hover:text-gray-700 shrink-0"><Pencil className="h-3 w-3" /></button>
                                            </div>
                                            <div className="text-[11px] text-gray-400 truncate">
                                                Runs “{workflowName || "this workflow"}”
                                                {l.created_at ? ` · created ${new Date(l.created_at).toLocaleDateString()}` : ""}
                                            </div>
                                        </div>
                                        <button onClick={() => revoke(l.id)} title="Revoke this link" className="text-red-500 hover:text-red-700 shrink-0">
                                            <Trash2 className="h-4 w-4" />
                                        </button>
                                    </div>
                                    {/* Full settings, labeled (mirrors the create form) */}
                                    <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-xs">
                                        <dt className="text-gray-500">Max total runs</dt>
                                        <dd className="text-gray-800">{l.max_total_runs != null ? `${l.max_total_runs} (${l.runs_used} used)` : "unlimited"}</dd>
                                        <dt className="text-gray-500">Credit budget</dt>
                                        <dd className="text-gray-800">{l.max_total_credits != null ? `${l.max_total_credits} (${l.credits_used} used)` : "unlimited"}</dd>
                                        <dt className="text-gray-500">Runs / hour / visitor</dt>
                                        <dd className="text-gray-800">{l.rate_limit_per_hour != null ? l.rate_limit_per_hour : "unlimited"}</dd>
                                        <dt className="text-gray-500">Expires</dt>
                                        <dd className="text-gray-800">{l.expires_at ? new Date(l.expires_at).toLocaleDateString() : "never"}</dd>
                                        <dt className="text-gray-500">Password</dt>
                                        <dd className="text-gray-800">{l.password_protected ? "Yes 🔒" : "No"}</dd>
                                        <dt className="text-gray-500">Views</dt>
                                        <dd className="text-gray-800">{l.visit_count != null ? l.visit_count : 0}</dd>
                                    </dl>
                                </>
                            )}
                            <div className="flex items-center gap-2">
                                <Input readOnly value={l.embed_url} className="text-xs" />
                                <Button size="sm" variant="outline" onClick={() => copy(l.embed_url, `u${l.id}`)}>
                                    <Copy className="h-3 w-3 mr-1" />{copied === `u${l.id}` ? "Copied" : "URL"}
                                </Button>
                            </div>
                            <div className="flex items-center gap-2">
                                <Button size="sm" variant="outline" onClick={() => copy(l.iframe, `i${l.id}`)}>
                                    <Copy className="h-3 w-3 mr-1" />{copied === `i${l.id}` ? "Copied" : "Copy iframe"}
                                </Button>
                                <a href={l.embed_url} target="_blank" rel="noreferrer" className="inline-flex items-center text-sm text-indigo-600 hover:underline">
                                    <ExternalLink className="h-3 w-3 mr-1" />Open
                                </a>
                            </div>

                            {/* Programmatic API — protected by the tenant's API key */}
                            <div className="mt-2 pt-2 border-t border-gray-100">
                                <div className="text-xs font-medium text-gray-600 mb-1">API integration (use your API key)</div>
                                <div className="flex items-center gap-2">
                                    <Input readOnly value={apiBaseOf(l)} className="text-xs" />
                                    <Button size="sm" variant="outline" onClick={() => copy(apiBaseOf(l), `a${l.id}`)}>
                                        <Copy className="h-3 w-3 mr-1" />{copied === `a${l.id}` ? "Copied" : "Base URL"}
                                    </Button>
                                </div>
                                <ol className="mt-2 space-y-1 text-xs text-gray-600">
                                    {apiSteps(l).map((s) => (
                                        <li key={s.n} className="flex gap-2">
                                            <span className="font-semibold text-gray-800">{s.n}.</span>
                                            <span><code className="text-[11px] bg-gray-100 px-1 rounded">{s.m} {s.path}</code> — {s.note}</span>
                                        </li>
                                    ))}
                                </ol>
                                <div className="mt-2 flex items-center justify-between gap-2">
                                    <span className="text-[11px] text-gray-500">Full script — exchange your API key for a token, then send it as <code>Bearer</code>. Create a key in Account → API keys.</span>
                                    <Button size="sm" variant="outline" onClick={() => copy(apiScript(l), `c${l.id}`)}>
                                        <Copy className="h-3 w-3 mr-1" />{copied === `c${l.id}` ? "Copied" : "Copy script"}
                                    </Button>
                                </div>
                                <pre className="mt-1 p-2 bg-gray-900 text-gray-100 rounded text-[11px] leading-relaxed whitespace-pre-wrap break-all">{apiScript(l)}</pre>
                            </div>
                        </div>
                    ))}
                </div>

                <DialogFooter>
                    <Button variant="outline" onClick={() => onOpenChange(false)}>Done</Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
