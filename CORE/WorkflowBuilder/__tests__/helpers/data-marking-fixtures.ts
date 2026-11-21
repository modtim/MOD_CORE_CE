/**
 * Shared fixtures for the data-marking banner suites
 * (data-marking-banner.test.tsx, fx163-marking-clear-visible.test.tsx).
 *
 * Lives in a NON-test module (jest testMatch only collects `*.test.ts(x)`)
 * so importing it does not re-register another suite's describe/it blocks.
 */

export function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

export function markingsResponse(items: Array<{ label: string; dissemination?: string | null }>): unknown {
  return { items, total: items.length };
}
