/**
 * Stack wizard — docker-compose import client contract (stack-wizard-compose-import).
 *
 * A managed MOD stack IS a docker-compose project. The stack wizard can import a
 * user's own compose: the backend PREVIEW endpoint
 * (`POST /persistent-stacks/import-compose`) parses + normalizes the YAML and
 * returns a candidate `compose_spec` plus a structured `needs_confirmation` list
 * of the MOD `x-mod` metadata the compose can't express (accelerator, egress,
 * readiness, and the stack-level ha_class / storage_class). The FINALIZE endpoint
 * (`POST /persistent-stacks/import-compose/finalize`) re-normalizes, applies the
 * confirmations, validates through the existing loop, and creates the stack.
 *
 * This module is the pure, framework-free glue the panel uses to turn the
 * preview's needs-confirmation list + the user's answers into the finalize
 * request body. Kept side-effect-free (no fetch, no React) so it is unit-tested
 * with jest and reused by the UI without a docker build.
 */

export type NeedScope = "service" | "stack";

/** One item the user must confirm, as returned by the preview endpoint. */
export interface NeedItem {
  scope: NeedScope;
  field: string; // e.g. "accelerator", "accelerator.min_vram_gb", "egress",
  //             // "readiness.path", "storage_class", "ha_class"
  suggested: unknown;
  reason: string;
  required: boolean;
  service?: string;
  options?: unknown[];
}

export interface ComposeImportPreview {
  name: string;
  compose_spec: Record<string, unknown>;
  needs_confirmation: NeedItem[];
  storage_class: string;
}

export interface AcceleratorConfirmation {
  required?: boolean;
  min_vram_gb?: number | null;
  count?: number;
  kind?: string;
}

export interface EgressConfirmation {
  mode: "none" | "api" | "hosts";
  hosts?: string[];
}

export interface ReadinessConfirmation {
  port?: number;
  path?: string;
}

export interface ServiceConfirmation {
  accelerator?: AcceleratorConfirmation | null;
  egress?: EgressConfirmation | null;
  readiness?: ReadinessConfirmation | null;
}

export interface Confirmations {
  services?: Record<string, ServiceConfirmation>;
  ha_class?: string;
  storage_class?: string;
}

export interface FinalizeRequest {
  name: string;
  compose: string;
  confirmations: Confirmations;
}

/**
 * The items still blocking a finalize: any `required` need the user has not
 * answered yet. A non-required need (e.g. an optional egress policy) never
 * blocks — its suggested/default value is used. Answered-ness is decided per
 * scope/field against the assembled confirmations.
 */
export function outstandingRequired(
  needs: NeedItem[],
  confirmations: Confirmations,
): NeedItem[] {
  return needs.filter((n) => n.required && !isAnswered(n, confirmations));
}

function isAnswered(need: NeedItem, confirmations: Confirmations): boolean {
  if (need.scope === "stack") {
    if (need.field === "storage_class") return !!confirmations.storage_class;
    if (need.field === "ha_class") return !!confirmations.ha_class;
    return true;
  }
  const svc = need.service ? confirmations.services?.[need.service] : undefined;
  if (!svc) return false;
  // The only currently-required service need is accelerator VRAM.
  if (need.field === "accelerator.min_vram_gb") {
    const vram = svc.accelerator?.min_vram_gb;
    return typeof vram === "number" && vram > 0;
  }
  return true;
}

/**
 * Seed a confirmations object from the preview's suggested values, so the UI
 * starts from sensible defaults the user can override. Suggested `null` (no
 * suggestion) is skipped. Stack-level storage_class/ha_class come from their
 * suggested values (or the preview's top-level storage_class).
 */
export function seedConfirmations(preview: ComposeImportPreview): Confirmations {
  const out: Confirmations = { services: {} };
  for (const need of preview.needs_confirmation) {
    if (need.scope === "stack") {
      if (need.field === "storage_class") {
        out.storage_class = (need.suggested as string) || preview.storage_class;
      } else if (need.field === "ha_class") {
        out.ha_class = (need.suggested as string) || "external";
      }
      continue;
    }
    if (!need.service || need.suggested == null) continue;
    const svc = (out.services![need.service] ||= {});
    if (need.field === "accelerator") {
      svc.accelerator = need.suggested as AcceleratorConfirmation;
    } else if (need.field === "egress") {
      svc.egress = need.suggested as EgressConfirmation;
    } else if (need.field === "readiness.path") {
      svc.readiness = { ...(svc.readiness || {}), path: need.suggested as string };
    }
  }
  return out;
}

/**
 * Build the finalize request body. We send the raw `compose` YAML (not the
 * normalized spec) so the server re-normalizes it — a client cannot smuggle a
 * spec past normalization. Empty service confirmation entries are pruned so the
 * payload carries only what the user actually set.
 */
export function buildFinalizeRequest(
  name: string,
  composeYaml: string,
  confirmations: Confirmations,
): FinalizeRequest {
  const services: Record<string, ServiceConfirmation> = {};
  for (const [svc, conf] of Object.entries(confirmations.services || {})) {
    const pruned: ServiceConfirmation = {};
    if ("accelerator" in conf) pruned.accelerator = conf.accelerator ?? null;
    if ("egress" in conf) pruned.egress = conf.egress ?? null;
    if ("readiness" in conf) pruned.readiness = conf.readiness ?? null;
    if (Object.keys(pruned).length) services[svc] = pruned;
  }
  const out: Confirmations = { services };
  if (confirmations.storage_class) out.storage_class = confirmations.storage_class;
  if (confirmations.ha_class) out.ha_class = confirmations.ha_class;
  return { name, compose: composeYaml, confirmations: out };
}
