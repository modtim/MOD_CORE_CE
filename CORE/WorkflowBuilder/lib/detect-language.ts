/**
 * Lightweight language detection for the New Node wizard's paste handler
 * (#18, launch-day-followup-2).
 *
 * Returns one of the dropdown values used in `new-node-wizard.tsx`:
 *   python3 | bash | node | typescript | ruby | php | perl | lua |
 *   go | rust | c | cpp | java | r | text | json | yaml | toml | xml
 *
 * Or `null` when the snippet is too short / ambiguous to call confidently.
 *
 * Why not highlight.js / guesslang: bundle weight (~150kb gzipped for
 * highlight.js, ~5MB for guesslang's WASM model). For the wizard's
 * "auto-fill the dropdown when someone pastes" UX, a couple dozen
 * shebang/keyword rules are enough. Wrong calls are corrected by the
 * user picking the dropdown manually — same as before, just with a
 * better starting point on the common-case paste.
 */

type Lang =
    | "python3" | "bash" | "node" | "typescript" | "ruby" | "php" | "perl"
    | "lua" | "go" | "rust" | "c" | "cpp" | "java" | "r" | "text"
    | "json" | "yaml" | "toml" | "xml";

interface Rule {
    lang: Lang;
    /** Higher = run first. Strong signals (shebangs) outrank generic keyword counts. */
    priority: number;
    test: (src: string, firstLine: string, hasNewlines: boolean) => number;
}

