"""product_names.py — Oz theming display map (OZ-A2, Phase 0).

ONE source of truth for the themed product names on the Python side: the exact
mirror of CORE/Dashboard/js/product-names.js (OZ-A1). Keyed by the INTERNAL
identifier (page id / nav id / router id) -> {codename, label, subtitle}.

The map is ADDITIVE: nothing here renames a page id, env var, compose service,
worker image name or API route. Renders read `label` + `subtitle` under the
MOD_THEME_NAMES feature flag (OZ-A3/OZ-0.5); an unknown key falls back to
today's literal (never blank).

Source of the names: the internal design notes (owner-fixed,
27 products, all FIXED as of 11-14). Style model: AWS ("Amazon Athena (Query
data in S3)") = memorable codename + plain-language subtitle.

Balloon Cloud (`burst-accounts`, burst to the
customer's OWN public cloud account) ships in CE and is NOT flagged.

Guard: open-core/tests/test_product_names_js_python_map_agree.py asserts this
dict agrees key-for-key and field-for-field with the JS map (28 keys). The
guard compares codename/label/subtitle only; `paidOnly` is an extra field both
sides carry and the guard ignores.

Note (OZ-A1 flag, corrected by the corrections leg from REV-OZ-A1-JS-MAP
ec5707b688, recorded here, NOT renamed): of the 14 NAME-MAP identifiers, the
real CE surfaces are - Dashboard sub-tab/nav ids: `stacks`, `managers` (was
`manager`; the real System sub-tab id, analytics.js:116), `workflows`
(nav href `#workflows`, a same-page hash, not an external redirect), plus `burst-accounts` (Balloon, System sub-tab). API-only routers:
`node-builder` (/v1/auto-node-build), `vector_store` (/v1), `triggers`
(/v1 http_triggers), `scheduler` (scheduler_admin /v1/clockwork). No CE surface today
(no page id, no router): `plane`, `wizards` (the wizard_ml_admin router is
paid), `converter`, `deploy`, `agents`, `sleep-back`, `image-mover` (Sawhorse
has no shipped id at all). OZ-B1 renames keys to the real anchors in its own
leg; renaming a key here without that result note is not this job's call.
"""
from __future__ import annotations

