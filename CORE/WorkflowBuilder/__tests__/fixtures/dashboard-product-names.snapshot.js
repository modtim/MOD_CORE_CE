/**
 * product-names.js — Oz theming display map (OZ-A1, Phase 0).
 *
 * ONE source of truth for the themed product names. Keyed by the INTERNAL
 * identifier (page id / nav id / router id) -> {codename, label, subtitle}.
 * The map is ADDITIVE: nothing here renames a page id, env var, compose
 * service, worker image name or API route. Renders read `label` + `subtitle`
 * under MOD_THEME_NAMES (OZ-A3); unknown key -> today's literal (never blank).
 *
 * Source of the names: the internal design notes (owner-fixed,
 * 28 products, all FIXED as of 11-14). Style model: AWS ("Amazon Athena
 * (Query data in S3)") = memorable codename + plain-language subtitle.
 * Python mirror: CORE/API/product_names.py (OZ-A2).
 *
 * Balloon Cloud (`burst-accounts`, burst to
 * the customer's OWN public cloud account) ships in CE and is NOT flagged.
 *
 * ANCHOR TABLE (corrections leg from REV-OZ-A1-JS-MAP ec5707b688, 11-14;
 * proof by grep in the lane clone). The 14 NAME-MAP identifiers with their
 * real Dashboard surface id and API router prefix. Keys are NOT renamed here
 * (OZ-B1 renames keys to anchors in its own leg); "none" = no such id exists
 * in CE today. Only rename made in this map: manager -> managers (the real
 * System sub-tab id, analytics.js:116).
 *   key           | Dashboard id                    | API prefix (CORE/API/main.py)          | CE
 *   plane         | none                            | none (seed_plane_node 544/5754)       | no CE surface
 *   wizards       | none (wizard-run-row 309 is a testid) | wizard_ml_admin /v1 3193 (optional_paid) | no CE page; API paid-only
 *   converter     | none                            | none (seed_converters 5193)           | no CE surface today
 *   deploy        | none (data-ps-act 71 is a button) | none                               | no CE surface today
 *   stacks        | System sub-tab analytics.js:116 | stack_call /v1 3255                  | ships in CE
 *   agents        | none                            | none (comment 2380)                  | no CE surface today
 *   managers      | System sub-tab analytics.js:116 | managers router_mapping 3386         | ships in CE
 *   node-builder  | none                            | auto_node_build /v1/auto-node-build 3201 | ships in CE (API only)
 *   sleep-back    | none                            | none (model-cache 3330)              | no CE surface today
 *   image-mover   | none                            | none                                 | no CE surface (no UI today)
 *   vector_store  | none                            | vector_store /v1 3307                | ships in CE (API only)
 *   triggers      | none (#triggers only in a stale doc) | http_triggers /v1 3279          | ships in CE (API only)
 *   workflows     | nav href #workflows dashboard-shell.html:32 (external redirect, sidebar-active-highlight.check.js:93) | _workflows_debug_ws /v1 3640 | ships in CE
 *   (also anchored, outside the 14: burst-accounts = System sub-tab analytics.js:116,
 *    API burst_accounts /v1/burst 2544 - Balloon, ships in CE.)
 */
