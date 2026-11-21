/**
 * FIX-CE-SHOW-PROJECT-CHAT-HISTORY (11-10) — the project chat history rail is
 * NOT edition-gated.
 *
 * Owner decision (Tim, explicit 11-10): show the project chat history rail in
 * Community Edition. The rail's API side already shipped in CE (support_chat
 * routers are deny_exceptions'd in — open-core/manifest.yaml :2475-:2488), so
 * the 2400UI123 `ceRailRef` carve in the two panels is deleted outright: the
 * sessions list + transcripts render on every edition, and the paid code path
 * is unchanged in behaviour (on paid `ceRail` was always false).
 *
 * Mounting the panels is too heavy (same reason as
 * workflowWizardPanelCeGate.test.tsx), so — like that suite — this pins the
 * SOURCE of both panels: no CE notice string, no edition carve identifier, and
 * the rail fetch/render are ungated.
 */

import * as fs from "fs";
import * as path from "path";

const WIZARD_SRC = fs.readFileSync(
  path.resolve(__dirname, "../components/project-wizard-panel.tsx"),
  "utf-8",
);
const CONVO_SRC = fs.readFileSync(
  path.resolve(__dirname, "../components/project-conversation-panel.tsx"),
  "utf-8",
);

const NOTICE = "Project chat history is not part of Community Edition";

describe("project-wizard-panel.tsx / project-conversation-panel.tsx — CE rail carve removed", () => {
  it("neither panel carries the CE hidden-state notice", () => {
    expect([WIZARD_SRC.includes(NOTICE), CONVO_SRC.includes(NOTICE)]).toEqual([false, false]);
  });

  it("no edition-gate identifier survives in either panel", () => {
    const re = /ceRailRef|setCeRail|\bceRail\b|ceEdition/;
    expect([re.test(WIZARD_SRC), re.test(CONVO_SRC)]).toEqual([false, false]);
  });

  it("the rail fetch is ungated on both panels", () => {
    for (const [name, src] of [["wizard", WIZARD_SRC], ["convo", CONVO_SRC]]) {
      const at = src.indexOf('authFetch("/support/sessions?kind=project&limit=50")');
      expect(at).toBeGreaterThan(-1);
      // the line right before the fetch must be the entitlements wait, not a skip
      const block = src.slice(Math.max(0, at - 60), at);
      expect(block).toContain("railGateRef.current!.wait");
      expect([`${name}: no guard before the rail fetch`, /return;/.test(block)]).toEqual([
        `${name}: no guard before the rail fetch`,
        false,
      ]);
    }
  });

  it("openSession is ungated on both panels", () => {
    for (const [name, src] of [["wizard", WIZARD_SRC], ["convo", CONVO_SRC]]) {
      const at = src.indexOf("const openSession = async (id: string) => {");
      expect(at).toBeGreaterThan(-1);
      const block = src.slice(at, at + 120);
      expect([`${name}: openSession opens straight into the fetch`, /return;/.test(block)]).toEqual([
        `${name}: openSession opens straight into the fetch`,
        false,
      ]);
    }
  });

  it("the sessions list renders unconditionally on both panels", () => {
    for (const [name, src] of [["wizard", WIZARD_SRC], ["convo", CONVO_SRC]]) {
      expect(src).toMatch(/\{sessions\.map\(s => \(/);
      expect(src).toMatch(/\{sessions\.length === 0 && \(/);
    }
  });

  it("the fail-open entitlements read + rail gate stay (paid behaviour unchanged)", () => {
    for (const [name, src] of [["wizard", WIZARD_SRC], ["convo", CONVO_SRC]]) {
      expect(src).toContain('import { useEntitlements } from "@/hooks/use-entitlements";');
      expect(src).toContain("const { edition, loading } = useEntitlements();");
      expect(src).toMatch(/railGateRef/);
      expect(src).toMatch(/\[edition, loading\]/);
    }
  });
});
