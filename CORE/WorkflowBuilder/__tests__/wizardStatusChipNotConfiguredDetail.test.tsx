/**
 * FIX-WB-WIZARD-STATUS-CHIP (wizard audit A1/A2) — on a fresh CE nothing is
 * bound to the Node Wizard, and GET /nodes/wizard-llm-status returns
 * loaded=false with detail "Wizard LLM not configured. No model is bound ..."
 * (CORE/API/routers/node_definitions_domains/node_authoring.py:2716-2731).
 * The Source-step chip used to throw that sentence away and show the lie
 * "AI model warming up ... First analysis can take up to 2 minutes."
 *
 * Contract now: loaded=true unchanged (green/ready); loaded=false AND
 * detail non-empty -> show the detail verbatim in the chip (amber, same
 * style); "warming up" ONLY when loaded=false and detail is empty.
 * Shared file (manifest allow root CORE/WorkflowBuilder/**): the paid
 * tenant with nothing bound gets the same honest text (its own shorter
 * "Wizard LLM not configured." detail) — behaviour change, not wording.
 *
 * Mounting the 7500-line wizard is too heavy (see newNodeWizardCreditRateCE
 * .test.tsx), so — like that suite — we pin the chip wiring against the
 * wizard SOURCE and drive a faithful mirror of the ternary chain.
 */
import * as fs from 'fs';
import * as path from 'path';

const SRC = fs.readFileSync(
  path.resolve(__dirname, '../components/new-node-wizard.tsx'),
  'utf-8',
);

const NOT_CONFIGURED_DETAIL =
  'Wizard LLM not configured. No model is bound to the Node Wizard yet. ' +
  'Open Settings > AI Configuration, choose a model for the Node Wizard ' +
  '(a model served by one of your own stacks, or your own OpenAI-compatible ' +
  'endpoint), then save.';

// --- source pins ----------------------------------------------------------
describe('new-node-wizard LLM status chip shows the API detail (A1/A2)', () => {
  it('chip block reads llmStatus.detail and falls back to warming up', () => {
    const at = SRC.indexOf('{!llmStatus\n                  ? <>Checking AI analyzer status');
    expect(at).toBeGreaterThan(0);
    const block = SRC.slice(at, at + 700);
    expect(block).toContain(': llmStatus.detail');
    expect(block).toContain('? <>{llmStatus.detail}</>');
    expect(block).toContain(': <>AI model warming up');
    // the old unconditional lie (ready-copy line immediately followed by the
    // warming-up line, detail never consulted) must be gone
    expect(block).not.toContain('Paste your script and click Next.</>\n                    : <>AI model warming up');
  });

  it('detail branch keeps the amber chip style (no new colors)', () => {
    const at = SRC.indexOf('bg-yellow-50 border-yellow-200 text-yellow-900');
    expect(at).toBeGreaterThan(0);
    expect(SRC).toContain('llmStatus.loaded ? "bg-green-500" : "bg-yellow-500 animate-pulse"');
  });
});

// --- behavioural mirror of the chip ternary chain -------------------------
function chipText(llmStatus: { loaded: boolean; model: string; detail: string } | null): string {
  if (!llmStatus) return 'Checking AI analyzer status…';
  if (llmStatus.loaded)
    return `AI analyzer ready${llmStatus.model ? ` — ${llmStatus.model}` : ''}. Paste your script and click Next.`;
  return llmStatus.detail
    ? llmStatus.detail
    : `AI model warming up${llmStatus.model ? ` — ${llmStatus.model}` : ''}. First analysis can take up to 2 minutes.`;
}

function chipColor(llmStatus: { loaded: boolean; model: string; detail: string } | null): string {
  if (!llmStatus) return 'bg-gray-50 border-gray-200 text-gray-700';
  return llmStatus.loaded
    ? 'bg-green-50 border-green-200 text-green-900'
    : 'bg-yellow-50 border-yellow-200 text-yellow-900';
}

describe('chip states: loaded / not-configured detail / empty detail', () => {
  it('state 1 - loaded=true: green chip, ready copy unchanged', () => {
    const s = { loaded: true, model: 'qwen3:14b', detail: 'Model resident in memory.' };
    expect(chipText(s)).toBe('AI analyzer ready — qwen3:14b. Paste your script and click Next.');
    expect(chipColor(s)).toBe('bg-green-50 border-green-200 text-green-900');
  });

  it('state 2 - loaded=false + not-configured detail: amber chip shows the detail', () => {
    const s = { loaded: false, model: '', detail: NOT_CONFIGURED_DETAIL };
    expect(chipText(s)).toBe(NOT_CONFIGURED_DETAIL);
    expect(chipText(s)).not.toMatch(/warming up/i);
    expect(chipColor(s)).toBe('bg-yellow-50 border-yellow-200 text-yellow-900');
  });

  it('state 3 - loaded=false + empty detail: warming up kept, amber', () => {
    const s = { loaded: false, model: 'qwen3:14b', detail: '' };
    expect(chipText(s)).toBe('AI model warming up — qwen3:14b. First analysis can take up to 2 minutes.');
    expect(chipColor(s)).toBe('bg-yellow-50 border-yellow-200 text-yellow-900');
  });
});
