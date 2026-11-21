/**
 * Platform-node classification — single source of truth (client side).
 *
 * Mirror of CORE/API/utils/platform_node.py. A node is a "platform
 * node" (system-shipped, vetted at build time, exempt from runtime
 * scan gating, rendered under "Platform nodes" in the library) when:
 *
 *   - its tenant_id is null/undefined (existing convention used by
 *     seed_converters.py — system rows have tenant_id IS NULL), OR
 *   - as a defensive fallback, its name starts with "system." (catches
 *     legacy or mis-seeded rows whose tenant_id was set in error)
 *
 * Keep this in sync with the Python helper. Drift between client and
 * server will produce surprising "this node is in My Nodes but the
 * orchestrator treats it as platform" anomalies.
 */
export function isPlatformNode(
  node: { tenant_id?: number | null; name?: string | null } | null | undefined,
): boolean {
  if (!node) return false;
  if (node.tenant_id == null) return true;
  return (node.name ?? "").startsWith("system.");
}