PRODUCT_NAMES: dict[str, dict[str, str]] = {
    "mod-core": {
        "codename": "OZ (Orchestration Zone)",
        "label": "OZ - Orchestration Zone",
        "subtitle": "The whole system: every zone in one place",
    },
    "plane": {
        "codename": "Dorothy",
        "label": "Dorothy - Voice",
        "subtitle": "Talk to MOD, hands free",
    },
    "workflow-builder": {
        "codename": "Toto",
        "label": "Toto - Workflows",
        "subtitle": "Build and run node graphs",
    },
    "wizards": {
        "codename": "Wizards",
        "label": "Wizards - Node Wizard",
        "subtitle": "Describe it, get a working node",
    },
    "converter": {
        "codename": "Woodsman",
        "label": "Woodsman - Converter",
        "subtitle": "Convert code and hardware",
    },
    "assistant-launcher": {
        "codename": "Glinda",
        "label": "Glinda - Help",
        "subtitle": "First login: chat that takes you places",
    },
    "assistant-panel": {
        "codename": "Glinda",
        "label": "Glinda - Mod Assistant",
        "subtitle": "Chat that takes you places and explains a failed run",
    },
    "workflows": {
        "codename": "The Road",
        "label": "The Road - Orchestration",
        "subtitle": "Runs move work across zones",
    },
    "compliance-center": {
        "codename": "Lion",
        "label": "Lion - Compliance",
        "subtitle": "Check rules, obligations, scanners",
    },
    "capture-devices": {
        "codename": "Field",
        "label": "Field - Capture",
        "subtitle": "Capture what your devices see",
    },
    "marketplace": {
        "codename": "Kansas",
        "label": "Kansas - Marketplace",
        "subtitle": "Find and install listings",
    },
    "triggers": {
        "codename": "Monkeys",
        "label": "Monkeys - Triggers",
        "subtitle": "Fire on events and schedules",
    },
    "dashboard-shell": {
        "codename": "The City",
        "label": "The City - Dashboard",
        "subtitle": "See everything, in one place",
    },
    "vector_store": {
        "codename": "Cyclone",
        "label": "Cyclone - RAG",
        "subtitle": "Feed documents to your assistant",
    },
    "deploy": {
        "codename": "Silver",
        "label": "Silver - Deploy",
        "subtitle": "Ship MOD to a host",
    },
    "stacks": {
        "codename": "Emeralds",
        "label": "Emeralds - Stacks",
        "subtitle": "Run models on your hardware",
    },
    "workers": {
        "codename": "Quadlings",
        "label": "Quadlings - Workers",
        "subtitle": "Run jobs on your machines",
    },
    "agents": {
        "codename": "Scarecrow",
        "label": "Scarecrow - Agents",
        "subtitle": "Autonomous helpers on your tasks",
    },
    "managers": {
        "codename": "Ozma",
        "label": "Ozma - Manager",
        "subtitle": "Manage zones and loans",
    },
    "scheduler": {
        "codename": "Clockwork Scheduler",
        "label": "Clockwork - Scheduler",
        "subtitle": "Run jobs on a schedule",
    },
    "burst-accounts": {
        "codename": "Balloon Cloud",
        "label": "Balloon Cloud - Burst Accounts",
        "subtitle": "Burst jobs to your own cloud account",
    },
    "lockbox": {
        "codename": "The Belt",
        "label": "The Belt - Secrets",
        "subtitle": "Keep secrets safe",
    },
    "admin-sso": {
        "codename": "The Gate",
        "label": "The Gate - Login",
        "subtitle": "Sign in, join the zone",
        "paidOnly": True,
    },
    "node-builder": {
        "codename": "Tinker",
        "label": "Tinker - Node Builder",
        "subtitle": "Build nodes from nothing",
    },
    "files": {
        "codename": "The Cellar",
        "label": "The Cellar - Files",
        "subtitle": "Store files and models",
    },
    "sleep-back": {
        "codename": "Poppies",
        "label": "Poppies - Sleep-Back",
        "subtitle": "Models rest, wake fast",
    },
    "security": {
        "codename": "The Guard (Omby Amby)",
        "label": "The Guard - Security",
        "subtitle": "Watch for trouble, prove clean",
    },
    "image-mover": {
        "codename": "Sawhorse",
        "label": "Sawhorse - Mover",
        "subtitle": "Move images and models between hosts",
    },
    "admin-nodes": {
        "codename": "Tinker",
        "label": "Tinker - Catalog",
        "subtitle": "Price and package the nodes",
    },
    "docker-registry": {
        "codename": "Sawhorse",
        "label": "Sawhorse - Registry",
        "subtitle": "Where your images come from",
    },
    "payments-admin": {
        "codename": "The Mint",
        "label": "The Mint - Payments",
        "subtitle": "Take money, see payouts",
        "paidOnly": True,
    },
    "promo-codes": {
        "codename": "Lucky Charm",
        "label": "Lucky Charm - Promo Codes",
        "subtitle": "Discount codes",
        "paidOnly": True,
    },
    "free-trials": {
        "codename": "First Steps",
        "label": "First Steps - Free Trials",
        "subtitle": "Try before you pay",
        "paidOnly": True,
    },
    "geographic-pricing": {
        "codename": "Kingdom Map",
        "label": "Kingdom Map - Geographic Pricing",
        "subtitle": "Price by country",
        "paidOnly": True,
    },
    "seasonal-pricing": {
        "codename": "Harvest Moon",
        "label": "Harvest Moon - Seasonal Pricing",
        "subtitle": "Time-limited prices",
        "paidOnly": True,
    },
    "webhooks": {
        "codename": "Message Bell",
        "label": "Message Bell - Webhooks",
        "subtitle": "Send events to other systems",
    },
    "drift-events": {
        "codename": "Crystal Ball",
        "label": "Crystal Ball - Drift Events",
        "subtitle": "Things that changed from the plan",
        "paidOnly": True,
    },
    "settings": {
        "codename": "Gearworks",
        "label": "Gearworks - Settings",
        "subtitle": "System settings",
    },
    "help": {
        "codename": "Signpost",
        "label": "Signpost - Help",
        "subtitle": "Help pages",
    },
    "storage": {
        "codename": "Granary",
        "label": "Granary - Storage",
        "subtitle": "Storage connections",
    },
    "counting-house": {
        "codename": "Counting House",
        "label": "Counting House - Plans and Spending",
        "subtitle": "plans, pricing and spending",
        "paidOnly": True,
    },
    # OZ-D1-NAMES 13 a-d pages named by owner 11-15 (mirror of product-names.js)
    "abuse-queue": {
        "codename": "The Guard",
        "label": "The Guard - Abuse Queue",
        "subtitle": "Review reported abuse",
    },
    "access-reviews": {
        "codename": "The Guard",
        "label": "The Guard - Access Reviews",
        "subtitle": "Check who can get in",
        "paidOnly": True,
    },
    "account": {
        "codename": "Dorothy",
        "label": "Dorothy - Account",
        "subtitle": "Your own account",
    },
    "admin-dsar-requests": {
        "codename": "Lion",
        "label": "Lion - Data Subject Requests",
        "subtitle": "Handle data-subject requests",
        "paidOnly": True,
    },
    "admin-plans": {
        "codename": "The Mint",
        "label": "The Mint - Plans",
        "subtitle": "Set up the plans",
        "paidOnly": True,
    },
    "analytics": {
        "codename": "The City",
        "label": "The City - Analytics",
        "subtitle": "See how the city is running",
    },
    "auditor-access": {
        "codename": "Lion",
        "label": "Lion - Auditor Access",
        "subtitle": "Let auditors look in",
        "paidOnly": True,
    },
    "billing": {
        "codename": "The Mint",
        "label": "The Mint - Billing",
        "subtitle": "Billing and receipts",
        "paidOnly": True,
    },
    "business": {
        "codename": "Counting House",
        "label": "Counting House - Business",
        "subtitle": "The business tier",
        "paidOnly": True,
    },
    "changelog": {
        "codename": "Signpost",
        "label": "Signpost - News",
        "subtitle": "What is new",
    },
    "coupons": {
        "codename": "Lucky Charm",
        "label": "Lucky Charm - Coupons",
        "subtitle": "Discount coupons",
        "paidOnly": True,
    },
    "custom-plans": {
        "codename": "The Mint",
        "label": "The Mint - Custom Plans",
        "subtitle": "Plans made just for one customer",
        "paidOnly": True,
    },
    "data-requests": {
        "codename": "The Guard",
        "label": "The Guard - Data Requests",
        "subtitle": "Ask for your data",
    },
    # OZ-D1 keys approved by the owner 11-15 (mirror of product-names.js)
    "active-sessions": {
        "codename": "The Gate",
        "label": "The Gate - Sessions",
        "subtitle": "See who is signed in right now",
    },
    "admin-scim": {
        "codename": "The Gate",
        "label": "The Gate - Activity",
        "subtitle": "Sync the people list into the zone",
    },
    "ai-inventory": {
        "codename": "Emeralds",
        "label": "Emeralds - Inventory",
        "subtitle": "Models running on your hardware",
    },
    "alert-rules": {
        "codename": "The Guard",
        "label": "The Guard - Alert Rules",
        "subtitle": "Fire when trouble shows up",
    },
    "audit-logs": {
        "codename": "The Guard",
        "label": "The Guard - Audit Logs",
        "subtitle": "Prove who changed what",
    },
    "bulk-permissions": {
        "codename": "The Guard",
        "label": "The Guard - Bulk Permissions",
        "subtitle": "Change access for many at once",
    },
    "home": {
        "codename": "O.Z.",
        "label": "O.Z. - Home",
        "subtitle": "Start here: the overview",
    },
    "system": {
        "codename": "The City",
        "label": "The City - System",
        "subtitle": "Watch how the system is running",
    },
    "logs": {
        "codename": "Records",
        "label": "Records - Logs",
        "subtitle": "Read what happened and when",
    },
    "users": {
        "codename": "Citizens",
        "label": "Citizens - Users",
        "subtitle": "Add and manage people",
    },
    "permissions": {
        "codename": "The Guard",
        "label": "The Guard - Permissions",
        "subtitle": "Decide who can do what",
    },
    # Sub-tabs (page inner tabs), owner 11-17: label = '<parent codename> - <sub-tab>'.
    # Gearworks, Signpost, Citizens, Records are Tim's codenames; The Guard
    # (Permissions) is PROPOSED. Key = '<page>-tab-<tab>'.
    "settings-tab-integrations": {"codename": "Gearworks", "label": "Gearworks - Integrations", "subtitle": "Connect email, mobile and outside services"},
    "settings-tab-secrets": {"codename": "Gearworks", "label": "Gearworks - Secrets", "subtitle": "Store passwords and tokens safely"},
    "settings-tab-permission-settings": {"codename": "Gearworks", "label": "Gearworks - Permission Settings", "subtitle": "Set the default sharing rules"},
    "settings-tab-api-keys": {"codename": "Gearworks", "label": "Gearworks - API Keys", "subtitle": "Create keys for programs to use"},
    "settings-tab-ai-configuration": {"codename": "Gearworks", "label": "Gearworks - AI Configuration", "subtitle": "Choose how the AI assistant works"},
    "settings-tab-knowledge-base": {"codename": "Gearworks", "label": "Gearworks - Knowledge Base", "subtitle": "Documents the assistant can read"},
    "settings-tab-stack-tls": {"codename": "Gearworks", "label": "Gearworks - Stack TLS", "subtitle": "Certificates for your stacks"},
    "help-tab-cards": {"codename": "Signpost", "label": "Signpost - Cards", "subtitle": "Browse help topics as cards"},
    "help-tab-mind-map": {"codename": "Signpost", "label": "Signpost - Mind Map", "subtitle": "See how the platform fits together"},
    "help-tab-ai-in-platform": {"codename": "Signpost", "label": "Signpost - AI in this Platform", "subtitle": "Where AI is used on this platform"},
    "help-card-getting-started": {"codename": "Signpost", "label": "Signpost - Getting Started Guide", "subtitle": "Follow the 5-minute first-run guide"},
    "help-card-nodes": {"codename": "Signpost", "label": "Signpost - Node Library", "subtitle": "Browse every node you can use"},
    "help-card-api-reference": {"codename": "Signpost", "label": "Signpost - API Reference", "subtitle": "Look up the platform API"},
    "help-card-worker-setup": {"codename": "Signpost", "label": "Signpost - Worker Setup", "subtitle": "Install and connect a worker"},
    "help-card-troubleshooting": {"codename": "Signpost", "label": "Signpost - Troubleshooting", "subtitle": "Fix common problems"},
    "help-card-full-docs": {"codename": "Signpost", "label": "Signpost - Full Documentation Site", "subtitle": "Open the full documentation site"},
    "help-link-open-docs": {"codename": "Signpost", "label": "Signpost - Open docs", "subtitle": "Open the docs page for the selected topic"},
    "users-tab-users": {"codename": "Citizens", "label": "Citizens - Users", "subtitle": "The people on your platform"},
    "users-tab-invitations": {"codename": "Citizens", "label": "Citizens - Invitations", "subtitle": "Invite new people to join"},
    "permissions-tab-data": {"codename": "The Guard", "label": "The Guard - Data", "subtitle": "Who can use which data"},
    "permissions-tab-secrets": {"codename": "The Guard", "label": "The Guard - Secrets", "subtitle": "Who can use which secrets"},
    "permissions-tab-api-keys": {"codename": "The Guard", "label": "The Guard - API Keys", "subtitle": "Who can use which API keys"},
    "permissions-tab-templates": {"codename": "The Guard", "label": "The Guard - Templates", "subtitle": "Reusable sharing rules"},
    "logs-tab-workflow-runs": {"codename": "Records", "label": "Records - Workflow Runs", "subtitle": "What your workflows did"},
    "logs-tab-system-tasks": {"codename": "Records", "label": "Records - System Tasks", "subtitle": "What the platform did on its own"},
    "system-tab-system-health": {"codename": "The City", "label": "The City - System Health", "subtitle": "See whether the platform is healthy"},
    "system-tab-external": {"codename": "The City", "label": "The City - External", "subtitle": "Load on your outside render farms"},
    "system-tab-settings": {"codename": "The City", "label": "The City - Settings", "subtitle": "Usage, activity and platform settings"},
    "system-tab-usage": {"codename": "The City", "label": "The City - Usage Analytics", "subtitle": "See how much your team uses"},
    "system-tab-activity": {"codename": "The City", "label": "The City - Activity", "subtitle": "Watch what is happening right now"},
    "system-tab-platform-settings": {"codename": "The City", "label": "The City - Platform Settings", "subtitle": "Choose how the platform runs"},
    "system-tab-change-gate": {"codename": "The City", "label": "The City - Change-Gate Report", "subtitle": "Weekly check of model changes"},
}

