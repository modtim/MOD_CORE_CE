#!/usr/bin/env node
/**
 * Build-time extractor that walks `docs/` and emits the help-tree
 * JSON the Dashboard's Mind Map tab fetches from
 * `${DOCS_URL}/help-tree.json`.
 *
 * Why .mjs and not .ts: the docs-site doesn't have tsx / ts-node
 * installed and adding one for a single ~100-line script isn't
 * worth the dependency churn. Node 20 (the Dockerfile base) runs
 * ES modules natively. gray-matter is already a transitive
 * Docusaurus dep so no install step.
 *
 * Schema is documented in
 *   openspec/changes/help-mind-map/specs/help-mind-map/spec.md
 * under "help-tree.json schema and source".
 *
 * Required front-matter per page: `title`. Build fails (exit 1)
 * if any .md is missing it. Optional: `description`,
 * `dashboard_route`, `wb_route`, `sidebar_position`, `slug`.
 *
 * Output: writes JSON to stdout (the caller redirects to
 * static/help-tree.json) and a one-line summary to stderr.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, basename, dirname, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import matter from 'gray-matter';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const DEFAULT_DOCS_ROOT = join(__dirname, '..', 'docs');

/**
 * Convert a docs-relative path to its Docusaurus URL slug.
 * Mirrors the routing rules used by `@docusaurus/preset-classic`
 * with the default `routeBasePath: '/'`:
 *   docs/getting-started.md → /getting-started
 *   docs/legal/privacy-policy.md → /legal/privacy-policy
 * When a page sets explicit `slug:` front-matter we use that
 * value verbatim instead.
 */
function slugFromPath(relPath, explicitSlug) {
    if (explicitSlug) return explicitSlug;
    const noExt = relPath.replace(/\.mdx?$/, '');
    return '/' + noExt.replace(/\\/g, '/');
}

/**
 * id = relative path with extension/dirs collapsed to a stable
 * slug-like string. For tree comparison + Cytoscape node IDs.
 *   docs/getting-started.md → 'getting-started'
 *   docs/legal/privacy-policy.md → 'legal/privacy-policy'
 */
function idFromPath(relPath) {
    return relPath.replace(/\.mdx?$/, '').replace(/\\/g, '/');
}

/**
 * Walk a directory and return { files: [...], subdirs: [...] }
 * sorted by name. Hidden files (dot-prefixed) are ignored.
 */
function readDir(dirAbs) {
    const entries = readdirSync(dirAbs)
        .filter(name => !name.startsWith('.'))
        .map(name => ({ name, abs: join(dirAbs, name), stat: statSync(join(dirAbs, name)) }));
    const files = entries.filter(e => e.stat.isFile() && /\.mdx?$/.test(e.name));
    const subdirs = entries.filter(e => e.stat.isDirectory());
    return { files, subdirs };
}

/**
 * Slugify a heading the way Docusaurus does (lowercase, hyphenate
 * spaces, strip most punctuation). Matches the auto-generated
 * `id` Docusaurus puts on every h2/h3 so the resulting
 * `/page#anchor` URLs jump to the right section.
 */
function headingSlug(text) {
    return text
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s-]/gu, '') // strip punctuation
        .trim()
        .replace(/\s+/g, '-');
}

/**
 * Parse h2/h3 lines from a markdown body and return a nested
 * tree. Each h2 becomes a child node; h3s nest under the
 * preceding h2. Code fences (``` …```) are skipped so a `## `
 * inside a code block doesn't accidentally become a heading.
 */
function extractHeadings(body, parentSlug, parentId) {
    const lines = body.split('\n');
    const h2s = [];
    let inFence = false;
    let currentH2 = null;
    for (const line of lines) {
        if (/^```/.test(line)) {
            inFence = !inFence;
            continue;
        }
        if (inFence) continue;
        const h2 = /^##\s+(.+?)\s*$/.exec(line);
        const h3 = /^###\s+(.+?)\s*$/.exec(line);
        if (h2) {
            const text = h2[1].trim();
            const slug = headingSlug(text);
            currentH2 = {
                id: `${parentId}#${slug}`,
                label: text,
                docs_slug: `${parentSlug}#${slug}`,
                children: [],
            };
            h2s.push(currentH2);
        } else if (h3 && currentH2) {
            const text = h3[1].trim();
            const slug = headingSlug(text);
            currentH2.children.push({
                id: `${parentId}#${slug}`,
                label: text,
                docs_slug: `${parentSlug}#${slug}`,
            });
        }
    }
    // Strip empty children arrays so leaf-shaped nodes stay
    // leaf-shaped in the emitted JSON.
    for (const h of h2s) {
        if (h.children.length === 0) delete h.children;
    }
    return h2s;
}

/**
 * Return the text of the first top-level `# H1` heading in a
 * markdown body, or '' if there is none. Code fences are skipped
 * (same rule as extractHeadings) so a `# comment` inside a code
 * block isn't mistaken for the page heading.
 */
