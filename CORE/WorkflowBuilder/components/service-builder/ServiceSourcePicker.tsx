"use client";

// Service source picker.
//
// Chooses the source kind for a service (image | node | git | script |
// site) and collects the fields that kind needs, emitting an updated
// `ServiceSource` (Group 0 contract). Every source ultimately resolves to
// a compose service on the server; this component only gathers input.
//
// Field map (mirrors ServiceSource):
//   image  → image
//   node   → node_definition_id   (delegated to <NodePicker>)
//   git    → repo_url, ref, build_context?
//   script → script, base_image, filename?
//   site   → site_source (upload|repo), site_ref

import React from "react";
import { Container, Boxes, GitBranch, FileCode, Globe } from "lucide-react";
import { ServiceSource, ServiceSourceKind } from "./types";
import { NodePicker } from "./NodePicker";

export interface ServiceSourcePickerProps {
  source: ServiceSource;
  onChange: (source: ServiceSource) => void;
}

const KINDS: { kind: ServiceSourceKind; label: string; icon: React.ReactNode }[] = [
  { kind: "image", label: "Image", icon: <Container className="h-3.5 w-3.5" /> },
  { kind: "node", label: "Node", icon: <Boxes className="h-3.5 w-3.5" /> },
  { kind: "git", label: "Git", icon: <GitBranch className="h-3.5 w-3.5" /> },
  { kind: "script", label: "Script", icon: <FileCode className="h-3.5 w-3.5" /> },
  { kind: "site", label: "Site", icon: <Globe className="h-3.5 w-3.5" /> },
];

const labelClass = "text-xs font-medium text-gray-700";
const inputClass =
  "w-full px-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500";

export function ServiceSourcePicker({ source, onChange }: ServiceSourcePickerProps) {
  // Switching kind starts a clean source of that kind so stale fields from
  // the previous kind never leak into the assemble request.
  const selectKind = (kind: ServiceSourceKind) => {
    if (kind === source.kind) return;
    onChange({ kind });
  };

  // Merge a partial field update onto the current source.
  const patch = (fields: Partial<ServiceSource>) => {
    onChange({ ...source, ...fields });
  };

  return (
    <div className="space-y-3">
      <div>
        <label className={labelClass}>Source</label>
        <div
          role="tablist"
          aria-label="Service source kind"
          className="mt-1.5 grid grid-cols-5 gap-1"
        >
          {KINDS.map(({ kind, label, icon }) => {
            const active = kind === source.kind;
            return (
              <button
                key={kind}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => selectKind(kind)}
                className={
                  "flex flex-col items-center gap-1 rounded-md border px-1 py-2 text-[10px] font-medium transition-colors " +
                  (active
                    ? "border-indigo-300 bg-indigo-50 text-indigo-700"
                    : "border-gray-200 bg-white text-gray-500 hover:bg-gray-50")
                }
              >
                {icon}
                {label}
              </button>
            );
          })}
        </div>
      </div>

      {source.kind === "image" && (
        <div className="space-y-1.5">
          <label className={labelClass}>Image reference</label>
          <input
            type="text"
            value={source.image ?? ""}
            onChange={(e) => patch({ image: e.target.value })}
            placeholder="e.g. nginx:1.27 or ghcr.io/org/app:tag"
            className={inputClass}
            autoComplete="off"
          />
        </div>
      )}

      {source.kind === "node" && (
        <div className="space-y-1.5">
          <label className={labelClass}>MOD node</label>
          <NodePicker
            value={source.node_definition_id}
            onChange={(next) => onChange(next)}
          />
          <p className="text-[11px] text-gray-400">
            The node is de-specialized into a long-lived service (no smoke-guard,
            no data-io) at deploy.
          </p>
        </div>
      )}

      {source.kind === "git" && (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <label className={labelClass}>Repository URL</label>
            <input
              type="text"
              value={source.repo_url ?? ""}
              onChange={(e) => patch({ repo_url: e.target.value })}
              placeholder="https://github.com/org/repo"
              className={inputClass}
              autoComplete="off"
            />
          </div>
          <div className="space-y-1.5">
            <label className={labelClass}>Ref (branch / tag / commit)</label>
            <input
              type="text"
              value={source.ref ?? ""}
              onChange={(e) => patch({ ref: e.target.value })}
              placeholder="main"
              className={inputClass}
              autoComplete="off"
            />
          </div>
          <div className="space-y-1.5">
            <label className={labelClass}>
              Build context <span className="text-gray-400">(optional)</span>
            </label>
            <input
              type="text"
              value={source.build_context ?? ""}
              onChange={(e) => patch({ build_context: e.target.value })}
              placeholder="."
              className={inputClass}
              autoComplete="off"
            />
          </div>
        </div>
      )}

      {source.kind === "script" && (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <label className={labelClass}>Base image</label>
            <input
              type="text"
              value={source.base_image ?? ""}
              onChange={(e) => patch({ base_image: e.target.value })}
              placeholder="python:3.11-slim"
              className={inputClass}
              autoComplete="off"
            />
          </div>
          <div className="space-y-1.5">
            <label className={labelClass}>
              Filename <span className="text-gray-400">(optional)</span>
            </label>
            <input
              type="text"
              value={source.filename ?? ""}
              onChange={(e) => patch({ filename: e.target.value })}
              placeholder="app.py"
              className={inputClass}
              autoComplete="off"
            />
          </div>
          <div className="space-y-1.5">
            <label className={labelClass}>Script</label>
            <textarea
              value={source.script ?? ""}
              onChange={(e) => patch({ script: e.target.value })}
              placeholder="#!/usr/bin/env python&#10;# your long-running service entrypoint"
              rows={6}
              className={inputClass + " font-mono resize-y"}
            />
          </div>
        </div>
      )}

      {source.kind === "site" && (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <label className={labelClass}>Site source</label>
            <select
              value={source.site_source ?? "upload"}
              onChange={(e) =>
                patch({ site_source: e.target.value as "upload" | "repo" })
              }
              className={inputClass + " bg-white"}
            >
              <option value="upload">Uploaded files</option>
              <option value="repo">Built from repo</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <label className={labelClass}>
              Site ref{" "}
              <span className="text-gray-400">
                ({source.site_source === "repo" ? "repo URL / ref" : "upload id"})
              </span>
            </label>
            <input
              type="text"
              value={source.site_ref ?? ""}
              onChange={(e) => patch({ site_ref: e.target.value })}
              placeholder={
                source.site_source === "repo"
                  ? "https://github.com/org/site#main"
                  : "upload-bundle-id"
              }
              className={inputClass}
              autoComplete="off"
            />
          </div>
          <p className="text-[11px] text-gray-400">
            Served as a long-lived nginx service over your files or a built repo.
          </p>
        </div>
      )}
    </div>
  );
}

export default ServiceSourcePicker;
