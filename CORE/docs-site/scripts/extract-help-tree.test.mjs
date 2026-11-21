#!/usr/bin/env node
/**
 * Unit tests for extract-help-tree.mjs.
 *
 * Uses Node's built-in test runner (Node 20+) — no extra deps.
 * Build fixture trees in a temp dir per test, call buildHelpTree
 * against them, assert on the returned tree shape.
 *
 * Cases:
 *   1. Full front-matter → all optional fields present in the leaf
 *   2. Missing optional fields → leaf has only required `id`,
 *      `label`, `docs_slug`
 *   3. Missing `title` but a `# H1` present → label falls back to
 *      the heading; missing both → buildHelpTree throws with the
 *      offending file path in the error message
 *   4. sidebar_position respected over alphabetical
 *   5. Subdirectory becomes a category node with min-child-position
 *      as its sort key
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildHelpTree } from './extract-help-tree.mjs';

function makeFixture(files) {
    const dir = mkdtempSync(join(tmpdir(), 'help-tree-test-'));
    for (const [relPath, content] of Object.entries(files)) {
        const abs = join(dir, relPath);
        mkdirSync(join(abs, '..'), { recursive: true });
        writeFileSync(abs, content, 'utf-8');
    }
    return dir;
}

test('full front-matter — all optional fields populated', () => {
    const dir = makeFixture({
        'workflow-builder.md': `---
title: Workflow Builder
description: Visual editor
sidebar_position: 2
dashboard_route: '#workflows'
wb_route: '/'
---
# body`,
    });
    try {
        const out = buildHelpTree(dir);
        assert.equal(out.version, 1);
        assert.equal(out.root.label, 'Help');
        assert.equal(out.root.children.length, 1);
        const leaf = out.root.children[0];
        assert.equal(leaf.id, 'workflow-builder');
        assert.equal(leaf.label, 'Workflow Builder');
        assert.equal(leaf.docs_slug, '/workflow-builder');
        assert.equal(leaf.description, 'Visual editor');
        assert.equal(leaf.dashboard_route, '#workflows');
        assert.equal(leaf.wb_route, '/');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('missing optional fields — only required fields present', () => {
    const dir = makeFixture({
        'sparse.md': `---
title: Sparse Page
---
# body`,
    });
    try {
        const leaf = buildHelpTree(dir).root.children[0];
        assert.equal(leaf.id, 'sparse');
        assert.equal(leaf.label, 'Sparse Page');
        assert.equal(leaf.docs_slug, '/sparse');
        assert.equal(leaf.description, undefined);
        assert.equal(leaf.dashboard_route, undefined);
        assert.equal(leaf.wb_route, undefined);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('missing title but # H1 present — label falls back to the heading', () => {
    const dir = makeFixture({
        'fallback.md': `# Fallback Heading

Body text.`,
    });
    try {
        const leaf = buildHelpTree(dir).root.children[0];
        assert.equal(leaf.id, 'fallback');
        assert.equal(leaf.label, 'Fallback Heading');
        assert.equal(leaf.docs_slug, '/fallback');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('missing title and no H1 — throws with file path in message', () => {
    const dir = makeFixture({
        'broken.md': `---
description: I forgot the title
---
Just body text, no heading.`,
    });
    try {
        assert.throws(
            () => buildHelpTree(dir),
            /no title for docs\/broken\.md/,
        );
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('sidebar_position respected over alphabetical', () => {
    const dir = makeFixture({
        'zzz-last.md': `---
title: Z
sidebar_position: 1
---`,
        'aaa-first.md': `---
title: A
sidebar_position: 2
---`,
    });
    try {
        const ids = buildHelpTree(dir).root.children.map(c => c.id);
        // sidebar_position 1 wins over alphabetical → Z first.
        assert.deepEqual(ids, ['zzz-last', 'aaa-first']);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('h2/h3 headings synthesize sub-nodes with anchored docs links', () => {
    const dir = makeFixture({
        'page.md': `---
title: Page
sidebar_position: 1
---

# Page

## Section One

Body text.

### Subsection 1A

More text.

### Subsection 1B

\`\`\`
## This is in a code fence — must be ignored
\`\`\`

## Section Two

Other text.
`,
    });
    try {
        const out = buildHelpTree(dir);
        const page = out.root.children[0];
        // Page itself stays a node with docs_slug.
        assert.equal(page.id, 'page');
        assert.equal(page.docs_slug, '/page');
        // Two h2 children synthesized.
        assert.equal(page.children?.length, 2);
        const s1 = page.children[0];
        assert.equal(s1.label, 'Section One');
        assert.equal(s1.docs_slug, '/page#section-one');
        assert.equal(s1.id, 'page#section-one');
        // h3s nested under their h2.
        assert.equal(s1.children?.length, 2);
        assert.equal(s1.children[0].label, 'Subsection 1A');
        assert.equal(s1.children[0].docs_slug, '/page#subsection-1a');
        // Code-fence content didn't leak in as an h2.
        const s2 = page.children[1];
        assert.equal(s2.label, 'Section Two');
        // Empty children stripped — Section Two has no h3s.
        assert.equal(s2.children, undefined);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('subdirectory becomes a category node with min-child sort key', () => {
    const dir = makeFixture({
        'top.md': `---
title: Top
sidebar_position: 5
---`,
        'legal/privacy.md': `---
title: Privacy
sidebar_position: 8
---`,
        'legal/terms.md': `---
title: Terms
sidebar_position: 9
---`,
    });
    try {
        const out = buildHelpTree(dir);
        const ids = out.root.children.map(c => c.id);
        // top (5) before legal (min child = 8).
        assert.deepEqual(ids, ['top', 'legal']);

        const legal = out.root.children.find(c => c.id === 'legal');
        assert.equal(legal.label, 'Legal');
        assert.equal(legal.children.length, 2);
        const legalIds = legal.children.map(c => c.id);
        // privacy (8) before terms (9).
        assert.deepEqual(legalIds, ['legal/privacy', 'legal/terms']);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});
