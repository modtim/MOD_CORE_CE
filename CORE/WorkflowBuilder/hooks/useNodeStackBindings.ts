"use client";

// generalized-stack-wake D12 — fetch the node_kind -> managed-stack binding map
// so the canvas can overlay each bound node with its stack's live wake-state.
//
// Routes through the SHARED poller (usePollingBackoff + lib/poller) exactly like
// the other migrated sites (RunRail, node-status, etc.): a 10s base cadence, it
// backs off on 5xx, stops-after-N with the reconnect banner, pauses when the tab
// is hidden and is single-flight. Enabled only when a workflow is open, so a
// closed builder does no polling. The poll fn throws (via pollErrorForResponse)
// on failure so the poller can classify + back off; a transient blip keeps the
// last-known map on screen.

import { useCallback, useState } from "react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import { authenticatedFetch } from "@/lib/api-client";
import { getApiUrl } from "@/lib/urls";
import type { NodeBindingsMap, StackInfoMap } from "@/lib/node-stack-bindings";

export interface NodeStackBindings {
  // node_kind -> managed-stack auto-binding.
  bindings: NodeBindingsMap;
  // stack_name -> {family,state}. Used to resolve a per-instance
  // data.requires_stack assignment to a live wake-state.
  stacks: StackInfoMap;
}

export function useNodeStackBindings(enabled: boolean): NodeStackBindings {
  const [bindings, setBindings] = useState<NodeBindingsMap>({});
  const [stacks, setStacks] = useState<StackInfoMap>({});

  const fetchBindings = useCallback(async () => {
    const res = await authenticatedFetch(`${getApiUrl()}/stacks/node-bindings`);
    const err = pollErrorForResponse(res);
    if (err) throw err;
    const data = await res.json();
    const map = data?.bindings;
    if (map && typeof map === "object") {
      setBindings(map as NodeBindingsMap);
    }
    const stackMap = data?.stacks;
    if (stackMap && typeof stackMap === "object") {
      setStacks(stackMap as StackInfoMap);
    }
  }, []);

  usePollingBackoff(fetchBindings, { baseMs: 10000, enabled }, []);

  return { bindings, stacks };
}
