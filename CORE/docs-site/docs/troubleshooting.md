---
sidebar_position: 7
title: Troubleshooting
description: Common issues + how to fix them. Worker not picking up tasks, slow runs, email failures, login loops, more.
---

# Troubleshooting

## Common Issues

### "Email verification required" on login
Your platform admin needs to configure SMTP first (Gearworks - Settings > Gearworks - Integrations), then enable email verification. If you're the admin and stuck, the verification toggle is off by default. (Community Edition has no signup page; the seeded admin signs in directly.)

### Worker shows "Not profiled" or "Profile failed"
The hardware benchmark container may not be in your registry. CE ships no build script for it: the platform's node builder builds it on demand (the compose `builder` profile and the `buildkitd` services). Re-run the node builder for the benchmark node and let the worker pick the image up.

### Workflow fails with "scan status is rejected"
Containers are security-scanned. A rejected node has critical CVE vulnerabilities. Check the scan report in the Workflow Builder edit modal and update the container image. If the finding looks wrong, or you have found a vulnerability in the platform itself, report it per the vulnerability disclosure policy in `SECURITY.md` at the root of this repository.

### Worker not appearing in Dashboard
Remote workers must go through the enrollment flow (Add Worker button). Workers that connect without enrollment are rejected. Check if the enrollment token is valid and not expired.

### "0s" completion time in System Health
Tasks completed without calling `report_started`. This is fixed in newer worker versions — update your worker binary.

### Files won't upload — storage full
Community Edition has no storage limit, so an upload failure is not a quota. The disk or bucket you provided is full. Free space on that volume, or delete unused files in Dashboard > Data. (Plans and Billing are not in Community Edition.)

### Emails aren't being delivered (signup verification, invites, receipts)
Platform email goes through whatever SMTP provider is saved in
**Settings → Integrations → Email (SMTP)**. Things to check:

1. **Provider configured?** The page's "Send Test Email" button is the
   fastest signal — if a test send fails, the same problem affects
   every triggered email. The error returned from Postmark / your
   SMTP host shows up inline.
2. **Templates present?** CE ships exactly six email templates
   in the `api` image: `permission_expiration_admin_summary`,
   `permission_expiration_granter_notice`, `permission_expiration_warning`,
   `permission_request_approved`, `permission_request_denied`,
   `permission_request_new`. The welcome, payment-received, usage-alert and
   scheduler templates are not shipped, so those Path-A sends audit
   `EMAIL_TEMPLATE_MISSING` — expected on CE, not a host problem.
3. **Audit log signal.** `EMAIL_TEMPLATE_MISSING` events in
   Dashboard → Logs → Audit log are the regression flag — every missed
   send writes one. Zero entries means the pipeline is healthy.
4. **Bounces.** Postmark bounce notifications are not received in CE:
   bounce tracking is not part of this edition and is not shipped, so CE
   neither receives `POST /webhooks/postmark/bounce` nor writes
   `POSTMARK_BOUNCE_RECEIVED` audit rows. On CE, check your SMTP provider's own bounce reports.

### Dark mode didn't follow me to another MOD surface
Light/dark choice is stored in a parent-domain cookie
(`mod_theme`) plus per-app localStorage. If a subdomain isn't
flipping with the rest, the cookie probably didn't make it across
because the browser is treating the subdomains as cross-site
(e.g. third-party-cookie blocking, or you're on `localhost` where
parent-domain cookies don't apply). Re-toggle the theme on the
subdomain that's stuck.

## Diagnosing over the API

When a run misbehaves, the API gives you the raw status without the UI in the way:

```bash
BASE=http://localhost:8000

# Exchange your sk_ key for a Bearer token (see /api-reference#authentication)
TOKEN=$(curl -s -X POST "$BASE/v1/api-keys/token" -H "Content-Type: application/json" \
  -d '{"api_key": "sk_your_key_here"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# Why is a run stuck / what failed? -> status + error detail
curl -s "$BASE/v1/road/workflow-runs/<run_id>" -H "$AUTH" | jq '{status, error_message}'

# Live progress detail for a running job
curl -s "$BASE/v1/road/workflow-runs/<run_id>/progress" -H "$AUTH"

# Cancel a wedged run
curl -s -X POST "$BASE/v1/road/workflow-runs/<run_id>/cancel" -H "$AUTH"

# Is the node registered / which hardware variants built?
curl -s "$BASE/v1/nodes/" -H "$AUTH" | jq '.[] | {name, gpu_variants}'

# Confirm you're authenticated as who you think you are
curl -s "$BASE/v1/me" -H "$AUTH" | jq '{email, tenant_id}'
```

The Swagger explorer at `$BASE/docs` lets you run these (and authorize once) from the browser. It is served only when the platform setting `api.public_docs_enabled` is on (default off).

## Getting Help

Run every `docker compose` command below from the checkout root (the directory with `docker-compose.yml` and `.env`).

- **API Docs**: `https://your-api-url/docs` (Swagger UI)
- **Status**: Community Edition runs on your own host, so there is no hosted status page. Check `docker compose ps` for the state of each service.
