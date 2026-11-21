// GSEC R-074 — tokens out of URLs.
//
// Dashboard → WorkflowBuilder (and library deep links) hand the session across
// by putting the access + refresh tokens in the URL. They used to ride in the
// QUERY string (?token=...&refresh_token=...), which is what the nginx /
// uvicorn access logs record — the tokens were being logged on every handoff.
// Senders now put them in the URL FRAGMENT (#token=...&refresh_token=...),
// which the browser never sends to the server, so it never reaches any log.
//
// parseHandoffTokens() reads the fragment first and falls back to the legacy
// query-string params so links minted by an older Dashboard (query form)
// still work for one release. stripHandoffTokens() then removes BOTH the
// fragment tokens and any legacy query tokens via history.replaceState so
// they don't linger in the address bar or history (and a refresh can't
// re-consume them). Every other query param (workflow, resume, node_search,
// checkout…) is preserved — only token/refresh_token are deleted.

export interface HandoffTokens {
    accessToken: string | null;
    refreshToken: string | null;
}

const TOKEN_PARAM = "token";
const REFRESH_TOKEN_PARAM = "refresh_token";

/**
 * Read handoff tokens from `location` — fragment (new senders) first, then
 * the legacy query string (old links, one-release fallback). Pure read: does
 * not touch history or storage.
 */
export function parseHandoffTokens(
    location: Pick<Location, "search" | "hash">,
): HandoffTokens {
    const fromFragment = parseFragmentTokens(location.hash);
    if (fromFragment.accessToken || fromFragment.refreshToken) {
        return fromFragment;
    }
    return parseQueryStringTokens(location.search);
}

function parseFragmentTokens(hash: string): HandoffTokens {
    const frag = hash.replace(/^#/, "");
    if (!frag.includes("=")) return { accessToken: null, refreshToken: null };
    const params = new URLSearchParams(frag);
    const accessToken = params.get(TOKEN_PARAM);
    const refreshToken = params.get(REFRESH_TOKEN_PARAM);
    return {
        accessToken: accessToken || null,
        refreshToken: refreshToken || null,
    };
}

function parseQueryStringTokens(search: string): HandoffTokens {
    const params = new URLSearchParams(search);
    const accessToken = params.get(TOKEN_PARAM);
    const refreshToken = params.get(REFRESH_TOKEN_PARAM);
    return {
        accessToken: accessToken || null,
        refreshToken: refreshToken || null,
    };
}

/**
 * Remove every handoff token from the URL (fragment + legacy query) via
 * history.replaceState, preserving all other query params and the pathname.
 * The resulting URL has no `#` if the fragment held only token params, and a
 * clean query string otherwise.
 */
export function stripHandoffTokens(
    location: Pick<Location, "href" | "search" | "hash">,
    history: Pick<History, "replaceState">,
): void {
    const url = new URL(location.href);

    const frag = location.hash.replace(/^#/, "");
    const fragParams = new URLSearchParams(frag);
    fragParams.delete(TOKEN_PARAM);
    fragParams.delete(REFRESH_TOKEN_PARAM);
    const newHash = fragParams.toString();
    url.hash = newHash ? `#${newHash}` : "";

    url.searchParams.delete(TOKEN_PARAM);
    url.searchParams.delete(REFRESH_TOKEN_PARAM);

    history.replaceState(null, "", url.toString());
}