const RULES: Rule[] = [
    // Shebangs — strongest possible signal, bypass the score floor.
    {
        lang: "python3", priority: 100,
        test: (_s, fl) => /^#!.*\b(python3?|pypy)\b/.test(fl) ? 1000 : 0,
    },
    {
        lang: "bash", priority: 100,
        test: (_s, fl) => /^#!.*\b(bash|sh|zsh)\b/.test(fl) ? 1000 : 0,
    },
    {
        lang: "node", priority: 100,
        test: (_s, fl) => /^#!.*\bnode\b/.test(fl) ? 1000 : 0,
    },
    {
        lang: "perl", priority: 100,
        test: (_s, fl) => /^#!.*\bperl\b/.test(fl) ? 1000 : 0,
    },
    {
        lang: "ruby", priority: 100,
        test: (_s, fl) => /^#!.*\bruby\b/.test(fl) ? 1000 : 0,
    },

    // PHP open-tag — also unmistakable.
    {
        lang: "php", priority: 95,
        test: (s) => /<\?php\b/.test(s) ? 1000 : 0,
    },

    // XML / HTML preamble.
    {
        lang: "xml", priority: 90,
        test: (s, fl) => {
            if (/^\s*<\?xml\b/.test(fl)) return 1000;
            if (/^\s*<!DOCTYPE\b/.test(fl)) return 600;
            return 0;
        },
    },

    // JSON — must start with { or [ and contain "key":
    {
        lang: "json", priority: 80,
        test: (s) => {
            const trimmed = s.trim();
            if (!/^[{[]/.test(trimmed)) return 0;
            // Quick sanity: looks like JSON if there are at least two
            // "key": or " : " patterns and balanced quotes are likely.
            const pairs = (trimmed.match(/"[^"\n]+"\s*:/g) || []).length;
            // Reject if there's `=>` (PHP/Ruby hash) or `=` outside strings.
            if (/=>/.test(trimmed)) return 0;
            return pairs >= 2 ? 50 + pairs * 5 : 0;
        },
    },

    // TOML — section headers + key = value
    {
        lang: "toml", priority: 70,
        test: (s) => {
            const sections = (s.match(/^\[[A-Za-z0-9_.-]+\]\s*$/gm) || []).length;
            const kvPairs = (s.match(/^[A-Za-z_][A-Za-z0-9_]*\s*=\s*("|\d|true|false|\[)/gm) || []).length;
            if (sections >= 1 && kvPairs >= 1) return 30 + sections * 5 + kvPairs * 2;
            return 0;
        },
    },

    // YAML — `key: value` block style + optional `---` doc separator
    {
        lang: "yaml", priority: 65,
        test: (s, fl) => {
            const docSep = /^---\s*$/m.test(s) ? 30 : 0;
            const kvLines = (s.match(/^[A-Za-z_][A-Za-z0-9_-]*\s*:\s*\S/gm) || []).length;
            const listItems = (s.match(/^\s*-\s+\S/gm) || []).length;
            if (kvLines >= 2 || (kvLines >= 1 && (docSep || listItems >= 1))) {
                return 20 + kvLines * 3 + listItems * 2 + docSep;
            }
            return 0;
        },
    },

    // TypeScript — TS-specific patterns over plain JS
    {
        lang: "typescript", priority: 60,
        test: (s) => {
            let score = 0;
            if (/\binterface\s+[A-Z]\w*\s*{/.test(s)) score += 30;
            if (/:\s*(string|number|boolean|any|unknown|void)\b/.test(s)) score += 20;
            if (/\bas\s+(string|number|boolean|unknown|any)\b/.test(s)) score += 15;
            if (/\btype\s+\w+\s*=/.test(s)) score += 15;
            if (/<[A-Z]\w*>/.test(s)) score += 5;
            return score;
        },
    },

    // JavaScript / Node
    {
        lang: "node", priority: 55,
        test: (s) => {
            let score = 0;
            if (/\b(const|let|var)\s+\w+\s*=/.test(s)) score += 15;
            if (/=>\s*[{(]/.test(s)) score += 15;
            if (/\bfunction\s+\w+\s*\(/.test(s)) score += 10;
            if (/\brequire\s*\(\s*['"]/.test(s)) score += 20;
            if (/\bmodule\.exports\b/.test(s)) score += 25;
            if (/\bconsole\.log\s*\(/.test(s)) score += 10;
            return score;
        },
    },

    // Python (kw + colon-indent blocks)
    {
        lang: "python3", priority: 55,
        test: (s) => {
            let score = 0;
            if (/^\s*def\s+\w+\s*\(.*\)\s*:/m.test(s)) score += 25;
            if (/^\s*class\s+\w+(\(.*\))?\s*:/m.test(s)) score += 20;
            if (/^\s*import\s+\w+/m.test(s)) score += 10;
            if (/^\s*from\s+\w+\s+import\b/m.test(s)) score += 15;
            if (/\bprint\s*\(/.test(s)) score += 5;
            if (/^\s*if\s+__name__\s*==\s*['"]__main__['"]\s*:/m.test(s)) score += 30;
            return score;
        },
    },

    // Bash — keywords + $vars (without shebang)
    {
        lang: "bash", priority: 50,
        test: (s) => {
            let score = 0;
            if (/\becho\s+/.test(s)) score += 5;
            if (/\bif\s*\[\s*/.test(s)) score += 10;
            if (/\bfi\b/.test(s)) score += 10;
            if (/\bdone\b/.test(s)) score += 10;
            if (/\$\{?\w+\}?/.test(s)) score += 5;
            if (/\bexport\s+\w+=/.test(s)) score += 10;
            return score;
        },
    },

    // Go
    {
        lang: "go", priority: 50,
        test: (s) => {
            let score = 0;
            if (/^\s*package\s+\w+\s*$/m.test(s)) score += 30;
            if (/^\s*import\s+\(/m.test(s)) score += 20;
            if (/\bfunc\s+\w+\s*\(/.test(s)) score += 15;
            if (/:=/.test(s)) score += 10;
            return score;
        },
    },

    // Rust
    {
        lang: "rust", priority: 50,
        test: (s) => {
            let score = 0;
            if (/\bfn\s+\w+\s*\(/.test(s)) score += 15;
            if (/\blet\s+mut\s+\w+/.test(s)) score += 20;
            if (/\bpub\s+fn\b/.test(s)) score += 20;
            if (/\buse\s+std::/.test(s)) score += 25;
            if (/\bimpl\s+\w+\s+for\s+\w+/.test(s)) score += 20;
            return score;
        },
    },

    // Ruby
    {
        lang: "ruby", priority: 50,
        test: (s) => {
            let score = 0;
            if (/^\s*def\s+\w+(\(.*\))?\s*$/m.test(s)) score += 20;
            if (/^\s*end\s*$/m.test(s)) score += 5;
            if (/\bputs\s+/.test(s)) score += 10;
            if (/\brequire\s+['"]/.test(s)) score += 15;
            if (/\bmodule\s+\w+/.test(s)) score += 15;
            return score;
        },
    },

    // Lua
    {
        lang: "lua", priority: 50,
        test: (s) => {
            let score = 0;
            if (/\blocal\s+\w+\s*=/.test(s)) score += 20;
            if (/\bfunction\s+\w+(\.\w+)?\s*\(/.test(s)) score += 15;
            if (/\bnil\b/.test(s)) score += 5;
            if (/^\s*end\s*$/m.test(s) && /\bfunction\b/.test(s)) score += 5;
            return score;
        },
    },

    // C / C++
    {
        lang: "cpp", priority: 50,
        test: (s) => {
            if (/\bstd::/.test(s)) return 30;
            if (/^\s*#include\s*<\w+>/.test(s) && /\bclass\s+\w+/.test(s)) return 25;
            return 0;
        },
    },
    {
        lang: "c", priority: 45,
        test: (s) => {
            let score = 0;
            if (/^\s*#include\s*<\w+\.h>/m.test(s)) score += 20;
            if (/\bint\s+main\s*\(/.test(s)) score += 25;
            if (/\bprintf\s*\(/.test(s)) score += 10;
            return score;
        },
    },

    // Java
    {
        lang: "java", priority: 50,
        test: (s) => {
            let score = 0;
            if (/\bpublic\s+class\s+\w+/.test(s)) score += 30;
            if (/^\s*import\s+java\./.test(s)) score += 25;
            if (/^\s*package\s+[a-z][\w.]*\s*;/m.test(s)) score += 20;
            if (/\bSystem\.out\.println\b/.test(s)) score += 20;
            return score;
        },
    },

    // R
    {
        lang: "r", priority: 45,
        test: (s) => {
            let score = 0;
            if (/<-/.test(s)) score += 15;
            if (/\blibrary\s*\(/.test(s)) score += 20;
            if (/\bfunction\s*\(/.test(s)) score += 5;
            if (/\bdata\.frame\s*\(/.test(s)) score += 25;
            return score;
        },
    },
];

/**
 * Detect the language of a code snippet. Returns the dropdown value or
 * null if the snippet is too short or no rule scores high enough.
 *
 * Confidence threshold (40 by default) is conservative — we'd rather
 * leave the user's manually-chosen language alone than override it
 * on a bad guess.
 */
export function detectLanguage(src: string, threshold = 40): Lang | null {
    if (!src || src.length < 30) return null;
    const firstLine = (src.match(/^[^\n]*/) || [""])[0];
    const hasNewlines = src.includes("\n");

    let best: { lang: Lang; score: number } | null = null;
    for (const rule of [...RULES].sort((a, b) => b.priority - a.priority)) {
        const score = rule.test(src, firstLine, hasNewlines);
        if (score > 0 && (!best || score > best.score)) {
            best = { lang: rule.lang, score };
        }
    }
    if (!best || best.score < threshold) return null;
    return best.lang;
}
