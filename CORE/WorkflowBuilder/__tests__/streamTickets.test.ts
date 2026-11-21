/**
 * WS5.34b — SSE stream tickets, WorkflowBuilder client side.
 *
 * Covers:
 *  - `mintStreamTicket` (lib/api-client.ts): POSTs the right body, returns
 *    the raw ticket on success, null on any failure.
 *  - `streamWizardRunLogs` (lib/wizard-queue.ts): mints a ticket BEFORE
 *    opening the EventSource and connects with `?ticket=` (never
 *    `?access_token=`); falls back to polling when minting fails.
 */

import { mintStreamTicket } from "@/lib/api-client";
import { streamWizardRunLogs } from "@/lib/wizard-queue";

function makeResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response;
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  listeners: Record<string, Array<(evt: unknown) => void>> = {};
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: (evt: unknown) => void): void {
    (this.listeners[type] ||= []).push(cb);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, evt: unknown = {}): void {
    (this.listeners[type] || []).forEach((cb) => cb(evt));
  }
}

describe("mintStreamTicket", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
    localStorage.clear();
  });

  it("POSTs the scope body and returns the raw ticket", async () => {
    fetchMock.mockResolvedValue(
      makeResponse(200, { ticket: "abc123", expires_in: 60 }),
    );
    const ticket = await mintStreamTicket("http://api.test", "notifications");
    expect(ticket).toBe("abc123");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/stream-tickets");
    expect(JSON.parse(init.body)).toEqual({ scope: "notifications" });
  });

  it("includes run_id for the wizard-run scope", async () => {
    fetchMock.mockResolvedValue(makeResponse(200, { ticket: "xyz", expires_in: 60 }));
    await mintStreamTicket("http://api.test", "wizard-run", "run-9");
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ scope: "wizard-run", run_id: "run-9" });
  });

  it("returns null on a non-2xx response", async () => {
    fetchMock.mockResolvedValue(makeResponse(403, { detail: "invalid ticket" }));
    const ticket = await mintStreamTicket("http://api.test", "notifications");
    expect(ticket).toBeNull();
  });

  it("returns null on a network error", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    const ticket = await mintStreamTicket("http://api.test", "notifications");
    expect(ticket).toBeNull();
  });
});

describe("streamWizardRunLogs ticket-then-connect", () => {
  let fetchMock: jest.Mock;
  const realEventSource = (global as unknown as { EventSource?: unknown }).EventSource;

  beforeEach(() => {
    FakeEventSource.instances = [];
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
    (global as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
    localStorage.clear();
    jest.useFakeTimers();
  });

  afterEach(() => {
    (global as unknown as { EventSource: unknown }).EventSource = realEventSource;
    jest.useRealTimers();
  });

  it("mints a ticket and connects with ?ticket= (never ?access_token=)", async () => {
    fetchMock.mockResolvedValue(
      makeResponse(200, { ticket: "the-ticket", expires_in: 60 }),
    );
    const close = streamWizardRunLogs("run-1", jest.fn(), "http://api.test");
    // Let the mint promise resolve before asserting.
    await act();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/stream-tickets",
      expect.objectContaining({ method: "POST" }),
    );
    expect(FakeEventSource.instances).toHaveLength(1);
    const es = FakeEventSource.instances[0];
    expect(es.url).toContain("ticket=the-ticket");
    expect(es.url).not.toContain("access_token=");

    close();
  });

  it("falls back to polling when the mint fails (no EventSource opened)", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes("/stream-tickets")) {
        // 403, not 401 — a 401 would send authenticatedFetch down its
        // silent-token-refresh path, which isn't what this test covers.
        return Promise.resolve(makeResponse(403, { detail: "forbidden" }));
      }
      return Promise.resolve(makeResponse(200, { run_id: "run-1", lines: [], last_seq: -1 }));
    });
    const close = streamWizardRunLogs("run-1", jest.fn(), "http://api.test");
    await act();

    expect(FakeEventSource.instances).toHaveLength(0);
    // The poll fallback hits the persisted-logs endpoint instead.
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/wizard-queue/run-1/logs"),
      expect.anything(),
    );

    close();
  });
});

// Flush pending microtasks (the async mint, and any 0ms poll-fallback
// timer it schedules) under fake timers.
async function act(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await jest.advanceTimersByTimeAsync(0);
}
