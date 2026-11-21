"use client";

import { useEffect, useRef } from "react";
import { authenticatedFetch } from "@/lib/api-client";
import { getApiUrl } from "@/lib/urls";
import { toast } from "@/components/ui/use-toast";
import type { WorkflowSummary } from "@/lib/types";

interface AccessDenied {
  workflowId: number;
  workflowName: string;
}

interface UseOpenWorkflowFromQueryArgs {
  /** Gate: only fire once auth has resolved to a real token. No token means
   * NOTHING may be fetched about the workflow (an anonymous probe would
   * return nothing to show and leak existence); the cold-load denial for a
   * stale/no-token user is raised by initializeAuth instead, with the same
   * generic `workflow <id>` name. */
  accessToken: string | null;
  /** useWorkflowApi's deep-link open handler (openWorkflowById): loads the
   * workflow's LATEST version straight onto the canvas — name set, dirty
   * flag cleared, no Load dialog. Same sealed / 403-access-denied handling
   * as the Load-list flow. */
  onOpenWorkflow: (workflow: WorkflowSummary) => void | Promise<void>;
  /** useWorkflowApi's setAccessDenied — same "request access" banner the
   * normal list-open flow shows on a 403 workflow_access_denied. */
  setAccessDenied: (denied: AccessDenied | null) => void;
}

/**
 * WB deep-link landing: `?workflow=<id>` on the main page.
 *
 * Dashboard Library's "Open workflow" button (library.js wbUrl) points here
 * with the tenant's installed_workflow_id so a click opens straight to that
 * workflow instead of leaving the user to hunt for it in the Load dialog.
 *
 * Runs at most once per page load (tried ref). Always strips `workflow` from
 * the URL via history.replaceState so a refresh never re-triggers it,
 * regardless of success or failure below.
 */
export function useOpenWorkflowFromQuery({
  accessToken,
  onOpenWorkflow,
  setAccessDenied,
}: UseOpenWorkflowFromQueryArgs): void {
  const triedRef = useRef(false);

  useEffect(() => {
    if (triedRef.current) return;
    if (!accessToken) return;
    if (typeof window === "undefined") return;

    const params = new URLSearchParams(window.location.search);
    const raw = params.get("workflow");
    if (raw === null) return;

    triedRef.current = true;

    const stripParam = () => {
      try {
        const url = new URL(window.location.href);
        url.searchParams.delete("workflow");
        window.history.replaceState({}, "", url.toString());
      } catch {
        // best-effort; never let this block the open attempt
      }
    };

    const id = Number(raw);
    if (!raw.trim() || !Number.isInteger(id) || id <= 0) {
      stripParam();
      toast({
        title: "Can't open workflow",
        description: `"${raw}" is not a valid workflow id.`,
        variant: "destructive",
      });
      return;
    }

    (async () => {
      try {
        const resp = await authenticatedFetch(`${getApiUrl()}/road/${id}`, {
          headers: { Accept: "application/json" },
        });
        stripParam();

        if (resp.status === 404) {
          toast({
            title: "Workflow not found",
            description: `Workflow ${id} doesn't exist or was removed.`,
            variant: "destructive",
          });
          return;
        }
        if (resp.status === 403) {
          const body = await resp.json().catch(() => ({} as any));
          const detail = (body as any)?.detail;
          if (detail && detail.code === "workflow_access_denied") {
            setAccessDenied({
              workflowId: id,
              workflowName: detail.workflow_name || `workflow ${id}`,
            });
          } else {
            toast({
              title: "Access denied",
              description: `You don't have access to workflow ${id}.`,
              variant: "destructive",
            });
          }
          return;
        }
        if (resp.status === 401) {
          // The session died between load and fetch (authenticatedFetch already
          // tried a refresh). Same generic name as the 403 fallback and the
          // cold-load denial; the 401 body is never read or shown.
          setAccessDenied({ workflowId: id, workflowName: `workflow ${id}` });
          return;
        }
        if (!resp.ok) {
          toast({
            title: "Can't open workflow",
            description: `Failed to load workflow ${id} (${resp.status}).`,
            variant: "destructive",
          });
          return;
        }

        const workflow: WorkflowSummary = await resp.json();
        await onOpenWorkflow(workflow);
      } catch (error) {
        stripParam();
        if ((error as Error)?.message === "Session expired") return;
        toast({
          title: "Can't open workflow",
          description: (error as Error)?.message || `Failed to load workflow ${id}.`,
          variant: "destructive",
        });
      }
    })();
  }, [accessToken, onOpenWorkflow, setAccessDenied]);
}