function firstH1(body) {
    let inFence = false;
    for (const line of body.split('\n')) {
        if (/^```/.test(line)) {
            inFence = !inFence;
            continue;
        }
        if (inFence) continue;
        const m = /^#\s+(.+?)\s*$/.exec(line);
        if (m) return m[1].trim();
    }
    return '';
}

/**
 * Parse one .md / .mdx file's front-matter into a tree node.
 * The page title comes from the `title` front-matter field when
 * present; otherwise we fall back to the page's first `# H1`
 * heading so a page without a front-matter block still builds.
 * Throws only if neither source yields a title — the build fails
 * loudly with the offending path so authors notice immediately.
 *
 * The returned node is a CATEGORY (has children synthesized
 * from the file's h2/h3 headings) when the body has any h2;
 * otherwise it's a leaf. Either way `docs_slug` points at the
 * page itself so the detail panel's "Open docs" link goes
 * there, while heading-derived children link to
 * `/page#anchor` so the user lands at the right section.
 */
function parseLeaf(absPath, docsRoot) {
    const relPath = relative(docsRoot, absPath);
    const raw = readFileSync(absPath, 'utf-8');
    const { data: fm, content: body } = matter(raw);

    const fmTitle = (typeof fm.title === 'string' && fm.title.trim())
        ? fm.title.trim()
        : '';
    const title = fmTitle || firstH1(body);
    if (!title) {
        throw new Error(
            `extract-help-tree: no title for docs/${relPath} — add a \`title\` front-matter field or a \`# H1\` heading`,
        );
    }

    const id = idFromPath(relPath);
    const slug = slugFromPath(relPath, fm.slug);
    const node = {
        id,
        label: title,
        docs_slug: slug,
    };
    if (fm.description && typeof fm.description === 'string') {
        node.description = fm.description.trim();
    }
    if (fm.dashboard_route && typeof fm.dashboard_route === 'string') {
        node.dashboard_route = fm.dashboard_route.trim();
    }
    if (fm.wb_route && typeof fm.wb_route === 'string') {
        node.wb_route = fm.wb_route.trim();
    }
    const headings = extractHeadings(body, slug, id);
    if (headings.length > 0) node.children = headings;
    // Internal sort key — not emitted to the JSON.
    node._sortKey = typeof fm.sidebar_position === 'number'
        ? fm.sidebar_position
        : Number.POSITIVE_INFINITY;
    return node;
}

/**
 * Recursively build a tree for a docs directory. Subdirectories
 * become category nodes whose children are their .md files plus
 * any nested subdirs. Files become leaf nodes.
 */
function buildTree(dirAbs, docsRoot) {
    const { files, subdirs } = readDir(dirAbs);
    const children = [];

    for (const f of files) {
        children.push(parseLeaf(f.abs, docsRoot));
    }
    for (const sd of subdirs) {
        const subChildren = buildTree(sd.abs, docsRoot).children;
        if (subChildren.length === 0) continue;
        // Category label: humanize the directory name unless a
        // sibling `<dir>/_category_.json` provides one. We don't
        // currently use _category_.json so this is the path we hit.
        const label = sd.name
            .replace(/[-_]/g, ' ')
            .replace(/\b\w/g, c => c.toUpperCase());
        children.push({
            id: idFromPath(relative(docsRoot, sd.abs)),
            label,
            children: subChildren,
            // Directory nodes have no explicit sidebar_position;
            // fall back to alphabetical placement after positioned
            // leaves. Picking the min child sort key gives a
            // predictable ordering: a "legal/" dir whose pages are
            // 8 and 9 sorts at position 8.
            _sortKey: Math.min(...subChildren.map(c => c._sortKey ?? Number.POSITIVE_INFINITY)),
        });
    }

    // Sort: numeric sidebar_position ascending, then alphabetical
    // by id. Mirrors how Docusaurus orders the sidebar.
    // Note: we DON'T strip `_sortKey` here — the parent recursion
    // step computes its directory node's `_sortKey` as the min of
    // its children's keys, so they need to still be present.
    // Stripping happens in one final pass via `stripSortKeys()` at
    // the top level.
    children.sort((a, b) => {
        const pa = a._sortKey ?? Number.POSITIVE_INFINITY;
        const pb = b._sortKey ?? Number.POSITIVE_INFINITY;
        if (pa !== pb) return pa - pb;
        return a.id.localeCompare(b.id);
    });

    return { children };
}

/** Recursive post-pass: delete internal `_sortKey` before emit. */
function stripSortKeys(node) {
    delete node._sortKey;
    if (node.children) for (const c of node.children) stripSortKeys(c);
}

/**
 * Public entry point. Builds the tree from `docsRoot` (defaults
 * to the docs-site's own `docs/` directory) and returns the JSON
 * object — does NOT write to stdout. Tests import this function
 * directly; the CLI path goes through main() below.
 */
export function buildHelpTree(docsRoot = DEFAULT_DOCS_ROOT) {
    const { children } = buildTree(docsRoot, docsRoot);
    const out = {
        version: 1,
        generated_at: new Date().toISOString(),
        root: {
            id: 'help',
            label: 'Help',
            children,
        },
    };
    stripSortKeys(out.root);
    return out;
}

export function countNodes(node) {
    let n = 1;
    if (node.children) for (const c of node.children) n += countNodes(c);
    return n;
}

function main() {
    const out = buildHelpTree();
    process.stdout.write(JSON.stringify(out, null, 2) + '\n');
    process.stderr.write(
        `extract-help-tree: emitted ${countNodes(out.root)} nodes from ${DEFAULT_DOCS_ROOT}\n`,
    );
}

// Only run main() when invoked directly (`node scripts/...`),
// not when imported by tests.
if (import.meta.url === `file://${process.argv[1]}`) {
    try {
        main();
    } catch (err) {
        process.stderr.write(`extract-help-tree FAILED: ${err.message}\n`);
        process.exit(1);
    }
}
