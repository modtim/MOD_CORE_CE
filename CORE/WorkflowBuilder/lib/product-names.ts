/**
 * product-names.ts — WorkflowBuilder copy of the Oz theming display map.
 *
 * SOURCE OF TRUTH: CORE/Dashboard/js/product-names.js (OZ-A1, Phase 0).
 * This file is a VERBATIM DATA COPY of that map, kept inside CORE/WorkflowBuilder
 * because the WorkflowBuilder app can only resolve imports inside its own tree
 * (tsconfig.json "paths": {"@/*": ["./*"]} and the jest moduleNameMapper both
 * map `@/` to CORE/WorkflowBuilder/). The Dashboard file never reaches the WB
 * image (CORE/WorkflowBuilder/Dockerfile COPYs only the WB tree), so the old
 * `@/Dashboard/js/product-names.js` import could not resolve: 12 WB jest suites
 * failed with "Cannot find module '../js/product-names'" and a WB rebuild would
 * break at build time (FIX-WB-PRODUCT-NAMES-IMPORT...).
 *
 * Do NOT edit entries here: edit CORE/Dashboard/js/product-names.js, then
 * mirror it. CORE/WorkflowBuilder/__tests__/product-names-drift.test.ts goes
 * RED when either file changes alone (key set + codename + subtitle drift).
 * Python mirror: CORE/API/product_names.py (OZ-A2).
 *
 * The map is ADDITIVE: nothing here renames a page id, env var, compose
 * service, worker image name or API route. Unknown key -> today's literal
 * (never blank). Balloon Cloud (`burst-accounts`) ships in CE.
 */
export const PRODUCT_NAMES: Record<
  string,
  { codename: string; label: string; subtitle: string; paidOnly?: boolean }
> = {
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
  'counting-house': {codename: 'Counting House', label: 'Counting House - Plans and Spending', subtitle: 'plans, pricing and spending', paidOnly: true},};

/**
 * MOD_THEME_NAMES feature flag (OZ-A3). Default ON in CE and paid alike.
 * OFF (env MOD_THEME_NAMES=0|off|false|no, or window.MOD_THEME_NAMES set the
 * same way) renders today's literal strings byte for byte. The map is read at
 * RUN TIME only: keys are never hard-coded here.
 */
export function themeEnabled(flag?: unknown) {
  if (flag === undefined) {
    if (typeof window === 'object') {
      const w = window as unknown as Record<string, unknown>;
      if ('MOD_THEME_NAMES' in w) flag = w.MOD_THEME_NAMES;
    }
    if (flag === undefined) {
      const g = globalThis as unknown as Record<string, unknown>;
      const env = g.process && typeof g.process === 'object'
        ? (g.process as Record<string, unknown>).env
        : undefined;
      if (env && typeof env === 'object' && 'MOD_THEME_NAMES' in env) {
        flag = (env as Record<string, unknown>).MOD_THEME_NAMES;
      }
    }
  }
  if (flag === undefined || flag === null || flag === '') return true;
  const s = String(flag).trim().toLowerCase();
  return !(s === '0' || s === 'off' || s === 'false' || s === 'no');
}

export function themeLabel(key: string, today: string, flag?: unknown) {
  if (!themeEnabled(flag)) return today;
  const entry = PRODUCT_NAMES[key];
  return entry && entry.label ? entry.label : today;
}

/**
 * WORKFLOW BUILDER navigation + popup labels (owner 11-17, ).
 *
 * Builder-specific keys (the 'wb-nav-' family). They are NOT part of the verbatim
 * Dashboard map above (so the Dashboard sidebar entries Tim already approved, and
 * the product-names-drift test, are untouched): the builder's left-hand function
 * list and the dialogs those entries open read their text from here, so a button
 * and the popup it opens always carry the same name. Theme OFF or an unknown key
 * returns today's literal (never blank), exactly like themeLabel.
 */
export const WB_NAV_LABELS: Record<string, string> = {
  'wb-nav-dashboard': 'O.Z. - Dashboard',
  'wb-nav-wizard-nodes': 'Wizard - Nodes',
  'wb-nav-wizard-flow': 'Wizard - Workflows',
  'wb-nav-wizard-stacks': 'Wizard - Stacks',
  'wb-nav-wizard-projects': 'Wizard - Projects',
  'wb-nav-process-queue': 'The Crossroads - Process Queue',
  'wb-nav-approvals': 'Guardian - Approvals',
  'wb-nav-wizard-queue': 'Behind The Curtain - Wizard Queue',
  'wb-nav-triggers': 'The Flying Monkeys - Triggers',
  'wb-nav-node-library': 'The Bricks - Node Library',
  'wb-nav-assistant': 'Glinda - Mod Assistant',
};

export function wbNavLabel(key: string, today: string, flag?: unknown) {
  if (!themeEnabled(flag)) return today;
  const label = WB_NAV_LABELS[key];
  return label ? label : today;
}
