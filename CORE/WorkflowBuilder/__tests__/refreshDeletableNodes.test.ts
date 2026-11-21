/**
 * Refresh All Nodes must never try to delete system (tenant_id null) rows.
 */
import type { NodeDefinition } from "@/lib/types";

jest.mock("@/components/ui/use-toast", () => ({ toast: jest.fn() }));

import { selectRefreshDeletableNodes } from "@/hooks/useWorkflowApi";

const mk = (id: number, tenant_id?: number | null): NodeDefinition =>
  ({ id, name: `n${id}`, tenant_id } as unknown as NodeDefinition);

describe("selectRefreshDeletableNodes", () => {
  it("drops rows with tenant_id null", () => {
    expect(selectRefreshDeletableNodes([mk(1, null)])).toEqual([]);
  });

  it("drops rows with tenant_id undefined", () => {
    expect(selectRefreshDeletableNodes([mk(1, undefined)])).toEqual([]);
  });

  it("keeps rows with tenant_id 1", () => {
    const r = selectRefreshDeletableNodes([mk(1, 1)]);
    expect(r.map((n) => n.id)).toEqual([1]);
  });

  it("keeps every numeric tenant_id and drops the system rows mixed in", () => {
    const input = [mk(1, 1), mk(2, null), mk(3, 7), mk(4, undefined), mk(5, 0)];
    expect(selectRefreshDeletableNodes(input).map((n) => n.id)).toEqual([1, 3, 5]);
  });

  it("returns a new array and does not mutate the input", () => {
    const input = [mk(1, 1), mk(2, null)];
    const snapshot = [...input];
    const out = selectRefreshDeletableNodes(input);
    expect(out).not.toBe(input);
    expect(input).toEqual(snapshot);
    expect(input).toHaveLength(2);
  });

  it("handles an empty list", () => {
    expect(selectRefreshDeletableNodes([])).toEqual([]);
  });
});
