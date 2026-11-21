/**
 * .
 *
 * Owner Tim: "the button should go triggered until the thing is done
 * loading. That way it looks clicked". The control the user just clicked
 * shows a pressed/busy state (class mod-busy, aria-busy, spinner) until the
 * requests it started through authenticatedFetch finish.
 *
 * RED before the fix: lib/busy-indicator.ts does not exist, so the import
 * below fails with "Cannot find module '@/lib/busy-indicator'".
 *
 * Fake timers (modern: Date is faked too) + a mocked global fetch whose
 * responses the test resolves by hand. Nothing real is fetched.
 */

import { authenticatedFetch } from "@/lib/api-client";
import {
  BUSY_SAFETY_MS,
  CLICK_ATTACH_WINDOW_MS,
  installBusyIndicator,
  uninstallBusyIndicator,
} from "@/lib/busy-indicator";

type Deferred = { resolve: (r: Response) => void; reject: (e: Error) => void };

let pending: Deferred[] = [];
let btn: HTMLButtonElement;

function ok(status = 200): Response {
  return { status, ok: status >= 200 && status < 300 } as unknown as Response;
}

beforeEach(() => {
  jest.useFakeTimers();
  uninstallBusyIndicator();
  installBusyIndicator();
  document.body.innerHTML = "";
  btn = document.createElement("button");
  btn.textContent = "Run";
  document.body.appendChild(btn);
  pending = [];
  (global as unknown as { fetch: unknown }).fetch = jest.fn(
    () =>
      new Promise<Response>((resolve, reject) => {
        pending.push({ resolve, reject });
      }),
  );
});

afterEach(() => {
  uninstallBusyIndicator();
  jest.useRealTimers();
});

const isBusy = () =>
  btn.classList.contains("mod-busy") &&
  btn.getAttribute("aria-busy") === "true" &&
  !!btn.querySelector(".mod-busy-spinner");

describe("clicked control busy state (workflow builder)", () => {
  it("marks the clicked control busy while its request runs, clears on finish", async () => {
    btn.click();
    jest.advanceTimersByTime(20);
    const p = authenticatedFetch("http://api/items");
    expect(isBusy()).toBe(true);
    pending[0].resolve(ok());
    await p;
    expect(isBusy()).toBe(false);
    expect(btn.hasAttribute("aria-busy")).toBe(false);
    expect(btn.querySelector(".mod-busy-spinner")).toBeNull();
  });

  it("clears on failure (network error thrown)", async () => {
    btn.click();
    const p = authenticatedFetch("http://api/items", { hardLogoutOnFailure: false });
    expect(isBusy()).toBe(true);
    pending[0].reject(new Error("boom"));
    await expect(p).rejects.toThrow("boom");
    expect(isBusy()).toBe(false);
  });

  it("marks nothing when the request starts later than the attach window", async () => {
    btn.click();
    jest.advanceTimersByTime(CLICK_ATTACH_WINDOW_MS + 50);
    const p = authenticatedFetch("http://api/items");
    expect(isBusy()).toBe(false);
    pending[0].resolve(ok());
    await p;
  });

  it("marks nothing when there was no preceding click", async () => {
    const p = authenticatedFetch("http://api/items");
    expect(document.querySelector(".mod-busy")).toBeNull();
    pending[0].resolve(ok());
    await p;
  });

  it("swallows a second click on the busy control", async () => {
    const seen = jest.fn();
    btn.addEventListener("click", seen);
    btn.click();
    expect(seen).toHaveBeenCalledTimes(1);
    const p = authenticatedFetch("http://api/items");
    btn.click();
    expect(seen).toHaveBeenCalledTimes(1); // swallowed in capture phase
    pending[0].resolve(ok());
    await p;
    btn.click();
    expect(seen).toHaveBeenCalledTimes(2); // free again
  });

  it("never marks stream / notification / poll paths", async () => {
    for (const path of [
      "notifications/unread-count",
      "notifications/list",
      "stream-tickets",
      "support/workflow-wizard/stream",
    ]) {
      btn.click();
      const p = authenticatedFetch(`http://api/${path}`);
      expect(document.querySelector(".mod-busy")).toBeNull();
      pending[pending.length - 1].resolve(ok());
      await p;
    }
  });

  it("overlapping requests keep it busy until both finish", async () => {
    btn.click();
    const p1 = authenticatedFetch("http://api/a");
    const p2 = authenticatedFetch("http://api/b");
    pending[0].resolve(ok());
    await p1;
    expect(isBusy()).toBe(true);
    pending[1].resolve(ok());
    await p2;
    expect(isBusy()).toBe(false);
  });

  it("releases at the 60 s safety limit even if the request never ends", async () => {
    btn.click();
    void authenticatedFetch("http://api/slow");
    expect(isBusy()).toBe(true);
    jest.advanceTimersByTime(BUSY_SAFETY_MS - 1000);
    expect(isBusy()).toBe(true);
    jest.advanceTimersByTime(2000);
    expect(isBusy()).toBe(false);
    expect(btn.querySelector(".mod-busy-spinner")).toBeNull();
  });

  it("injects its own CSS with a reduced-motion rule", () => {
    btn.click();
    void authenticatedFetch("http://api/x");
    const css = document.head.querySelector("style#mod-busy-css")?.textContent || "";
    expect(css).toContain("prefers-reduced-motion");
  });
});
