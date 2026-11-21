#!/usr/bin/env bash
#
# CI lint guard for the Dashboard theming refactor (launch-day-followup-5
# §4.5). Fails (exit non-zero) if any of the retired token forms appear
# in source files, so a regression doesn't silently re-introduce the
# old `--mod-*` prefix or `bg-brand-*` utility classes.
#
# Checks:
#   1. `--mod-`                — old CSS variable prefix
#   2. `bg-brand-` / `text-brand-` / `border-brand-` / `hover:bg-brand-`
#      / `hover:text-brand-` / `focus:ring-brand-` / `focus:border-brand-`
#                              — old brand utility classes
#   3. `text-text-(primary|secondary|muted|inverse|link)`
#                              — old text-* utility namespace
#   4. `bg-surface-(base|raised|sunken|overlay|inverse)`
#                              — old surface-* utility namespace
#   5. `text-feedback-` / `bg-feedback-` / `border-feedback-`
#                              — old feedback-* utility namespace
#   6. `#06B6D4` / `#0891B2`   — old wrong cyan brand hex
#   7. `'Inter'` or `"Inter"`  — old font family
#   8. tokens.css drift        — Dashboard/tokens.css must match
#                                /assets/branding/tokens.css except
#                                for the build-time font path tweak
#
# Run from the repo root or from CORE/Dashboard. The script auto-
# locates its own dashboard root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DASHBOARD_DIR}/../.." && pwd)"
CANONICAL_TOKENS="${REPO_ROOT}/assets/branding/tokens.css"
LOCAL_TOKENS="${DASHBOARD_DIR}/tokens.css"

failures=0

# Files to scan: HTML, JS, CSS — but not the script itself, not built
# tailwind.css output (regenerated from input.css), not node_modules.
scan_paths=(
    "${DASHBOARD_DIR}/index.html"
    "${DASHBOARD_DIR}/downloads.html"
    "${DASHBOARD_DIR}/style.css"
    "${DASHBOARD_DIR}/input.css"
    "${DASHBOARD_DIR}/pages"
    "${DASHBOARD_DIR}/modals"
    "${DASHBOARD_DIR}/js"
)

# Build a find expression for the existing files/dirs.
existing_paths=()
for p in "${scan_paths[@]}"; do
    if [[ -e "$p" ]]; then
        existing_paths+=("$p")
    fi
done

if [[ ${#existing_paths[@]} -eq 0 ]]; then
    echo "ERROR: no Dashboard source paths to scan from $DASHBOARD_DIR" >&2
    exit 2
fi

# Helper: grep -E across the source tree, excluding node_modules and
# the built tailwind.css. Prints offending matches and bumps `failures`.
fail_if_match() {
    local pattern="$1"
    local label="$2"
    local matches
    # `|| true` so an empty match doesn't trip set -e.
    matches=$(
        find "${existing_paths[@]}" \
            -type f \
            \( -name "*.html" -o -name "*.js" -o -name "*.css" \) \
            ! -path "*/node_modules/*" \
            ! -name "tailwind.css" \
            -print0 \
        | xargs -0 grep -nE "$pattern" 2>/dev/null \
        || true
    )
    if [[ -n "$matches" ]]; then
        echo "FAIL [$label]:"
        echo "$matches" | sed 's/^/    /'
        echo
        failures=$((failures + 1))
    fi
}

echo "Dashboard theming lint — checking for retired tokens..."
echo

fail_if_match '--mod-'                                                    "old --mod- CSS prefix"
fail_if_match '\b(bg|text|border)-brand-[0-9]+\b'                         "old brand utility class"
fail_if_match '\bhover:(bg|text|border)-brand-[0-9]+\b'                   "old hover:brand utility"
fail_if_match '\bfocus:(ring|border|bg|text)-brand-[0-9]+\b'              "old focus:brand utility"
fail_if_match '\btext-text-(primary|secondary|muted|inverse|link)\b'      "old text-text- utility"
fail_if_match '\bbg-surface-(base|raised|sunken|overlay|inverse)\b'       "old bg-surface- utility"
fail_if_match '\b(text|bg|border)-feedback-(success|warning|error|info)(-soft)?\b' \
                                                                          "old feedback- utility"
fail_if_match '#06B6D4|#0891B2|#0E7490|#67E8F9|#22D3EE'                   "old wrong-cyan brand hex"
fail_if_match "['\"]Inter['\"]"                                           "Inter font family"

# tokens.css drift check. The local copy diverges from the canonical
# only on the @font-face url() lines (relative path differs) so we
# normalize both sides before diffing.
echo "tokens.css drift — comparing $LOCAL_TOKENS vs $CANONICAL_TOKENS..."
if [[ ! -f "$CANONICAL_TOKENS" ]]; then
    echo "WARN: canonical tokens.css not found at $CANONICAL_TOKENS — skipping drift check"
elif [[ ! -f "$LOCAL_TOKENS" ]]; then
    echo "FAIL: $LOCAL_TOKENS missing"
    failures=$((failures + 1))
else
    # Strip url('./fonts/...') vs url('./branding/fonts/...') divergence
    # and the local-only header comment block. The header block is the
    # first /* ... */ comment, which is also where the font-path note
    # lives. We compare from `:root {` onwards.
    canonical_body=$(awk '/:root[[:space:]]*{/{found=1} found' "$CANONICAL_TOKENS")
    local_body=$(awk '/:root[[:space:]]*{/{found=1} found' "$LOCAL_TOKENS")
    if [[ "$canonical_body" != "$local_body" ]]; then
        echo "FAIL: Dashboard/tokens.css has drifted from /assets/branding/tokens.css"
        echo "      Run:"
        echo "        cp $CANONICAL_TOKENS $LOCAL_TOKENS"
        echo "      then re-apply the @font-face url('./branding/fonts/...') tweak."
        diff <(echo "$canonical_body") <(echo "$local_body") | head -40 | sed 's/^/    /'
        echo
        failures=$((failures + 1))
    fi
fi

echo
if [[ "$failures" -gt 0 ]]; then
    echo "RESULT: $failures check(s) failed."
    exit 1
fi
echo "RESULT: all checks passed."
