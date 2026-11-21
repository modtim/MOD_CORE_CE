"use client";

// Per-service editor (right pane of the stack builder).
//
// Controlled component: the parent owns the `ServiceSpec` and passes it in
// via `service`; every edit emits a full updated `ServiceSpec` through
// `onChange`. Covers name, source (delegated to <ServiceSourcePicker>),
// env (key/value rows), container ports, volumes, GPU (vram_gb), replicas,
// cpu / mem_gb (the reservation vector cost is priced from), and the
// "Expose publicly" toggle.
//
// Parse-sensitive fields (numbers, comma/line lists, env rows) are edited
// through a local buffer so half-typed values ("1." , "8080,") don't get
// clobbered mid-keystroke; the buffer resyncs from props only when the
// committed representation actually differs (e.g. a different service is
// selected).

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Cpu, Plus, X, Globe, Lock } from "lucide-react";
import { authenticatedFetch } from "@/lib/api-client";
import { getApiUrl } from "@/lib/urls";
import { ServiceSpec, ServiceSource, PlacementCheckResponse } from "./types";
import { ServiceSourcePicker } from "./ServiceSourcePicker";

export interface ServiceEditorProps {
  service: ServiceSpec;
  onChange: (service: ServiceSpec) => void;
  // When editing an EXISTING stack, pass its id so the "does this fit the
  // fleet?" check treats the stack's own reserved cards as free (a re-check
  // must not report the running stack as unplaceable by competing with
  // itself). Omitted (null) for a brand-new stack.
  stackId?: number | null;
}

interface EnvRow {
  k: string;
  v: string;
}

interface Buffer {
  envRows: EnvRow[];
  portsText: string;
  volumesText: string;
  cpuText: string;
  memText: string;
  replicasText: string;
  gpuEnabled: boolean;
  vramText: string;
}

const labelClass = "text-xs font-medium text-gray-700";
const inputClass =
  "w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500";

function num(s: string): number {
  const n = parseFloat(s);
  return isNaN(n) ? 0 : n;
}

function intNum(s: string): number {
  const n = parseInt(s, 10);
  return isNaN(n) ? 0 : n;
}

function parsePorts(text: string): number[] {
  return text
    .split(/[\s,]+/)
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => parseInt(t, 10))
    .filter((n) => !isNaN(n) && n > 0);
}

function parseVolumes(text: string): string[] {
  return text
    .split("\n")
    .map((t) => t.trim())
    .filter(Boolean);
}

function envRowsToRecord(rows: EnvRow[]): Record<string, string> {
  const rec: Record<string, string> = {};
  for (const { k, v } of rows) {
    const key = k.trim();
    if (key) rec[key] = v;
  }
  return rec;
}

function bufferFromService(s: ServiceSpec): Buffer {
  return {
    envRows: Object.entries(s.env).map(([k, v]) => ({ k, v })),
    portsText: s.ports.join(", "),
    volumesText: s.volumes.join("\n"),
    cpuText: String(s.cpu ?? 0),
    memText: String(s.mem_gb ?? 0),
    replicasText: String(s.replicas ?? 1),
    gpuEnabled: !!s.gpu,
    vramText: String(s.gpu?.vram_gb ?? 0),
  };
}

// The committed (parsed) representation of a buffer — the parts of the
// ServiceSpec the buffer is responsible for. Used both to build the
// emitted spec and to detect external changes.
function bufferToParts(b: Buffer) {
  return {
    env: envRowsToRecord(b.envRows),
    ports: parsePorts(b.portsText),
    volumes: parseVolumes(b.volumesText),
    cpu: num(b.cpuText),
    mem_gb: num(b.memText),
    replicas: intNum(b.replicasText),
    gpu: b.gpuEnabled ? { vram_gb: num(b.vramText) } : null,
  };
}

function partsSig(parts: {
  env: Record<string, string>;
  ports: number[];
  volumes: string[];
  cpu: number;
  mem_gb: number;
  replicas: number;
  gpu: { vram_gb: number } | null;
}): string {
  return JSON.stringify({
    env: Object.keys(parts.env)
      .sort()
      .map((k) => [k, parts.env[k]]),
    ports: parts.ports,
    volumes: parts.volumes,
    cpu: parts.cpu,
    mem_gb: parts.mem_gb,
    replicas: parts.replicas,
    gpu: parts.gpu,
  });
}

// Best-effort preview of the magic URL an exposed service will get. The
// real MagicDNS name is minted server-side at deploy; this shows the shape
// so the operator isn't guessing.
function magicUrlPreview(name: string): string {
  const slug =
    name
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "service";
  let base = "your-domain";
  if (typeof window !== "undefined" && window.location?.hostname) {
    const host = window.location.hostname;
    const parts = host.split(".");
    base = parts.length > 1 ? parts.slice(1).join(".") : host;
  }
  return `https://${slug}.${base}`;
}

