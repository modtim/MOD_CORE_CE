// coldLinkRenderGate.test.ts
// Verifies that the accessDeniedDialog is reachable in every early-return
// tree (Q9=B). Reads the source file with fs; no DB, no mocks, no mount.
// Pattern: read the source file and assert on string indices — see bug-b61-no-jsx-text-comment.test.ts.

import * as fs from 'fs';
import * as path from 'path';

const SRC = path.resolve(__dirname, '../components/workflow-builder.tsx');

const src = fs.readFileSync(SRC, 'utf-8');

describe('Q9=B accessDeniedDialog is reachable in all early-return gates', () => {
  it('accessDeniedDialog const is defined BEFORE the first early-return gate', () => {
    const dialogIdx = src.indexOf('const accessDeniedDialog');
    const firstGateIdx = src.indexOf('if (harborStatus.loading)');
    expect(dialogIdx).toBeGreaterThan(0);
    expect(firstGateIdx).toBeGreaterThan(0);
    expect(dialogIdx).toBeLessThan(firstGateIdx);
  });

  it('all four early-return trees contain {accessDeniedDialog}', () => {
    const gates = [
      'if (harborStatus.loading)',
      'if (!harborStatus.configured && !harborStatus.ceEdition)',
      'if (workerStatus.loading || (!workerStatus.checked && harborStatus.configured))',
      'if (workerStatus.checked && !workerStatus.hasWorkers && workerStatus.onlineCount !== -1)',
    ];
    for (const gate of gates) {
      const gateIdx = src.indexOf(gate);
      expect(gateIdx).toBeGreaterThan(0);
      // Brace-match the tree's OWN block: every gate string in `gates` ends at
      // the condition's closing ')', so the block starts at the next '{'. Seeding
      // depth on the '(' of `if (` (the old scan) never closed that paren - the
      // loop only counts braces - so the scan ran past the tree's own '}' and
      // matched a later usage from another tree (mutant survived). Start at the
      // '{' after the condition with depth 0 instead.
      const condEnd = gateIdx + gate.length;
      let depth = 0;
      let endIdx = condEnd;
      for (let i = condEnd; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (depth === 0) { endIdx = i; break; } }
      }
      const blockText = src.slice(gateIdx, endIdx + 1);
      expect(blockText).toContain('{accessDeniedDialog}');
    }
  });

  it('exactly ONE open={api.accessDenied !== null} in the file, and the main return contains {accessDeniedDialog}', () => {
    const allMatches = [];
    let idx = 0;
    const pattern = 'open={api.accessDenied !== null}';
    while (idx !== -1) {
      idx = src.indexOf(pattern, idx);
      if (idx !== -1) {
        allMatches.push(idx);
        idx += pattern.length;
      }
    }
    // The const definition contains the pattern, so exactly one match is expected
    expect(allMatches.length).toBe(1);
    // The main return block (after the last early-return gate) must contain {accessDeniedDialog}
    // Bound the region to the MAIN-return area only: brace-match the LAST gate's
    // OWN block (same condEnd scan the four-trees it() uses) and slice AFTER it,
    // so tree 4's own usage (product :1770) can no longer satisfy the assert when
    // the main-return usage (product :2729) is deleted (mutant M8).
    const lastGate = 'if (workerStatus.checked && !workerStatus.hasWorkers && workerStatus.onlineCount !== -1)';
    const lastGateIdx = src.lastIndexOf(lastGate);
    let lastDepth = 0;
    let lastEndIdx = lastGateIdx + lastGate.length;
    for (let i = lastGateIdx + lastGate.length; i < src.length; i++) {
      if (src[i] === '{') lastDepth++;
      else if (src[i] === '}') { lastDepth--; if (lastDepth === 0) { lastEndIdx = i; break; } }
    }
    const mainReturnBlock = src.slice(lastEndIdx + 1);
    expect(mainReturnBlock).toContain('{accessDeniedDialog}');
  });

  it('the four gate conditions remain verbatim (gates not weakened)', () => {
    const gates = [
      'if (harborStatus.loading)',
      'if (!harborStatus.configured && !harborStatus.ceEdition)',
      'if (workerStatus.loading || (!workerStatus.checked && harborStatus.configured))',
      'if (workerStatus.checked && !workerStatus.hasWorkers && workerStatus.onlineCount !== -1)',
    ];
    for (const gate of gates) {
      expect(src).toContain(gate);
    }
  });

  it('the checkHarborStatus if (!api.accessToken) bail still precedes the /me fetch (auth gates intact)', () => {
    // Scope to checkHarborStatus: the bail is multi-line in the source, and
    // '/me' appears in comments earlier in the file, so anchor both after the
    // useCallback definition.
    const fnIdx = src.indexOf('const checkHarborStatus = useCallback');
    expect(fnIdx).toBeGreaterThan(0);
    const bailIdx = src.indexOf('if (!api.accessToken)', fnIdx);
    const meFetchIdx = src.indexOf('/me', fnIdx);
    expect(bailIdx).toBeGreaterThan(0);
    expect(meFetchIdx).toBeGreaterThan(0);
    expect(bailIdx).toBeLessThan(meFetchIdx);
  });

  it('the dialog const contains the required tokens and no workflow_name', () => {
    // The const sits between its declaration and the first early-return gate;
    // slice that region (paren-matching is unreliable: the Q41 comment inside
    // the dialog has an unbalanced '(').
    const constIdx = src.indexOf('const accessDeniedDialog');
    expect(constIdx).toBeGreaterThan(0);
    const gateIdx = src.indexOf('if (harborStatus.loading)');
    expect(gateIdx).toBeGreaterThan(constIdx);
    const dialogBlock = src.slice(constIdx, gateIdx);
    expect(dialogBlock).toContain('api.accessDenied?.workflowName');
    expect(dialogBlock).toContain('api.accessToken == null');
    expect(dialogBlock).toContain('Sign in again');
    expect(dialogBlock).toContain('Request access');
    expect(dialogBlock).not.toContain('workflow_name');
  });
});
