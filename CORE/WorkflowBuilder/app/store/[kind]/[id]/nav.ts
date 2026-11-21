/**
 * Navigation seam for the store detail page.
 *
 * The page never assigns `location.href` directly (jsdom cannot intercept that
 * assignment), so every navigation it performs goes through this swappable
 * object; tests spy on `nav.go` and assert the recorded URLs.
 *
 * It lives in a sibling module rather than in `page.tsx` because Next's
 * generated app-router type guard for `app/store/[kind]/[id]/page.tsx` only
 * allows the entry-shape names (default + the known config props) to be
 * exported from the page module itself.
 */
export const nav = {
  go: (url: string): void => {
    window.location.assign(url);
  },
};
