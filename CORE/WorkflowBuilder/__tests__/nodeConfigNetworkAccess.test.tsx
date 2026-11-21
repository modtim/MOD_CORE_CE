/**
 * FX.91 p2b [U] — the node config panel's "Network access" section.
 *
 * The panel hydrates `egressInfo` from the SAME live-def GET it already makes
 * (GET /nodes/{defId} carries p2a `egress_domains` + the p2c `egress_approval`
 * enrichment), so these tests drive the real NodeConfigPanel with that
 * response stubbed and pin: the domain list + approval wording, the read-only
 * view for a non tenant admin, and the two-step Approve (arm -> confirm) that
 * hits the existing p2c POST /nodes/{id}/egress-approval with the EXACT
 * declared set.
 */
import React from "react";
import { render, screen, act } from "@testing-library/react";

beforeAll(() => {
  (global as any).ResizeObserver = class {
    observe() {} unobserve() {} disconnect() {}
  };
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

import NodeConfigPanel from "@/components/node-config-panel";

const ORIG_FETCH = (global as any).fetch;
afterEach(() => { (global as any).fetch = ORIG_FETCH; posts.length = 0; });

let defBody: any = {};
let meBody: any = {};
const posts: any[] = [];

function stubFetch(url: any, options: any) {
  const u = url as string;
  if (u.includes("/egress-approval")) {
    posts.push({ url: u, options });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ approved: true }) });
  }
  if (u.includes("/me")) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(meBody) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(defBody) });
}

function openPanel() {
  (global as any).fetch = stubFetch;
  render(
    <NodeConfigPanel
      node={{
        id: "n1",
        type: "process",
        position: { x: 0, y: 0 },
        data: {
          label: "Cog", node_kind: "cog", nodeDefinitionId: 77,
          input_schema: { input_type: "image" },
          output_schema: { output_type: "image" },
        },
      } as any}
      updateNodeData={() => {}}
      onClose={() => {}}
      accessToken="tok"
    />,
  );
}

describe('FX.91 p2b node config panel — "Network access"', () => {
  it("lists the declared domains and the pending approval state", async () => {
    defBody = {
      id: 77,
      egress_domains: ["huggingface.co", "cdn.example.com"],
      egress_approval: { state: "required", declared: ["huggingface.co", "cdn.example.com"], approved: [] },
    };
    meBody = { parent_user_id: null };
    openPanel();
    expect(await screen.findByText("Network access", { timeout: 20000 })).toBeTruthy();
    expect(await screen.findByText("huggingface.co", { timeout: 20000 })).toBeTruthy();
    expect(await screen.findByText("cdn.example.com", { timeout: 20000 })).toBeTruthy();
    expect(
      await screen.findByText(/not approved yet — outbound use is not granted/, { timeout: 20000 }),
    ).toBeTruthy();
  });

  it("a default-deny node says so and lists nothing", async () => {
    defBody = { id: 77, egress_domains: [], egress_approval: { state: "not_required", declared: [] } };
    meBody = { parent_user_id: null };
    openPanel();
    expect(await screen.findByText("Network access", { timeout: 20000 })).toBeTruthy();
    expect(screen.getByText(/zero-outbound network/)).toBeTruthy();
    expect(screen.queryByText("huggingface.co")).not.toBeTruthy();
  });

  it("a tenant admin gets the Approve button, and the first click only arms it", async () => {
    defBody = {
      id: 77,
      egress_domains: ["huggingface.co"],
      egress_approval: { state: "required", declared: ["huggingface.co"], approved: [] },
    };
    meBody = { parent_user_id: null };
    openPanel();
    const arm = await screen.findByText("Approve network access", { timeout: 20000 });
    expect(arm).toBeTruthy();
    await act(async () => { arm.click(); });
    const confirm = await screen.findByText("Confirm approve network access", { timeout: 20000 });
    expect(confirm).toBeTruthy();
    // Arming must not decide anything yet — the POST is the confirm click's.
    expect(posts).toHaveLength(0);
  });

  it("the confirm click POSTs the exact declared set to the p2c endpoint", async () => {
    defBody = {
      id: 77,
      egress_domains: ["huggingface.co"],
      egress_approval: { state: "required", declared: ["huggingface.co"], approved: [] },
    };
    meBody = { parent_user_id: null };
    openPanel();
    const arm = await screen.findByText("Approve network access", { timeout: 20000 });
    await act(async () => { arm.click(); });
    const confirm = await screen.findByText("Confirm approve network access", { timeout: 20000 });
    await act(async () => { confirm.click(); });
    await screen.findByText(/Approved 1 domain\(s\) for this tenant/, { timeout: 20000 });
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toContain("/nodes/77/egress-approval");
    expect(JSON.parse(posts[0].options.body)).toEqual({ domains: ["huggingface.co"] });
  });

  it("a non tenant admin sees the section read-only (no Approve button)", async () => {
    defBody = {
      id: 77,
      egress_domains: ["huggingface.co"],
      egress_approval: { state: "required", declared: ["huggingface.co"], approved: [] },
    };
    meBody = { parent_user_id: 5 };
    openPanel();
    expect(await screen.findByText("Network access", { timeout: 20000 })).toBeTruthy();
    expect(screen.queryByText(/Approve network access/)).not.toBeTruthy();
  });
});
