/**
 * MOD Assistant — shared client-side ACTION runtime (mod-assistant-agent §6.4).
 *
 * This is the app-facing entry point for the assistant action runtime that
 * the panels call: navigate / open-new-tab / open-dialog (plus open-tab,
 * scroll-to, highlight, chain, support-contacted). The canonical
 * implementation already lives in `@/lib/assistant-nav/navActions.ts` —
 * BOTH the WorkflowBuilder panel (`components/assistant-panel.tsx`) and the
 * Dashboard panel (`CORE/Dashboard/js/assistant-panel.js`, via the
 * generated `js/vendor/assistant-nav.js`) consume that one module, so this
 * file is a re-export, not a second copy. Keeping a single source of truth
 * here is deliberate: a duplicated runtime is exactly the drift hazard §6.4
 * was created to eliminate (the two apps' ~identical inline copies diverged
 * silently before that extraction).
 *
 * Why a re-export instead of moving the file: `navActions.ts` is also the
 * input to the Dashboard vendor build (`lib/assistant-nav/build-dashboard.js`
 * + `tsconfig.dashboard.json` compile it to `js/vendor/assistant-nav.js`,
 * which assigns `window.ModAssistantNav`). Moving the canonical body to this
 * path would require regenerating that vendor artifact and re-pointing the
 * build config — a build step, out of scope for this job. Re-exporting keeps
 * the vendor pipeline, the launcher import, and the existing navActions test
 * all working untouched, while giving the panels the shared path the spec
 * names.
 *
 * Security note: the same-origin / shape allow-list (isValidRoute, isSafeId,
 * isSafeAnchorSelector, isKnownActionType) and the server-side site-map +
 * per-tenant allow-list in `services/assistant_actions.py` are the security
 * logic. They are NOT reimplemented or weakened here — this file only
 * re-exports them unchanged.
 */

export * from "@/lib/assistant-nav/navActions";