export const PRODUCT_NAMES = {
  'mod-core': {codename: 'OZ (Orchestration Zone)', label: 'OZ - Orchestration Zone', subtitle: 'The whole system: every zone in one place'},
  'plane': {codename: 'Dorothy', label: 'Dorothy - Voice', subtitle: 'Talk to MOD, hands free'},
  'workflow-builder': {codename: 'Toto', label: 'Toto - Workflows', subtitle: 'Build and run node graphs'},
  'wizards': {codename: 'Wizards', label: 'Wizards - Node Wizard', subtitle: 'Describe it, get a working node'},
  'converter': {codename: 'Woodsman', label: 'Woodsman - Converter', subtitle: 'Convert code and hardware'},
  'assistant-launcher': {codename: 'Glinda', label: 'Glinda - Help', subtitle: 'First login: chat that takes you places'},
  'assistant-panel': {codename: 'Glinda', label: 'Glinda - Mod Assistant', subtitle: 'Chat that takes you places and explains a failed run'},
  'workflows': {codename: 'The Road', label: 'The Road - Orchestration', subtitle: 'Runs move work across zones'},
  'compliance-center': {codename: 'Lion', label: 'Lion - Compliance', subtitle: 'Check rules, obligations, scanners'},
  'capture-devices': {codename: 'Field', label: 'Field - Capture', subtitle: 'Capture what your devices see'},
  'marketplace': {codename: 'Kansas', label: 'Kansas - Marketplace', subtitle: 'Find and install listings'},
  'triggers': {codename: 'Monkeys', label: 'Monkeys - Triggers', subtitle: 'Fire on events and schedules'},
  'dashboard-shell': {codename: 'The City', label: 'The City - Dashboard', subtitle: 'See everything, in one place'},
  'vector_store': {codename: 'Cyclone', label: 'Cyclone - RAG', subtitle: 'Feed documents to your assistant'},
  'deploy': {codename: 'Silver', label: 'Silver - Deploy', subtitle: 'Ship MOD to a host'},
  'stacks': {codename: 'Emeralds', label: 'Emeralds - Stacks', subtitle: 'Run models on your hardware'},
  'workers': {codename: 'Quadlings', label: 'Quadlings - Workers', subtitle: 'Run jobs on your machines'},
  'agents': {codename: 'Scarecrow', label: 'Scarecrow - Agents', subtitle: 'Autonomous helpers on your tasks'},
  'managers': {codename: 'Ozma', label: 'Ozma - Manager', subtitle: 'Manage zones and loans'},
  'scheduler': {codename: 'Clockwork Scheduler', label: 'Clockwork - Scheduler', subtitle: 'Run jobs on a schedule'},
  'burst-accounts': {codename: 'Balloon Cloud', label: 'Balloon Cloud - Burst Accounts', subtitle: 'Burst jobs to your own cloud account'},
  'lockbox': {codename: 'The Belt', label: 'The Belt - Secrets', subtitle: 'Keep secrets safe'},
  'admin-sso': {codename: 'The Gate', label: 'The Gate - Login', subtitle: 'Sign in, join the zone'},
  'node-builder': {codename: 'Tinker', label: 'Tinker - Node Builder', subtitle: 'Build nodes from nothing'},
  'files': {codename: 'The Cellar', label: 'The Cellar - Files', subtitle: 'Store files and models'},
  'sleep-back': {codename: 'Poppies', label: 'Poppies - Sleep-Back', subtitle: 'Models rest, wake fast'},
  'security': {codename: 'The Guard (Omby Amby)', label: 'The Guard - Security', subtitle: 'Watch for trouble, prove clean'},
  'image-mover': {codename: 'Sawhorse', label: 'Sawhorse - Mover', subtitle: 'Move images and models between hosts'},
  'payments-admin': {codename: 'The Mint', label: 'The Mint - Payments', subtitle: 'Take money, see payouts', paidOnly: true},
  'promo-codes': {codename: 'Lucky Charm', label: 'Lucky Charm - Promo Codes', subtitle: 'Discount codes', paidOnly: true},
  'free-trials': {codename: 'First Steps', label: 'First Steps - Free Trials', subtitle: 'Try before you pay', paidOnly: true},
  'geographic-pricing': {codename: 'Kingdom Map', label: 'Kingdom Map - Geographic Pricing', subtitle: 'Price by country', paidOnly: true},
  'seasonal-pricing': {codename: 'Harvest Moon', label: 'Harvest Moon - Seasonal Pricing', subtitle: 'Time-limited prices', paidOnly: true},
  'webhooks': {codename: 'Message Bell', label: 'Message Bell - Webhooks', subtitle: 'Send events to other systems'},
  'drift-events': {codename: 'Crystal Ball', label: 'Crystal Ball - Drift Events', subtitle: 'Things that changed from the plan'},
  'settings': {codename: 'Gearworks', label: 'Gearworks - Settings', subtitle: 'System settings'},
  'help': {codename: 'Signpost', label: 'Signpost - Help', subtitle: 'Help pages'},
  'storage': {codename: 'Granary', label: 'Granary - Storage', subtitle: 'Storage connections'},
  'counting-house': {codename: 'Counting House', label: 'Counting House - Plans and Spending', subtitle: 'plans, pricing and spending', paidOnly: true},
};

/**
 * MOD_THEME_NAMES feature flag (OZ-A3). Default ON in CE and paid alike.
 * OFF (env MOD_THEME_NAMES=0|off|false|no, or window.MOD_THEME_NAMES set the
 * same way) renders today's literal strings byte for byte; ON renders the map
 * `label`. Unknown key -> today's literal (never blank). The map is read at
 * RUN TIME only: keys are never hard-coded here, so a concurrent key rename
 * (e.g. `manager` -> `managers` by the corrections lane) needs no change to
 * this helper. Anchors that do not exist yet are simply not themed (no error).
 */
export function themeEnabled(flag) {
  if (flag === undefined) {
    if (typeof window === 'object' && 'MOD_THEME_NAMES' in window) {
      flag = window.MOD_THEME_NAMES;
    } else if (typeof process === 'object' && process.env) {
      flag = process.env.MOD_THEME_NAMES;
    }
  }
  if (flag === undefined || flag === null || flag === '') return true;
  const s = String(flag).trim().toLowerCase();
  return !(s === '0' || s === 'off' || s === 'false' || s === 'no');
}

export function themeLabel(key, today, flag) {
  if (!themeEnabled(flag)) return today;
  const entry = PRODUCT_NAMES[key];
  return entry && entry.label ? entry.label : today;
}
