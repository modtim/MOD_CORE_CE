"use client";
import { apiUrl } from "@/lib/runtime-config";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { CopyLogButton } from "@/components/ui/copy-log-button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AlertCircle, CheckCircle2, Clock, Loader2, ShieldX, Wrench, RefreshCcw, ExternalLink } from "lucide-react";
import type { NodeDefinition } from "@/lib/types";

interface ScanDetailsDialogProps {
  open: boolean;
  onClose: () => void;
  node: Partial<NodeDefinition> | null;
  accessToken: string | null;
  onRescan: (nodeId: number) => Promise<void>;
  rescanning: boolean;
}

type Vuln = {
  id?: string;
  severity?: string;
  package?: string;
  installed?: string;
  fixed?: string | null;
  title?: string;
};

export function ScanDetailsDialog({
  open,
  onClose,
  node,
  accessToken,
  onRescan,
  rescanning,
}: ScanDetailsDialogProps) {
  const [fresh, setFresh] = useState<Partial<NodeDefinition> | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [justRescanned, setJustRescanned] = useState(false);

  // Pending-state queue context — total scans in flight + position
  // of THIS node's most recent scan task in the pending queue. Polled
  // every 5s while the dialog is open and the node is pending_scan.
  const [queueInfo, setQueueInfo] = useState<{
    in_flight: number;
    pending: number;
    this_node_position: number | null;
    last_attempt: { task_id: number; status: string; completed_at: string | null; error_message: string | null } | null;
  } | null>(null);

  // Re-fetch full node definition when the dialog opens, when the user
  // hits Rescan (refreshTick), or every 5s while the scan is in flight.
  // Without the refreshTick path, clicking Rescan would POST + 202 in
  // a few ms but the dialog body would still show the pre-rescan state,
  // making it feel like nothing happened.
  useEffect(() => {
    if (!open || !node?.id || !accessToken) {
      setFresh(null);
      setQueueInfo(null);
      return;
    }
    const API_URL = apiUrl();
    let aborted = false;
    const fetchOnce = async () => {
      try {
        const r = await fetch(`${API_URL}/nodes/${node.id}`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
        });
        if (r.ok && !aborted) setFresh(await r.json());
      } catch {}

      // Queue context regardless of current status — after a rescan
      // we need to render queue position even if the node hasn't
      // flipped back to pending_scan in the DB yet.
      try {
        const q = await fetch(`${API_URL}/nodes/${node.id}/scan-queue-context`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
        });
        if (q.ok && !aborted) setQueueInfo(await q.json());
      } catch {}
    };
    fetchOnce();

    // Poll every 5s when actively waiting on a scan (either the existing
    // pending_scan / scanning state, OR the user just hit Rescan).
    const effective = fresh || node;
    const status = (effective.scan_status || "approved");
    const shouldPoll = status === "pending_scan" || status === "scanning" || justRescanned;
    if (shouldPoll) {
      const interval = setInterval(fetchOnce, 5000);
      return () => { aborted = true; clearInterval(interval); };
    }
    return () => { aborted = true; };
  }, [open, node?.id, accessToken, refreshTick, justRescanned, fresh?.scan_status]);

  // Wrap the parent's rescan handler so the dialog can refresh itself
  // immediately on success — kicks the useEffect above and shows the
  // user that something concretely happened.
  const handleRescanClick = async (id: number) => {
    setJustRescanned(true);
    await onRescan(id);
    // Bump the tick so the fetch re-runs with fresh state. The library
    // already POSTs the rescan; we just need to re-fetch our copy.
    setTimeout(() => setRefreshTick((t) => t + 1), 200);
    // Drop the just-rescanned flag once we've seen the status flip
    // (next polling tick will catch it). 30s is a hard cap.
    setTimeout(() => setJustRescanned(false), 30000);
  };

  if (!node) return null;
  const effective = fresh || node;
  const status = (effective.scan_status || "approved") as string;
  const reason = effective.rejection_reason || "";
  const scanResult: any = (effective as any).scan_result || null;
  const vulns: Vuln[] = (scanResult?.vulnerabilities as Vuln[]) || [];

  // Split the vuln set into the same three buckets our tiered scanner uses.
  const fixable = vulns.filter((v) => v.severity === "HIGH" && v.fixed);
  const unfixedUserspace = vulns.filter(
    (v) =>
      v.severity === "HIGH" &&
      !v.fixed &&
      /^(libncurses|libtinfo|ncurses-|libsystemd|libudev|systemd-sysv|libpam|libgcc|libstdc\+\+|zlib|gcc-|base-files)/.test(
        v.package || "",
      ),
  );
  const critical = vulns.filter((v) => v.severity === "CRITICAL");
  const otherHighs = vulns.filter(
    (v) =>
      v.severity === "HIGH" &&
      !fixable.includes(v) &&
      !unfixedUserspace.includes(v),
  );

  // Banner color + icon per scan_status. The Harbor-driven scan path
  // (push → Harbor's built-in Trivy → poll → outcome) writes one of
  // `clean` / `high_vulns` / `critical_vulns` / `scan_failed` — those
  // are NEW statuses not in the legacy enum, so they need explicit
  // entries here. Without them the banner fell through to a gray
  // "unknown status" tile and the user had no idea why their node
  // was disabled in the palette. The legacy enum
  // (`approved` / `rejected` / `failed`) is kept for back-compat
  // with admin-curated nodes that pre-date the Harbor migration.
  const banner = {
    approved:       { cls: "bg-green-50 border-green-200 text-green-900",   icon: <CheckCircle2 className="w-5 h-5 text-green-700" />, title: "Approved" },
    clean:          { cls: "bg-green-50 border-green-200 text-green-900",   icon: <CheckCircle2 className="w-5 h-5 text-green-700" />, title: "Clean — No Vulnerabilities Found" },
    high_vulns:     { cls: "bg-amber-50 border-amber-200 text-amber-900",   icon: <AlertCircle className="w-5 h-5 text-amber-700" />,   title: "High-Severity Findings — Usable with Warning" },
    critical_vulns: { cls: "bg-red-50 border-red-200 text-red-900",         icon: <ShieldX className="w-5 h-5 text-red-700" />,         title: "Critical Vulnerabilities — Blocked" },
    scan_failed:    { cls: "bg-orange-50 border-orange-200 text-orange-900", icon: <AlertCircle className="w-5 h-5 text-orange-700" />, title: "Registry Scan Failed" },
    rejected:       { cls: "bg-red-50 border-red-200 text-red-900",         icon: <ShieldX className="w-5 h-5 text-red-700" />,         title: "Rejected" },
    failed:         { cls: "bg-orange-50 border-orange-200 text-orange-900", icon: <AlertCircle className="w-5 h-5 text-orange-700" />, title: "Scan Failed" },
    pending_scan:   { cls: "bg-yellow-50 border-yellow-200 text-yellow-900", icon: <Clock className="w-5 h-5 text-yellow-700" />,        title: "Waiting for Scan" },
    scanning:       { cls: "bg-blue-50 border-blue-200 text-blue-900",       icon: <Clock className="w-5 h-5 text-blue-700" />,         title: "Scanning" },
  }[status] || { cls: "bg-gray-50 border-gray-200 text-gray-900", icon: null, title: status };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-3xl max-h-[85vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {banner.icon}
            Scan Details — {effective.name}
          </DialogTitle>
          <DialogDescription>
            Security scan status and recommended next steps for this node.
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className="flex-1 pr-2">
          {/* STATUS BANNER — the big clear "what is happening here" block */}
          <div className={`rounded-lg border p-4 ${banner.cls}`}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-lg font-semibold">{banner.title}</span>
              <Badge variant="outline">{status}</Badge>
            </div>

            {status === "approved" && (
              <p className="text-sm">
                No critical or reject-class high-severity vulnerabilities were found.
                This node is cleared for use in workflows.
              </p>
            )}

            {status === "pending_scan" && (
              <div className="text-sm space-y-3">
                <p>
                  A Trivy CVE scan is queued for this node. Workers process scans
                  sequentially. The node will unlock automatically when it completes.
                </p>

                {queueInfo && (
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="rounded bg-white/60 border p-2">
                      <div className="text-xs text-yellow-700">In flight</div>
                      <div className="text-2xl font-bold">{queueInfo.in_flight}</div>
                      <div className="text-[10px] text-yellow-700">running now</div>
                    </div>
                    <div className="rounded bg-white/60 border p-2">
                      <div className="text-xs text-yellow-700">Pending</div>
                      <div className="text-2xl font-bold">{queueInfo.pending}</div>
                      <div className="text-[10px] text-yellow-700">in queue</div>
                    </div>
                    <div className="rounded bg-white/60 border p-2">
                      <div className="text-xs text-yellow-700">Your position</div>
                      <div className="text-2xl font-bold">
                        {queueInfo.this_node_position !== null
                          ? `#${queueInfo.this_node_position}`
                          : "—"}
                      </div>
                      <div className="text-[10px] text-yellow-700">
                        {queueInfo.this_node_position === null ? "no task yet" : "in line"}
                      </div>
                    </div>
                  </div>
                )}

                {queueInfo?.last_attempt && (
                  <div className="rounded bg-white/60 border p-2 text-xs">
                    <div className="font-semibold mb-1">Last scan attempt</div>
                    <div>
                      Task #{queueInfo.last_attempt.task_id} —{" "}
                      <Badge variant="outline" className="text-xs">{queueInfo.last_attempt.status}</Badge>
                      {queueInfo.last_attempt.completed_at && (
                        <span className="text-gray-600"> at {queueInfo.last_attempt.completed_at}</span>
                      )}
                    </div>
                    {queueInfo.last_attempt.error_message && (
                      <div>
                        <div className="flex justify-end mt-1">
                          <CopyLogButton
                            getText={() => String(queueInfo.last_attempt?.error_message || "")}
                            title="Copy the scan error to the clipboard"
                          />
                        </div>
                        <pre className="mt-1 p-1 bg-red-50 border border-red-200 rounded text-[11px] font-mono whitespace-pre-wrap break-words max-h-20 overflow-auto">
                          {queueInfo.last_attempt.error_message}
                        </pre>
                      </div>
                    )}
                    <p className="text-gray-600 mt-1">
                      A new attempt is queued. If it keeps failing the same way,
                      click "View Details" again after the next attempt finishes
                      (or click Rescan Now to retry immediately).
                    </p>
                  </div>
                )}

                {!queueInfo && (
                  <p className="text-xs text-yellow-700">
                    Loading queue position…
                  </p>
                )}
              </div>
            )}

            {status === "scanning" && (
              <p className="text-sm">
                A scan is actively running. This page will update when it completes.
              </p>
            )}

            {status === "failed" && (
              <div className="text-sm space-y-2">
                <p className="font-medium">
                  The scan could not complete. This usually means the scanner
                  could not pull the image, or Trivy itself hit an error.
                </p>
                {reason && (
                  <div>
                    <div className="flex justify-end">
                      <CopyLogButton
                        getText={() => reason}
                        title="Copy the failure reason to the clipboard"
                      />
                    </div>
                    <pre className="mt-1 p-2 bg-white/50 rounded text-xs font-mono whitespace-pre-wrap break-words max-h-40 overflow-auto">
                      {reason}
                    </pre>
                  </div>
                )}
                <div className="mt-2">
                  <div className="font-semibold mb-1">What to try:</div>
                  <ul className="list-disc list-inside text-sm space-y-1">
                    <li>Confirm the image exists in the registry the scanner pulls from (the platform registry for system nodes, registry:5000 for tenant builds).</li>
                    <li>If the node was copied from a deleted system node, the referenced image may no longer exist. Rebuild it from source.</li>
                    <li>Click Rescan below to retry — the error may be transient (registry flake, CVE DB update).</li>
                    <li>If Rescan keeps failing with the same reason, contact support with the node name + the error text above.</li>
                  </ul>
                </div>
              </div>
            )}

            {status === "rejected" && (
              <div className="text-sm space-y-2">
                <p className="font-medium">
                  The image has vulnerabilities that block approval.
                  The recommended action depends on which class they fall into:
                </p>
                {reason && (
                  <div>
                    <div className="flex justify-end">
                      <CopyLogButton
                        getText={() => reason}
                        title="Copy the rejection reason to the clipboard"
                      />
                    </div>
                    <pre className="mt-1 p-2 bg-white/50 rounded text-xs font-mono whitespace-pre-wrap break-words max-h-40 overflow-auto">
                      {reason}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* VULN BREAKDOWN — only when we have data */}
          {status === "rejected" && vulns.length > 0 && (
            <div className="mt-4 space-y-3">
              {critical.length > 0 && (
                <VulnGroup
                  label="Critical"
                  tone="red"
                  description="Always reject. Rebuild with a patched base or switch base images."
                  vulns={critical}
                />
              )}
              {fixable.length > 0 && (
                <VulnGroup
                  label={`High — fixable (${fixable.length})`}
                  tone="red"
                  description="These have patched versions available upstream. Rebuild with `apt-get -y upgrade` + `pip install --upgrade ...` and they'll clear. Use one of MOD's curated base images (ghcr.io/modtim/base-*) — they rebuild nightly."
                  vulns={fixable}
                />
              )}
              {otherHighs.length > 0 && (
                <VulnGroup
                  label={`High — unclassified (${otherHighs.length})`}
                  tone="red"
                  description="Unfixed HIGHs in packages MOD's classifier hasn't categorized. Policy default is reject (conservative). File an exception request with your support contact if the package is reachable from Layer-3 sandbox."
                  vulns={otherHighs}
                />
              )}
              {unfixedUserspace.length > 0 && (
                <VulnGroup
                  label={`High — unfixed userspace (${unfixedUserspace.length})`}
                  tone="yellow"
                  description="Unpatched CVEs in ncurses / systemd-libs / similar. These do NOT by themselves cause rejection — the Layer-3 sandbox (read-only rootfs, seccomp, no-new-privs) contains them. Shown here for transparency only."
                  vulns={unfixedUserspace}
                />
              )}
            </div>
          )}

          {/* METADATA PANEL — always, so the user knows when this was scanned */}
          <div className="mt-4 rounded-lg border p-3 bg-gray-50 text-xs space-y-1">
            <div className="flex justify-between">
              <span className="text-gray-500">Image</span>
              <code className="font-mono max-w-[60%] truncate text-right">{effective.executable_path || "—"}</code>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Storage used</span>
              <span className="text-gray-700">
                {(() => {
                  const bytes = (effective as any).image_size_bytes || 0;
                  if (!bytes) return "— (not measured yet; rebuild to populate)";
                  const k = 1024, sizes = ["B", "KB", "MB", "GB", "TB"];
                  const i = Math.floor(Math.log(bytes) / Math.log(k));
                  return `${Math.round((bytes / Math.pow(k, i)) * 100) / 100} ${sizes[i]}`;
                })()}
                {((effective as any).image_size_bytes || 0) > 0 && (
                  <span className="text-gray-400 ml-1">(deleting the node frees this)</span>
                )}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Last scanned</span>
              <span>{(effective as any).scanned_at || "never"}</span>
            </div>
            {(effective as any).image_digest && (
              <div className="flex justify-between">
                <span className="text-gray-500">Image digest</span>
                <code className="font-mono max-w-[60%] truncate text-right text-gray-700">{(effective as any).image_digest}</code>
              </div>
            )}
            {(effective as any).manifest_ref && (
              <div className="flex justify-between">
                <span className="text-gray-500">Manifest</span>
                <code className="font-mono max-w-[60%] truncate text-right text-gray-700">{(effective as any).manifest_ref}</code>
              </div>
            )}
          </div>
        </ScrollArea>

        <DialogFooter className="gap-2 pt-2 border-t flex-col sm:flex-row sm:items-center">
          {justRescanned && (
            <div className="flex-1 text-xs text-blue-700 bg-blue-50 border border-blue-200 rounded px-2 py-1 sm:mr-2">
              <Loader2 className="w-3 h-3 inline mr-1 animate-spin" />
              Rescan queued. Watching for the worker to pick it up — this dialog will refresh as it progresses.
            </div>
          )}
          <Button variant="outline" onClick={onClose}>Close</Button>
          {(status === "rejected" || status === "failed" || status === "pending_scan" || justRescanned) && effective.id !== undefined && (
            <Button
              onClick={() => handleRescanClick(effective.id as number)}
              disabled={rescanning || justRescanned}
            >
              {rescanning || justRescanned ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" /> Rescanning…
                </>
              ) : (
                <>
                  <RefreshCcw className="w-4 h-4 mr-2" /> Rescan Now
                </>
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function VulnGroup({
  label,
  tone,
  description,
  vulns,
}: {
  label: string;
  tone: "red" | "yellow";
  description: string;
  vulns: Vuln[];
}) {
  const toneCls = tone === "red"
    ? "border-red-200 bg-red-50"
    : "border-yellow-200 bg-yellow-50";

  return (
    <div className={`rounded-lg border ${toneCls} p-3`}>
      <div className="flex items-center gap-2 font-semibold text-sm mb-1">
        <Wrench className="w-4 h-4" />
        {label}
      </div>
      <p className="text-xs text-gray-700 mb-2">{description}</p>
      <div className="bg-white/60 rounded border text-xs max-h-48 overflow-auto">
        <table className="w-full">
          <thead className="sticky top-0 bg-white/95">
            <tr>
              <th className="text-left p-1 font-semibold">Package</th>
              <th className="text-left p-1 font-semibold">Installed</th>
              <th className="text-left p-1 font-semibold">Fixed in</th>
              <th className="text-left p-1 font-semibold">CVE</th>
            </tr>
          </thead>
          <tbody>
            {vulns.map((v, i) => (
              <tr key={i} className="border-t">
                <td className="p-1 font-mono">{v.package}</td>
                <td className="p-1 font-mono">{v.installed}</td>
                <td className="p-1 font-mono">{v.fixed || "—"}</td>
                <td className="p-1 font-mono">
                  {v.id ? (
                    <a
                      href={`https://nvd.nist.gov/vuln/detail/${v.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-700 underline inline-flex items-center gap-1"
                    >
                      {v.id} <ExternalLink className="w-3 h-3" />
                    </a>
                  ) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