export function ServiceEditor({ service, onChange, stackId }: ServiceEditorProps) {
  const [buffer, setBuffer] = useState<Buffer>(() => bufferFromService(service));

  // Track the last signature we emitted so we only resync the buffer on a
  // genuine external change (e.g. a different service selected), not on our
  // own commits echoing back through props.
  const committedSig = useMemo(() => partsSig(bufferToParts(buffer)), [buffer]);
  const committedSigRef = useRef(committedSig);
  committedSigRef.current = committedSig;

  useEffect(() => {
    const incoming = partsSig({
      env: service.env,
      ports: service.ports,
      volumes: service.volumes,
      cpu: service.cpu,
      mem_gb: service.mem_gb,
      replicas: service.replicas,
      gpu: service.gpu ? { vram_gb: service.gpu.vram_gb } : null,
    });
    if (incoming !== committedSigRef.current) {
      setBuffer(bufferFromService(service));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [service]);

  // "Does this fit the fleet?" verdict for the reserved GPU, straight from
  // the same placement engine a deploy claim uses. Debounced; failures are
  // silent — a capacity lookup must never block editing the spec.
  const [fit, setFit] = useState<PlacementCheckResponse | null>(null);
  const fitReqSeq = useRef(0);
  const fitVram = buffer.gpuEnabled ? num(buffer.vramText) : 0;

  useEffect(() => {
    if (fitVram <= 0) {
      setFit(null);
      return;
    }
    const seq = ++fitReqSeq.current;
    const handle = setTimeout(async () => {
      try {
        const resp = await authenticatedFetch(
          `${getApiUrl()}/stacks/placement-check`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              services: [{
                name: service.name,
                accelerator: {
                  kind: "any",
                  count: 1,
                  min_vram_gb: fitVram,
                },
              }],
              // Re-checking an existing stack: treat its own reservation as
              // free so a running stack doesn't read as unplaceable.
              ...(stackId != null ? { stack_id: stackId } : {}),
            }),
          },
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: PlacementCheckResponse = await resp.json();
        if (seq === fitReqSeq.current) setFit(data);
      } catch {
        if (seq === fitReqSeq.current) setFit(null);
      }
    }, 500);
    return () => clearTimeout(handle);
  }, [fitVram, stackId]);

  // Commit a buffer change: update local buffer AND emit the merged spec.
  const commitBuffer = (next: Buffer) => {
    setBuffer(next);
    const parts = bufferToParts(next);
    onChange({ ...service, ...parts });
  };

  // Direct (non-buffered) field edits — strings / booleans, no parsing.
  const patch = (fields: Partial<ServiceSpec>) => onChange({ ...service, ...fields });

  const exposed = service.expose;

  return (
    <div className="space-y-5">
      {/* Name */}
      <div className="space-y-1.5">
        <label className={labelClass}>
          Service name <span className="text-red-500">*</span>
        </label>
        <input
          type="text"
          value={service.name}
          onChange={(e) => patch({ name: e.target.value })}
          placeholder="web"
          className={inputClass}
          autoComplete="off"
        />
      </div>

      {/* Source */}
      <ServiceSourcePicker
        source={service.source}
        onChange={(source: ServiceSource) => patch({ source })}
      />

      {/* Environment */}
      <div className="space-y-1.5">
        <label className={labelClass}>Environment variables</label>
        <div className="space-y-1.5">
          {buffer.envRows.length === 0 && (
            <p className="text-[11px] text-gray-400">No variables.</p>
          )}
          {buffer.envRows.map((row, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <input
                type="text"
                value={row.k}
                onChange={(e) => {
                  const rows = buffer.envRows.slice();
                  rows[i] = { ...rows[i], k: e.target.value };
                  commitBuffer({ ...buffer, envRows: rows });
                }}
                placeholder="KEY"
                className={inputClass + " flex-1 font-mono"}
                autoComplete="off"
              />
              <span className="text-gray-400 text-xs">=</span>
              <input
                type="text"
                value={row.v}
                onChange={(e) => {
                  const rows = buffer.envRows.slice();
                  rows[i] = { ...rows[i], v: e.target.value };
                  commitBuffer({ ...buffer, envRows: rows });
                }}
                placeholder="value"
                className={inputClass + " flex-1 font-mono"}
                autoComplete="off"
              />
              <button
                type="button"
                aria-label="Remove variable"
                onClick={() => {
                  const rows = buffer.envRows.filter((_, j) => j !== i);
                  commitBuffer({ ...buffer, envRows: rows });
                }}
                className="text-gray-400 hover:text-red-600 flex-shrink-0"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() =>
            commitBuffer({
              ...buffer,
              envRows: [...buffer.envRows, { k: "", v: "" }],
            })
          }
          className="flex items-center gap-1 text-[11px] font-medium text-indigo-600 hover:text-indigo-800"
        >
          <Plus className="h-3 w-3" /> Add variable
        </button>
      </div>

      {/* Ports + volumes */}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <label className={labelClass}>Container ports</label>
          <input
            type="text"
            value={buffer.portsText}
            onChange={(e) => commitBuffer({ ...buffer, portsText: e.target.value })}
            placeholder="8080, 9090"
            className={inputClass}
            autoComplete="off"
          />
          <p className="text-[11px] text-gray-400">
            Container ports only — never host ports.
          </p>
        </div>
        <div className="space-y-1.5">
          <label className={labelClass}>Volumes</label>
          <textarea
            value={buffer.volumesText}
            onChange={(e) => commitBuffer({ ...buffer, volumesText: e.target.value })}
            placeholder={"data:/var/lib/data\nconfig:/etc/app"}
            rows={2}
            className={inputClass + " font-mono resize-y"}
          />
          <p className="text-[11px] text-gray-400">One per line.</p>
        </div>
      </div>

      {/* Reserved capacity — reserved == hard cap == what you pay. */}
      <div className="space-y-1.5">
        <div className="flex items-baseline justify-between">
          <label className={labelClass}>Reserved capacity</label>
          <span className="text-[11px] text-indigo-600 font-medium">reserved = hard cap = what you pay</span>
        </div>
        <p className="text-[11px] text-gray-400">
          The service is guaranteed this much and cannot exceed it — so your bill equals exactly what you reserve.
        </p>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div className="space-y-1.5">
          <label className={labelClass}>CPU cores</label>
          <input
            type="number"
            min={0}
            step="0.25"
            value={buffer.cpuText}
            onChange={(e) => commitBuffer({ ...buffer, cpuText: e.target.value })}
            className={inputClass}
          />
        </div>
        <div className="space-y-1.5">
          <label className={labelClass}>Memory (GB)</label>
          <input
            type="number"
            min={0}
            step="0.5"
            value={buffer.memText}
            onChange={(e) => commitBuffer({ ...buffer, memText: e.target.value })}
            className={inputClass}
          />
        </div>
        <div className="space-y-1.5">
          <label className={labelClass}>Replicas</label>
          <input
            type="number"
            min={1}
            step="1"
            value={buffer.replicasText}
            onChange={(e) => commitBuffer({ ...buffer, replicasText: e.target.value })}
            className={inputClass}
          />
        </div>
      </div>

      {/* GPU reservation */}
      <div className="space-y-2 rounded-md border border-gray-200 p-3">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={buffer.gpuEnabled}
            onChange={(e) =>
              commitBuffer({ ...buffer, gpuEnabled: e.target.checked })
            }
            className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
          />
          <Cpu className="h-3.5 w-3.5 text-gray-500" />
          <span className={labelClass}>Reserve a GPU</span>
        </label>
        {buffer.gpuEnabled && (
          <div className="space-y-1.5 pl-6">
            <label className={labelClass}>VRAM (GB)</label>
            <input
              type="number"
              min={0}
              step="1"
              value={buffer.vramText}
              onChange={(e) => commitBuffer({ ...buffer, vramText: e.target.value })}
              className={inputClass + " max-w-[140px]"}
            />
            <p className="text-[11px] text-gray-400">
              A reserved GPU is whole-device (exclusively yours) and billed at its full reserved cost.
            </p>
            {fit && fit.placeable && (
              <p className="text-[11px] text-emerald-600" data-testid="service-fit-ok">
                Fits on {(fit.services?.[0]?.best_worker ||
                          fit.services?.[0]?.name ||
                          "the fleet")}
              </p>
            )}
            {fit && !fit.placeable && (() => {
              const s = fit.services?.[0];
              const sf = s?.shortfall;
              return (
                <p className="text-[11px] text-red-600" data-testid="service-fit-shortfall">
                  No worker can place this: needs {sf?.needed_gb ?? "?"} GB,
                  {" "}largest free is {sf?.max_available_gb ?? 0} GB
                  {sf?.kind ? ` (${sf.kind})` : ""}.
                </p>
              );
            })()}
          </div>
        )}
      </div>

      {/* Expose publicly */}
      <div
        className={
          "space-y-2 rounded-md border p-3 " +
          (exposed ? "border-emerald-200 bg-emerald-50" : "border-gray-200")
        }
      >
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={exposed}
            onChange={(e) => patch({ expose: e.target.checked })}
            className="h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
          />
          {exposed ? (
            <Globe className="h-3.5 w-3.5 text-emerald-600" />
          ) : (
            <Lock className="h-3.5 w-3.5 text-gray-500" />
          )}
          <span className={labelClass}>Expose publicly</span>
        </label>
        {exposed ? (
          <div className="pl-6 space-y-1">
            <p className="text-[11px] text-emerald-700">
              Gets a magic URL (mesh + Caddy on-demand TLS). Host-port publishing
              is dropped — it&apos;s reached over the mesh, so it can&apos;t collide
              with another stack on the same container port.
            </p>
            <p className="text-[11px] text-gray-500">
              URL (approx.):{" "}
              <span className="font-mono text-gray-700">
                {magicUrlPreview(service.name)}
              </span>
            </p>
          </div>
        ) : (
          <p className="text-[11px] text-gray-400 pl-6">
            Internal only — reachable inside the stack&apos;s compose network, binds
            no host port.
          </p>
        )}
      </div>
    </div>
  );
}

export default ServiceEditor;