_TRUTHY_OFF = {"0", "off", "false", "no"}


def theme_enabled(flag: str | None = None) -> bool:
    """MOD_THEME_NAMES feature flag (OZ-A3): default ON in CE and paid alike.

    OFF (env MOD_THEME_NAMES = 0/off/false/no) renders today's literal
    strings byte for byte; ON renders the map label. Unknown key -> today's
    literal (never blank). The map is read at RUN TIME only: keys are never
    hard-coded here, so a concurrent key rename (e.g. `manager` ->
    `managers` by the corrections lane) needs no change to this helper.
    """
    if flag is None:
        import os

        flag = os.environ.get("MOD_THEME_NAMES")
    if flag is None or flag == "":
        return True
    return str(flag).strip().lower() not in _TRUTHY_OFF


def label_for(key: str, today: str, flag: str | None = None) -> str:
    """Themed label for an internal key, or today's literal.

    Flag OFF or unknown key -> `today` byte-for-byte; anchors that do not
    exist yet are simply not themed (no error).
    """
    if not theme_enabled(flag):
        return today
    entry = PRODUCT_NAMES.get(key)
    label = entry.get("label") if entry else None
    return label if label else today


def themed_logger(old_name: str, key: str):
    """Logger named `<codename>.<old_name>` when the theme is ON, else old_name.

    ON keeps the old module tag AFTER the codename (for example
    `The Road - Orchestration.routers.workflows_domains.runs`), so log-line
    greps that match on the old module tag still match (OZ-G3).
    OFF (or unknown key / no label) returns logging.getLogger(old_name),
    identical to today. Never raises.
    """
    import logging

    try:
        if theme_enabled():
            entry = PRODUCT_NAMES.get(key)
            label = entry.get("label") if entry else None
            if label:
                return logging.getLogger(f"{label}.{old_name}")
    except Exception:
        pass
    return logging.getLogger(old_name)
