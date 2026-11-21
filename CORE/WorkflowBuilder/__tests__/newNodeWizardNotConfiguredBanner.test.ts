/**
 * 2400LR-g4-wb-wizard — CE describe-search answers not_configured=true (no
 * platform github_token). With a confident I/O description, describe-chat.ts
 * sets hasAskedQA and routes to ready_to_build, so the NOT_CONFIGURED_PROMPT
 * bubble is never shown; the only other mention (admin banner) needs
 * is_platform_admin. The wizard must therefore render its own inline notice
 * naming github_token for every user, and the Describe step must not strand
 * Next once a Dockerfile exists. Mounting the 7500-line wizard is too heavy
 * (see newNodeWizardCreditRateCE.test.tsx), so pin the wiring on the source.
 */
import * as fs from 'fs';
import * as path from 'path';

const SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/new-node-wizard.tsx'),
  'utf-8',
);

describe('new-node-wizard describe not_configured notice (g4)', () => {
  it('tracks the describe-search not_configured answer in state', () => {
    expect(SRC).toContain('const [describeSearchNotConfigured, setDescribeSearchNotConfigured] = useState(false);');
    expect(SRC).toContain('setDescribeSearchNotConfigured(!!data.not_configured);');
  });

  it('renders a notice naming github_token that is not admin-gated', () => {
    const at = SRC.indexOf('{describeSearchNotConfigured && (');
    expect(at).toBeGreaterThan(0);
    const block = SRC.slice(at, at + 400);
    expect(block).toContain('github_token');
    expect(block).not.toContain('isPlatformAdmin');
  });

  it('Describe step allows Next once a Dockerfile has been generated', () => {
    const at = SRC.indexOf('if (sourceType === "describe") {');
    expect(at).toBeGreaterThan(0);
    const block = SRC.slice(at, at + 900);
    expect(block).toContain('return generatedDockerfile.trim().length > 0;');
  });

  // 8c73d4218 (10-31) gated the Workflow Wizard opener off on CE because CE shipped
  // no /support/workflow-wizard/* route. 14af1d11f (11-06, SUPPORTCHAT-CE-4, owner
  // Q-SC1: the whole support package ships in Community Edition) REMOVED that gate
  // and the route now ships on CE, so the un-gated form is the current contract —
  // the older gated pin here was stale. Pinned behaviourally by
  // workflowWizardPanelCeGate.test.tsx.
  it('workflow wizard panel is imported, rendered and opened from the library button', () => {
    const wb = fs.readFileSync(path.resolve(__dirname, '../components/workflow-builder.tsx'), 'utf-8');
    expect(wb).toContain('import { WorkflowWizardPanel } from "./workflow-wizard-panel";');
    expect(wb).toContain('onOpenWorkflowBuilder={() => setIsWizardPanelOpen(true)}');
    expect(wb).toContain('open={isWizardPanelOpen}');
    // Regression guard for the owner rule: no edition carve may survive on the
    // Workflow/Project Wizard openers again (the Stack Wizard row is the separate,
    // deliberate STACK-WIZARD-UNLOCK exception).
    expect(wb).not.toMatch(/isWorkflowWizardCE|isProjectWizardCE/);
    expect(fs.existsSync(path.resolve(__dirname, '../components/workflow-wizard-panel.tsx'))).toBe(true);
  });
});
