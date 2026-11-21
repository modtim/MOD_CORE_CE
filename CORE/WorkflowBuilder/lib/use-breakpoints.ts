"use client";

// sz-2.10d — React hook wrapper over lib/breakpoints.ts for the canvas.
//
// useBreakpoints({apiUrl, accessToken, workflowId, versionId}) ->
//   { map, loading, error, isActive(nodeId), toggle(nodeId) }
//
// Loads ONCE per (workflowId, versionId) — and never at all when any id is
// missing (unsaved workflow: nothing to breakpoint). toggle() is optimistic:
// the local map flips immediately so the UI reacts without waiting for the
// round-trip, and rolls back + surfaces `error` if the API call rejects.
//
// breakpointMenuLabel() is exported pure so the context-menu item can be
// unit-tested without rendering the whole 2900-line canvas component.

import { useEffect, useRef, useState } from "react";
import {
  listBreakpoints,
  setBreakpoint,
  removeBreakpoint,
  breakpointMap,
  isBreakpointActive,
  type Breakpoint,
} from "@/lib/breakpoints";

/**
 * Pure: the right-click menu label for a node's current breakpoint state.
 * `hasBreakpoint` = the node already has an ENABLED breakpoint.
 */
export function breakpointMenuLabel(hasBreakpoint: boolean): string {
  return hasBreakpoint ? "Remove breakpoint" : "Add breakpoint";
}

export interface BreakpointsProps {
  apiUrl: string;
  accessToken: string | null;
  workflowId: (string | number) | null | undefined;
  versionId: (string | number) | null | undefined;
}

export interface BreakpointsState {
  map: Map<string, Breakpoint>;
  loading: boolean;
  error: string | null;
  isActive: (nodeId: string) => boolean;
  toggle: (nodeId: string) => Promise<void>;
}

export function useBreakpoints(props: BreakpointsProps): BreakpointsState {
  const { apiUrl, accessToken, workflowId, versionId } = props;

  const [state, setState] = useState<{
    map: Map<string, Breakpoint>;
    loading: boolean;
    error: string | null;
  }>({ map: new Map(), loading: false, error: null });

  // Mirror of the current map, so imperative callers (toggle) and the
  // isActive() predicate always read the freshest value without waiting for
  // the surrounding component to re-render.
  const mapRef = useRef<Map<string, Breakpoint>>(new Map());
  const loadedForRef = useRef<string | null>(null);

  useEffect(() => {
    if (!apiUrl || !accessToken || !workflowId || !versionId) return;
    const wfId = String(workflowId);
    const verId = String(versionId);
    const key = `${wfId}:${verId}:${accessToken}`;
    if (loadedForRef.current === key) return;
    loadedForRef.current = key;
    setState((prev) => (
      prev.loading ? prev : { ...prev, loading: true }
    ));
    void (async () => {
      try {
        const list = await listBreakpoints(apiUrl, accessToken, wfId, verId);
        const next = breakpointMap(list);
        mapRef.current = next;
        setState({ map: next, loading: false, error: null });
      } catch (err: any) {
        setState((prev) => ({ ...prev, loading: false, error: err?.message || "breakpoints request failed" }));
      }
    })();
  }, [apiUrl, accessToken, workflowId, versionId]);

  const toggle = async (nodeId: string) => {
    if (!apiUrl || !accessToken || !workflowId || !versionId) return;
    const wfId = String(workflowId);
    const verId = String(versionId);

    const before = mapRef.current;
    const active = isBreakpointActive(before, nodeId);

    // Optimistic flip: absent-or-disabled -> enabled; enabled -> dropped.
    const optimistic = new Map(before);
    if (active) {
      optimistic.delete(nodeId);
    } else {
      optimistic.set(nodeId, {
        id: 0,
        node_id: nodeId,
        enabled: true,
        condition: null,
        hit_count: 0,
        last_hit_at: null,
        created_at: "",
        updated_at: "",
      });
    }
    mapRef.current = optimistic;
    setState((prev) => ({ ...prev, map: optimistic, loading: true, error: null }));

    try {
      if (active) {
        await removeBreakpoint(apiUrl, accessToken, wfId, verId, nodeId);
      } else {
        const saved = await setBreakpoint(
          apiUrl,
          accessToken,
          wfId,
          verId,
          nodeId,
          { enabled: true, condition: null }
        );
        const accepted = new Map(mapRef.current);
        accepted.set(nodeId, saved);
        mapRef.current = accepted;
      }
      setState((prev) => ({ ...prev, map: mapRef.current, loading: false }));
    } catch (err: any) {
      // Roll the optimistic flip back and surface the failure.
      mapRef.current = before;
      setState((prev) => ({
        ...prev,
        map: before,
        loading: false,
        error: err?.message || "breakpoints request failed",
      }));
    }
  };

  return {
    map: state.map,
    loading: state.loading,
    error: state.error,
    isActive: (nodeId: string) => isBreakpointActive(mapRef.current, nodeId),
    toggle,
  };
}
