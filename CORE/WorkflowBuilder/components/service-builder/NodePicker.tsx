"use client";

// Node picker — pull a MOD node into the stack as a long-lived service.
//
// Fetches the tenant's node library (the same list the node-library
// sidebar uses: GET /node-definitions/?limit=1000&offset=0) and lets the
// operator pick one. On pick it emits a `node` ServiceSource carrying the
// chosen `node_definition_id`; the API's node→service resolver (Group B)
// de-specializes it into a compose service at assemble time.

import React, { useEffect, useMemo, useState } from "react";
import { Loader2, Search } from "lucide-react";
import { authenticatedFetch } from "@/lib/api-client";
import { getApiUrl } from "@/lib/urls";
import type { NodeDefinition } from "@/lib/types";
import { ServiceSource } from "./types";

const API_URL = getApiUrl();

export interface NodePickerProps {
  // Currently selected node id (from the service's source), if any.
  value?: number;
  onChange: (source: ServiceSource) => void;
}

export function NodePicker({ value, onChange }: NodePickerProps) {
  const [nodes, setNodes] = useState<NodeDefinition[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const resp = await authenticatedFetch(
          `${API_URL}/nodes/?limit=1000&offset=0`,
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: NodeDefinition[] = await resp.json();
        if (cancelled) return;
        setNodes(Array.isArray(data) ? data : []);
      } catch (e) {
        if (!cancelled) setError((e as Error).message || "Could not load nodes.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return nodes;
    return nodes.filter(
      (n) =>
        n.name?.toLowerCase().includes(q) ||
        n.description?.toLowerCase().includes(q),
    );
  }, [nodes, query]);

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search nodes…"
          className="w-full pl-8 pr-3 py-2 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500"
          autoComplete="off"
        />
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-xs text-gray-400 px-1 py-2">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading nodes…
        </div>
      ) : error ? (
        <p className="text-xs text-red-600 px-1 py-2">{error}</p>
      ) : filtered.length === 0 ? (
        <p className="text-xs text-gray-400 px-1 py-2">
          {nodes.length === 0
            ? "No nodes in your library yet."
            : "No nodes match your search."}
        </p>
      ) : (
        <div className="max-h-52 overflow-y-auto space-y-1 pr-0.5">
          {filtered.map((node) => {
            const selected = node.id === value;
            return (
              <button
                key={node.id}
                type="button"
                onClick={() => onChange({ kind: "node", node_definition_id: node.id })}
                className={
                  "w-full text-left rounded-md border px-2.5 py-2 transition-colors " +
                  (selected
                    ? "border-indigo-300 bg-indigo-50"
                    : "border-gray-200 bg-white hover:bg-gray-50")
                }
              >
                <div
                  className={
                    "text-xs font-medium truncate " +
                    (selected ? "text-indigo-800" : "text-gray-800")
                  }
                >
                  {node.name}
                </div>
                {node.description && (
                  <div className="text-[11px] text-gray-400 truncate">
                    {node.description}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default NodePicker;
