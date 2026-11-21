/**
 * A palette node is "local" only when its id is one of the client-side
 * sentinel definitions (If/Else, Switch, Split, Human Approval ...).
 * Real server NodeDefinition rows can have any positive id (ids above 900
 * exist), so a numeric threshold must never be used for this test.
 */
export function isLocalNodeId(
  id: number | null | undefined,
  localNodes: ReadonlyArray<{ id?: number | null }>,
): boolean {
  if (id === null || id === undefined) return false;
  return localNodes.some((n) => n.id === id);
}
