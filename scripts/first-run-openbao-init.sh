#!/bin/sh
# =============================================================================
# first-run-openbao-init.sh — CE one-shot OpenBao bootstrap.
#
# Runs as the `openbao-init` compose service, after `openbao: service_healthy`.
# Single-key init/
# unseal (secret_shares=1, secret_threshold=1 — CE default, NOT Shamir
# production), KV v2 at secret/, one core-api AppRole role plus the break-glass
# AppRole, the database engine + an api-readwrite role, and the transit mount
# (no key creation). No Vault Agent / token-rotation sidecar — out of scope for CE.
#
# Idempotent: re-running after first boot does not re-init and does not
# rotate the unseal key. Never prints secret material (unseal key, root
# token, AppRole secret-id) to stdout/stderr.
# =============================================================================
set -eu

# POLICY_VERSION: bump whenever any `bao policy write` heredoc changes below.
# Fresh installs write the marker after write_policies; re-runs compare it and
# either skip, or print the operator upgrade message (see the DONE_MARKER branch
# below). A DONE install with no marker counts as version 0. A root token is
# never persisted. 3 -> 4 (break-glass AppRole): added the policy-admin heredoc
# in write_policies (the break-glass AppRole's policy), so DONE installs must
# re-write policies to get it. 4 -> 5:the api-readwrite DB
# role now carries revocation_statements (REASSIGN OWNED BY "<name>" TO the
# owner role, DROP OWNED BY, DROP ROLE IF EXISTS), so an existing install must
# also RE-WRITE THE ROLE, not just the policies — the role write lives in the
# ONE write_db_role() function below, called by the fresh path AND by both
# policy-upgrade branches (operator BAO_TOKEN / break-glass pair), so the two
# paths cannot drift.
# 5 -> 6:policy-admin may CREATE secret/infrastructure/embedded-worker so an
# upgrade seeds the embedded worker token.
# 6 -> 7 (FIX-AICONFIG-REVIEW-CHANGES): core-api now grants platform/ai-config/* —
# the manifest (open-core/manifest.yaml:846-849) SHIPS AI config (BYO LLM) in CE, so
# the old policy left every AI-config read/write 403ing (REV-AICONFIG-APPROLE item 2).
# DONE installs (xeon, Dell) must re-write the policies: the version marker below
# triggers the existing upgrade branches (operator BAO_TOKEN / break-glass pair).
# 7 -> 8 (FIX-OPENBAO-POLICY, from DIAG-CPU3-VAULT verdict POLICY-GAP): (G1) the v7
# core-api grant `secret/data/platform/ai-config/*` uses a single-segment `*`, which
# never matches the BARE parent, so the legacy-migration read of `platform/ai-config`
# (CORE/API/routers/ai_config.py:1912) 403'd on cpu3/gpu5 — added the exact BARE
# paths (data read, metadata read+list) alongside the existing `/*` grant, no
# capability widened. (G2) the break-glass/policy-admin upgrade branch seeds the
# stack-ingress ask_token (seed_ingress_ask_token, :396-412) with its login token,
# whose heredoc had NO `secret/data/infrastructure/ingress` grant, so the kv put was
# 403'd (the WARNING at :412) and the edge token check stayed fail-open — added
# EXACTLY create+read on that ONE path, mirroring the embedded-worker line above it.
# DONE installs must re-apply: the roll runbook upgraded 6 -> 7 via the break-glass
# pair, the same branches now take 7 -> 8.
POLICY_VERSION=8

STATE_DIR="/state"
UNSEAL_KEY_PATH="$STATE_DIR/unseal_key"
ROLE_ID_PATH="$STATE_DIR/approle_role_id"
SECRET_ID_PATH="$STATE_DIR/approle_secret_id"
DONE_MARKER="$STATE_DIR/.bootstrap-done"
POLICY_VERSION_MARKER="$STATE_DIR/.policy-version"
# Uid the readers run as (CORE/API/Dockerfile:222 `USER 10001`,
# CORE/Worker/Dockerfile:222 `USER 10001:10001`) — compose mounts this state
# volume at /run/openbao into api, mod-worker, mod-migrate and ce-node-repair,
# which `cat` the AppRole pair. Both
# the mountpoint dir and the pair files must be reachable by THIS uid: a root
# 700 dir + root:root 0600 files (the failing shape) make every `cat` yield empty
# -> CORE/API/database.py:224 "No OpenBao authentication available".
CE_STATE_OWNER_UID=10001
# The break-glass delivery file lives in the `openbao-breakglass`
# named volume, mounted ONLY into openbao-init at /breakglass (never api/mod-worker).
BREAKGLASS_FILE="/breakglass/breakglass.txt"

export BAO_ADDR="${OPENBAO_ADDR:-http://openbao:8200}"
export VAULT_ADDR="$BAO_ADDR"

(umask 077 && mkdir -p "$STATE_DIR")
chmod 700 "$STATE_DIR"
# the mountpoint dir must belong to the reader uid (mode 700
# keeps it private to that uid alone). Verified, never assumed.
chown "$CE_STATE_OWNER_UID":"$CE_STATE_OWNER_UID" "$STATE_DIR" 2>/dev/null || true
_DIR_OWNER="$(ls -ldn "$STATE_DIR" 2>/dev/null | awk '{print $3}' || true)"
if [ "${_DIR_OWNER:-}" != "$CE_STATE_OWNER_UID" ]; then
    echo "ERROR: the state dir $STATE_DIR is not owned by the reader uid $CE_STATE_OWNER_UID (owner: ${_DIR_OWNER:-unknown}) - every uid-$CE_STATE_OWNER_UID reader would see an empty /run/openbao; aborting. Fix with: sudo chown -R $CE_STATE_OWNER_UID $STATE_DIR (or run the container with the matching uid), then run scripts/first-run-openbao-init.sh again." >&2
    exit 1
fi
unset _DIR_OWNER

_write_secret() {
    # $1=content $2=dest — write under a private umask, force 0600, never echo.
    (umask 077 && printf '%s' "$1" > "$2")
    chmod 600 "$2"
}

# The AppRole pair is read by the uid-10001 readers ONLY
# (compose mounts `openbao-init-state:/run/openbao:ro` into api, mod-worker,
# mod-migrate and ce-node-repair only, which `cat` it into
# VAULT_APPROLE_ROLE_ID/VAULT_APPROLE_SECRET_ID — CORE/API/
# entrypoint.sh:19-22, CORE/API/Dockerfile:222 `USER 10001`). This one-shot runs
# as root (compose `user: "0:0"`, which bypasses the image entrypoint's chown —
# see the compose comment above), so a root:root 0600 file is unreadable to
# every one of them: their `cat` yields empty, and CORE/API/database.py:224
# raises "No OpenBao authentication available (no agent token after 30s, no
# AppRole credentials)" — the failure seen on a fresh CE boot (the state dir
# listed approle_role_id/approle_secret_
# id as `-rw------- root root`). Deliver the pair to the reader uid instead;
# the unseal key, the markers and the break-glass file stay root-only (nothing
# reads them as uid 10001). Ownership is VERIFIED, never assumed: a failed
# chown aborts non-zero with a clear message (no silent skip).
CE_STATE_OWNER_UID=10001

_deliver_secret() {
    # $1=content $2=dest — _write_secret plus delivery to the reader uid.
    _write_secret "$1" "$2"
    # The chown attempt is not the gate - the OWNER CHECK right below is: a run
    # that already owns the file at the reader uid needs no chown, a run that
    # cannot deliver it to that uid fails loud on the verify.
    chown "$CE_STATE_OWNER_UID":"$CE_STATE_OWNER_UID" "$2" 2>/dev/null || true
    _OWNER="$(ls -ln "$2" 2>/dev/null | awk '{print $3}' || true)"
    if [ "$_OWNER" != "$CE_STATE_OWNER_UID" ]; then
        echo "ERROR: $2 is not owned by the reader uid $CE_STATE_OWNER_UID (owner: ${_OWNER:-unknown}) - aborting before the stack starts. Fix with: sudo chown $CE_STATE_OWNER_UID $2, then run scripts/first-run-openbao-init.sh again." >&2
        exit 1
    fi
    unset _OWNER
}

# The ONE place the core-api AppRole pair is written. Called by the fresh-install
# path (root token) AND by the unseal-only path when the pair is missing on a
# DONE install (a prior run marked the bootstrap done without the pair landing,
# or a state volume that kept the marker while the pair never reached the reader
# uid). Needs BAO_TOKEN in the environment. Values never printed.
deliver_approle_pair() {
    _DR="$(bao read -field=role_id auth/approle/role/core-api/role-id || true)"
    _DS="$(bao write -field=secret_id -f auth/approle/role/core-api/secret-id || true)"
    if [ -z "$_DR" ] || [ -z "$_DS" ]; then
        echo "ERROR: failed to read back the core-api AppRole role_id/secret_id (role missing on this install, or the token in use lacks approle read rights) - aborting before delivery. Re-run with an operator token: docker compose run --rm -e BAO_TOKEN openbao-init, or with the break-glass pair: docker compose run --rm -e BAO_BREAKGLASS_ROLE_ID -e BAO_BREAKGLASS_SECRET_ID openbao-init." >&2
        return 1
    fi
    _deliver_secret "$_DR" "$ROLE_ID_PATH"
    _deliver_secret "$_DS" "$SECRET_ID_PATH"
    unset _DR _DS
    echo "core-api AppRole pair delivered (mode 0600, owner uid $CE_STATE_OWNER_UID - the uid the api/worker/migrate readers run as)."
    return 0
}

echo "waiting for OpenBao listener..."
i=0
until bao status >/tmp/status.out 2>&1 || [ "$i" -ge 30 ]; do
    i=$((i + 1))
    sleep 2
done
# `bao status` exits 2 when sealed/uninitialized (not an error for us).
INITIALIZED="$(grep -i '^Initialized' /tmp/status.out 2>/dev/null | awk '{print $2}' || true)"

if [ "$INITIALIZED" != "true" ]; then
    if [ -f "$UNSEAL_KEY_PATH" ]; then
        echo "ERROR: OpenBao reports uninitialized but $UNSEAL_KEY_PATH already exists — refusing to re-init over stale state. Restore the openbao-init-state volume from backup, or, only if its OpenBao secrets may be destroyed, remove it (find the name with: docker volume ls | grep openbao-init-state) and re-run: docker compose run --rm openbao-init." >&2
        exit 1
    fi
    echo "first-run: initializing OpenBao (secret_shares=1, secret_threshold=1)..."
    # `-format=json` is pretty-printed (one field per line, array values on
    # their own line below the key) — not single-line JSON, so extraction is
    # line-based rather than a single regex over the whole blob.
    INIT_OUT="$(bao operator init -key-shares=1 -key-threshold=1 -format=json)"
    UNSEAL_KEY="$(printf '%s\n' "$INIT_OUT" | grep -A1 '"unseal_keys_b64"' | tail -1 | sed 's/^[[:space:]]*"//; s/"[[:space:]]*,\{0,1\}$//')"
    ROOT_TOKEN="$(printf '%s\n' "$INIT_OUT" | sed -n 's/^[[:space:]]*"root_token":[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [ -z "$UNSEAL_KEY" ] || [ -z "$ROOT_TOKEN" ]; then
        echo "ERROR: failed to parse unseal key or root token out of 'bao operator init' output — aborting before writing anything. Re-run: docker compose run --rm openbao-init; if it repeats, check the daemon with: docker compose exec openbao bao status." >&2
        exit 1
    fi
    unset INIT_OUT
    _write_secret "$UNSEAL_KEY" "$UNSEAL_KEY_PATH"
    echo "written"
else
    if [ ! -f "$UNSEAL_KEY_PATH" ]; then
        echo "ERROR: OpenBao is already initialized but no local unseal key file ($UNSEAL_KEY_PATH) was found in the openbao-init-state volume. Cannot auto-unseal. Restore that volume or re-init." >&2
        exit 1
    fi
fi

echo "⚠️  OpenBao auto-unsealed from a local key file. This is fine for local trial use. For any production deployment, rotate to Shamir multi-share manual unseal (raise secret_shares and secret_threshold above 1) or to a cloud KMS auto-unseal seal configured per the OpenBao seal documentation."

SEALED="$(grep -i '^Sealed' /tmp/status.out 2>/dev/null | awk '{print $2}' || echo true)"
if [ "$SEALED" != "false" ]; then
    # NOT stdin: `bao operator unseal` with no key argument PROMPTS — it never reads a
    # piped key (proven live on openbao/openbao:2.6.4: rc=1, "file descriptor 0 is not
    # a terminal", `bao status` stays Sealed true, so every later bao write 403s).
    # Use the raw write API with the key loaded from a 0600 temp file (`key=@file`, the
    # same form proven live for `bao kv put`): the key never reaches exec argv or `ps`,
    # and never sits in a shell variable either.
    _unseal_file="$(umask 077 && mktemp)"
    # The 0600 key file must not survive a signal: the trap removes it on EXIT, and on
    # INT/TERM removes it and exits 1 (fail closed, never falls through to the policy writes).
    trap 'rm -f "$_unseal_file"' EXIT
    trap 'rm -f "$_unseal_file"; exit 1' INT TERM
    printf '%s\n' "$(cat "$UNSEAL_KEY_PATH")" > "$_unseal_file"
    _unseal_rc=0
    bao write sys/unseal key=@"$_unseal_file" >/dev/null 2>&1 || _unseal_rc=$?
    rm -f "$_unseal_file"
    trap - EXIT INT TERM
    if [ "$_unseal_rc" -ne 0 ]; then
        echo "ERROR: auto-unseal FAILED (bao write sys/unseal rc $_unseal_rc) — OpenBao stays sealed and every later bao write gets 403. Restore the openbao-init-state volume or unseal manually." >&2
        # Fail CLOSED: a failed unseal must never report success with OpenBao still sealed,
        # or compose starts the dependants against a sealed OpenBao.
        exit 1
    fi
fi
rm -f /tmp/status.out

# write_policies: the ONE place every `bao policy write` lives. Called by the
# fresh-install path (root token from init) and by the operator upgrade path
# (a policy-write token handed in as BAO_TOKEN). Needs BAO_TOKEN in the
# environment.
write_policies() {
bao policy write core-api - <<'EOF' >/dev/null
path "secret/data/tenants/*"    { capabilities = ["create", "read", "update", "delete", "list"] }
path "secret/metadata/tenants/*" { capabilities = ["read", "list", "delete"] }
path "database/creds/api-readwrite" { capabilities = ["read"] }
path "transit/datakey/plaintext/tenant-*" { capabilities = ["create", "update"] }
path "transit/decrypt/tenant-*"           { capabilities = ["create", "update"] }
path "transit/rewrap/tenant-*"            { capabilities = ["create", "update"] }
path "transit/keys/tenant-*"              { capabilities = ["create", "update"] }
path "sys/mounts"                         { capabilities = ["read"] }
# The API's startup seed (main.py:_startup_seed_embedded_worker_enrollment)
# READS the worker bootstrap token to hash it onto a WorkerEnrollment row, and the
# Go worker reads the same path via GetInfraSecret("embedded-worker").
# Narrower than a blanket `secret/data/infrastructure/*` read grant:
# scoped to the ONE path CE actually uses (never blanket infrastructure/*).
path "secret/data/infrastructure/embedded-worker"    { capabilities = ["read"] }
path "secret/metadata/infrastructure/embedded-worker" { capabilities = ["read", "list"] }
# CE-kept KV paths the core-api policy was missing.
# On rc11 CE, POST /system/email returned 503 because the Vault client hit
# HTTP 403 on secret/data/platform/email-credentials (traced on a CE rc build).
# Exact paths only — no blanket secret/data/platform/* or secret/data/apps/* (S1).
path "secret/data/platform/email-credentials"    { capabilities = ["create", "read", "update"] }
path "secret/metadata/platform/email-credentials" { capabilities = ["read", "list"] }
# Save SMTP writes it (routers/admin_email_config.py), reads it from the same module;
# read side crud/smtp_cud.py:210. No delete grant: no CE-kept code deletes it.
path "secret/data/apps/mod_app_session_hmac"    { capabilities = ["read"] }
path "secret/metadata/apps/mod_app_session_hmac" { capabilities = ["read", "list"] }
# App forward-auth HMAC key: services/app_forward_auth.py:487 (route routers/apps.py:41).
path "secret/data/infrastructure/ingress"    { capabilities = ["read"] }
path "secret/metadata/infrastructure/ingress" { capabilities = ["read", "list"] }
# Stack ingress ask-token read: routers/persistent_stacks/_shared.py:4212.
# Read-only — infrastructure/* grants stay read-only (S3).
# benchmark-hmac/<id> is built dynamically (f-string at routers/worker_management.py:1382),
# so the narrowest workable grant is that prefix, not a wildcard elsewhere:
# write at :1382, read at :1439, delete at :1473 (delete = credential removal,
# not product data, and the code does call it).
path "secret/data/benchmark-hmac/*"    { capabilities = ["create", "read", "update", "delete", "list"] }
path "secret/metadata/benchmark-hmac/*" { capabilities = ["read", "list", "delete"] }
# The per-manager build-task signing keypair. utils/build_task_token.py
# ensure_manager_keypair() (main.py:3791-3792 startup self-heal) reads AND writes
# managers/{MANAGER_ID}/build_task_signing in KV v2. The core-api policy had NO
# managers/* grant, so the mint got HTTP 403 "permission denied" -> the self-heal
# returned "unavailable (fail-safe)" -> is_configured() False -> the node run-token
# mint 503'd and the node container exited 1 (observed in a CE node run).
# Narrow prefix only (never blanket secret/data/*): write in ensure_manager_keypair()
# (build_task_token.py:530), read at :473 and :513. No delete grant — no CE-kept
# code deletes a manager keypair.
path "secret/data/managers/*"    { capabilities = ["create", "read", "update", "list"] }
path "secret/metadata/managers/*" { capabilities = ["read", "list"] }
# FIX-AICONFIG-REVIEW-CHANGES — AI config (BYO LLM) SHIPS in CE
# (open-core/manifest.yaml:846-849 keeps routers/ai_config.py + the AI-config
# readers), so the core-api policy must reach the per-tool Vault slots or every save
# 403s (REV-AICONFIG-APPROLE item 2: token resolved, PUT /platform/ai-config/{tool}
# still 403 -> 500). Narrow prefix only — never blanket secret/data/platform/*.
# Writes: ai_config.py:_write_vault_path (routers/ai_config.py:4177,4298 + the
# managed-state/advanced-stash slots), utils/tool_config.py reads. NO delete grant:
# no CE-kept code deletes an ai-config path (ai_config.py retire path — ISO 27001
# A.18.1.3, `enabled=false` + `retired_at`,
# never a hard delete); delete_kv_v2 in CE touches tenants/*, benchmark-hmac/*,
# platform secrets/log_streams and the erase_* paths only, never platform/ai-config/*.
path "secret/data/platform/ai-config/*"    { capabilities = ["create", "read", "update", "list"] }
path "secret/metadata/platform/ai-config/*" { capabilities = ["read", "list"] }
# FIX-OPENBAO-POLICY (G1, DIAG-CPU3-VAULT): OpenBao's `*` matches exactly ONE path
# segment, so the two lines above cover `platform/ai-config/<tool>` but NEVER the
# BARE parent, and the legacy-migration read `_read_vault_path("platform/ai-config")`
# (CORE/API/routers/ai_config.py:1912 and CORE/API/utils/tool_config.py:87) got HTTP 403
# permission denied on cpu3 and gpu5
# (live probe: bare path 403, `platform/ai-config/wizard` write OK). Added the EXACT
# BARE paths only — capabilities a SUBSET of the `/*` grant above: READ on the data
# path (the only CE-kept access to the bare parent is the legacy/migration READ —
# every write goes to `platform/ai-config/{tool}` via _vault_path_for(), ai_config.py:1781,
# which the `/*` grant already covers), read+list on metadata (the exact-path style this
# file already uses at :194/:200/:204/:207). No create/update/delete on the bare parent,
# no new wildcard, no extra nesting level. The 404s on wizard/_managed_ai_setting/assistant
# are NOT a defect (slots created on first successful PUT) — untouched.
path "secret/data/platform/ai-config"    { capabilities = ["read"] }
path "secret/metadata/platform/ai-config" { capabilities = ["read", "list"] }
# Denied families stay ungranted (see the manifest deny list).
# (platform/ai-config/* moved from denied to granted above — owner decision: AI
# config / BYO LLM ships in CE.)
EOF
bao policy write policy-admin - <<'EOF' >/dev/null
# The break-glass AppRole's policy: rewrite ACL policies
# ONLY, plus revoke-self so the upgrade run can revoke its own login token
# (token_no_default_policy=true drops default's revoke-self). No delete, no
# other auth/*, no sys/*, no secret/*, no sys/audit*, no sys/mounts*.
path "sys/policies/acl/*" { capabilities = ["create", "update", "read"] }
path "sys/policies/acl" { capabilities = ["list"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
# the upgrade branch also re-writes the DB role (write_db_role),
# so the break-glass login token needs EXACTLY these two paths — the engine config
# and the one role — nothing else (no database/creds/*: minting stays core-api's).
path "database/config/app-db" { capabilities = ["create","update"] }
path "database/roles/api-readwrite" { capabilities = ["create","update"] }
# the break-glass login token must be able to seed a MISSING embedded-worker
# bootstrap token on an upgrade run. EXACTLY create+read on the ONE path (no
# wildcard, no update/delete/list) — create-only means it can never overwrite or
# rotate an existing token, so the keep-if-present guard stays honest.
path "secret/data/infrastructure/embedded-worker" { capabilities = ["create", "read"] }
# FIX-OPENBAO-POLICY (G2, DIAG-CPU3-VAULT Q4): seed_ingress_ask_token() (:396)
# runs on BOTH upgrade branches (:525 operator BAO_TOKEN, :551 break-glass login
# token) and does `bao kv get` then `bao kv put secret/infrastructure/ingress
# ask_token=...`. On the break-glass branch the token in play is the policy-admin
# AppRole login (:505 `bao write -field=token auth/approle/login`), whose policy had
# NO ingress grant at all — the core-api heredoc only grants ingress READ (:207-208),
# which is why the slot READS fine on cpu3 while the UPGRADE seed was 403'd and the
# run printed the WARNING at :412 (edge token check stayed fail-open). The FRESH
# branch seeds it with the ROOT token (no policy restriction), which is why cpu3 has
# the slot anyway. Same shape as the embedded-worker line above: EXACTLY create+read
# on the ONE path (no wildcard, no update/delete/list) — create-only can never
# overwrite or rotate an existing ask_token, so the keep-if-present guard stays
# honest.
path "secret/data/infrastructure/ingress" { capabilities = ["create", "read"] }
EOF
}

# The ACL-policy
# registry does NOT survive the Vault-1.15 -> OpenBao data copy — only the builtins
# (`default`) remain — while the AppRole ROLE still lists its `token_policies`, so
# AppRole login 500s until the policies are re-written. The POLICY_VERSION marker
# alone cannot see that: the marker lives in the `openbao-init-state` volume, the
# policy registry lives in the DATA volume. So the policy write is ONE idempotent
# step, ensure_policies(), guarded on `bao policy list`: it runs on EVERY unsealed
# run of this script (fresh install AND an already-initialized/post-cutover DONE
# install — both call sites below are after the unseal block), writes ONLY through
# write_policies() above (the same names + heredocs the first init writes — nothing
# is invented here), and prints a no-op when the registry already holds them.
# Names only are ever printed; no token material. Never blocks stack start.
EXPECTED_POLICIES="core-api policy-admin"

missing_policy_names() {
    # Echoes the expected policy names ABSENT from `bao policy list` (space-
    # separated), or the literal UNREADABLE when the list call itself fails, so
    # the caller can tell "all present" from "cannot tell". Policy NAMES only.
    # `bao policy list` prints a table (name, then a `|`-separated type column on
    # some builds). Split on `|` AND whitespace so the name is a line of its own
    # either way; header/separator junk stays harmless (the match is exact).
    _MPN_LIST="$(bao policy list 2>/dev/null | tr '|' '\n' | tr -d ' \t' | grep -v '^$' || true)"
    if [ -z "$_MPN_LIST" ]; then
        printf 'UNREADABLE\n'
        return 0
    fi
    _MPN_MISSING=""
    for _mpn in $EXPECTED_POLICIES; do
        printf '%s\n' "$_MPN_LIST" | grep -q "^${_mpn}$" || _MPN_MISSING="$_MPN_MISSING $_mpn"
    done
    printf '%s\n' "$_MPN_MISSING"
    return 0
}

# $1 = caller label, $2 = "force" (fresh-install path: write even when the
# registry cannot be listed — `bao policy write` is an overwrite, so a re-run
# stays a no-op). Sets POLICY_GUARD_MISSING to whatever is still missing after
# the step (empty = registry complete / guard skipped).
ensure_policies() {
    POLICY_GUARD_MISSING=""
    _EP_LABEL="${1:-policy step}"
    _EP_FORCE="${2:-}"
    _EP_MISSING="$(missing_policy_names)"
    if [ "$_EP_MISSING" = "UNREADABLE" ]; then
        if [ "$_EP_FORCE" = "force" ] && [ -n "${BAO_TOKEN:-}" ]; then
            echo "openbao-init: policy-list guard ($_EP_LABEL): registry cannot be listed — fresh install, writing policies unconditionally."
            if ! write_policies; then
                echo "openbao-init: policy-list guard ($_EP_LABEL): unconditional policy write FAILED (a fresh install aborts below; an already-initialized run is not blocked)." >&2
                POLICY_GUARD_MISSING=" $EXPECTED_POLICIES"
            fi
        else
            echo "openbao-init: policy-list guard ($_EP_LABEL): registry cannot be listed on this run — guard skipped; the version-marker decision still applies."
        fi
        unset _EP_LABEL _EP_FORCE _EP_MISSING
        return 0
    fi
    if [ -z "$_EP_MISSING" ]; then
        echo "openbao-init: policy-list guard ($_EP_LABEL): core-api + policy-admin both registered — policy write is a no-op."
        unset _EP_LABEL _EP_FORCE _EP_MISSING
        return 0
    fi
    if [ -z "${BAO_TOKEN:-}" ]; then
        echo "openbao-init: policy-list guard ($_EP_LABEL): missing policies:$_EP_MISSING and no token on this run — not written (stack start not blocked)." >&2
        POLICY_GUARD_MISSING="$_EP_MISSING"
        unset _EP_LABEL _EP_FORCE _EP_MISSING
        return 0
    fi
    echo "openbao-init: policy-list guard ($_EP_LABEL): re-writing missing policies:$_EP_MISSING (same names + heredocs as first init) ..."
    write_policies || echo "openbao-init: policy-list guard ($_EP_LABEL): policy write FAILED (stack start not blocked). Re-run: docker compose run --rm openbao-init, or list the policies with: docker compose exec openbao bao policy list." >&2
    _EP_AGAIN="$(missing_policy_names)"
    if [ -z "$_EP_AGAIN" ]; then
        echo "openbao-init: policy-list guard ($_EP_LABEL): registry now complete — a further run of this script is a no-op."
    elif [ "$_EP_AGAIN" != UNREADABLE ]; then
        echo "openbao-init: policy-list guard ($_EP_LABEL): still missing after the write:$_EP_AGAIN (stack start not blocked)." >&2
        POLICY_GUARD_MISSING="$_EP_AGAIN"
    fi
    unset _EP_LABEL _EP_FORCE _EP_MISSING _EP_AGAIN
    return 0
}

# The ONE place the stack-ingress ask_token slot is seeded.
# Needs a usable BAO_TOKEN in the env (root token on the fresh path, the
# operator token or the break-glass AppRole login token on a DONE install).
# Keep-if-present (rotating would orphan the edge the stack already configured);
# generates a token when MOD_INGRESS_ASK_TOKEN is unset so an upgraded box gets
# the slot at all (edge_ready in c2-J2). The value is NEVER echoed, logged or
# written to a file — it goes through argv only, in the existing `kv put key=value`
# shape. Never aborts `set -eu`: every failure path warns on stderr and returns 0,
# so the stack start is never blocked.
seed_ingress_ask_token() {
    if bao kv get secret/infrastructure/ingress >/dev/null 2>&1; then
        echo "infrastructure/ingress already present — keeping existing ask_token."
        return 0
    fi
    _seed_tok="${MOD_INGRESS_ASK_TOKEN:-}"
    if [ -n "$_seed_tok" ]; then
        _seed_src="from env"
    else
        # Same generator shape as the other random-token helpers in this script.
        _seed_tok="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-43)"
        _seed_src="generated"
    fi
    # value goes through a 0600 temp file (`key=@file`), never argv (`ps`, exec argv).
    _seed_file="$(umask 077 && mktemp)"
    printf '%s' "$_seed_tok" > "$_seed_file"
    _seed_rc=0
    bao kv put secret/infrastructure/ingress ask_token="@$_seed_file" >/dev/null 2>&1 || _seed_rc=$?
    rm -f "$_seed_file"
    unset _seed_file
    if [ "$_seed_rc" -eq 0 ]; then
        echo "infrastructure/ingress ask_token written ($_seed_src)."
    else
        echo "WARNING: seeding infrastructure/ingress ask_token FAILED (kv put rejected or mount missing) — edge token check stays fail-open; stack start not blocked. Check the kv mount with: docker compose exec openbao bao ls secret/infrastructure/ingress, then re-run: docker compose run --rm -e BAO_TOKEN openbao-init." >&2
    fi
    unset _seed_tok _seed_src
    return 0
}

# The ONE place the embedded-worker bootstrap token slot is seeded.
# The fresh path used to be the ONLY writer, so an install whose DONE marker came
# from a previous CE image never got the slot: main.py's startup seed skips
# ("Embedded worker enrollment seed skipped"), no APPROVED enrollment row exists
# for mod-worker-embedded, and worker_tasks.py's require_enrolled_worker 401s while
# worker.embedded_requires_secret stays True (the default — auth is NOT weakened).
# Needs a usable BAO_TOKEN (root on the fresh path, the operator token or the
# break-glass AppRole login token on a DONE install; the policy-admin policy now
# CREATEs this path since POLICY_VERSION 6 (current: 8)). Keep-if-present: rotating would orphan the
# enrollment hash the seed already wrote. Same kv get/put sugar as the ingress
# seed (the raw read/write API does not translate kv-v2 data/ paths). The value
# goes through argv only — never echoed, logged, or written to a file.
seed_embedded_worker_token() {
    local _rc _err
    # Measured on the pinned OpenBao 2.6.4 image (probe run 2026-11-05; the stderr-match gate is pinned by open-core/tests/test_openbao_policy_branches.py):
    # `bao kv get` exits 2 when the key is MISSING (stderr first line "No value found at
    # secret/data/<key>"), and ALSO exits 2 for a 403 permission denial (stderr carries
    # "Code: 403" / "permission denied") and for an unreachable server ("connection refused").
    # Exit 1 never happens for these, so the exit code alone cannot separate not-found from a
    # permission/transport failure: seed ONLY on exit 2 whose stderr carries the literal text
    # "No value found". Anything else (other exit codes, exit 2 with any other text, 127) fails
    # closed. stdout goes to /dev/null so a present secret value is never held in a variable or
    # printed; stderr is kept for the literal-text check only and is NEVER printed (an API error
    # body could carry secret material).
    _rc=0
    _err="$(bao kv get secret/infrastructure/embedded-worker 2>&1 >/dev/null)" || _rc=$?
    if [ "$_rc" = 0 ]; then
        echo "infrastructure/embedded-worker already present — keeping existing token."
        unset _err _rc
        return 0
    fi
    if [ "$_rc" != 2 ] || ! printf '%s\n' "$_err" | grep -q 'No value found'; then
        echo "openbao-init: cannot verify the embedded-worker slot (kv get exit $_rc) — not seeding; check the OpenBao connection and the token's read permission on secret/data/infrastructure/embedded-worker, then re-run." >&2
        unset _err _rc
        return 1
    fi
    unset _err _rc
    # 64 hex chars (32 random bytes, hex-encoded).
    _emb_tok="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    # value goes through a 0600 temp file (`key=@file`), never argv (`ps`, exec argv).
    _emb_file="$(umask 077 && mktemp)"
    printf '%s' "$_emb_tok" > "$_emb_file"
    _emb_rc=0
    bao kv put secret/infrastructure/embedded-worker bootstrap_token="@$_emb_file" >/dev/null 2>&1 || _emb_rc=$?
    rm -f "$_emb_file"
    unset _emb_file
    if [ "$_emb_rc" -eq 0 ]; then
        unset _emb_tok
        echo "infrastructure/embedded-worker bootstrap_token written (generated). Restart the api and mod-worker so the enrollment seed and the worker pick it up."
    else
        unset _emb_tok
        echo "WARNING: seeding infrastructure/embedded-worker FAILED (kv put rejected or mount missing) — the embedded worker stays refused (401 'Worker bootstrap token required') until it is seeded; on a fresh install this ABORTS the bootstrap (openbao-init exits 1, stack start IS blocked; on an upgrade re-run the seed failure is non-fatal) — next step: fix the mount/token, then re-run docker compose run --rm -e BAO_TOKEN openbao-init." >&2
        return 1
    fi
}

# the ONE place the database engine + the api-readwrite role
# are written. The fresh path calls it with the root token; BOTH policy-upgrade
# branches (operator BAO_TOKEN / break-glass pair) call it too, so an existing
# install picks up the new revocation_statements without drift. Carries
# the same revocation trio as every other copy of this bootstrap, so the copies
# cannot drift. WHY: CE's role is only `IN ROLE "<POSTGRES_USER>"` — membership in the
# owner role confers rights on OWNER-role tables but NEVER ownership of tables
# left behind by an expired sibling dyn role (BUG-1028b: 345 tables stayed
# owned by an expired role -> `permission denied for table ...` forever).
# revocation_statements run when a lease expires/is revoked: REASSIGN hands any
# still-owned objects to the owner role, DROP OWNED clears the rest of its
# grants, DROP ROLE removes the role itself (no more 340-role pile-up).
# NO GRANT ... ON ALL TABLES / ALTER DEFAULT PRIVILEGES here: CE's grant model
# is membership in the OWNER role (cr12 proves that works once tables are
# owned by core); the SaaS grant set stays SaaS-only.
# `bao write` takes key=value pairs; a value starting with @ is loaded from a
# file (`bao write -h`: "If the value begins with an \"@\", then it is loaded
# from a file"), so revocation_statements is a JSON array in a temp file —
# statically guarded by the open-core/tests check test_d1_revocation_statements_trio (it inspects this statement list; it does not run OpenBao or Postgres).
write_db_role() {
    _DB_REV_FILE="$(umask 077 && mktemp)"
    _DB_PW_FILE="$(umask 077 && mktemp)"
    printf '%s' "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}" > "$_DB_PW_FILE"
    printf '%s' '["REASSIGN OWNED BY \"{{name}}\" TO \"'"${POSTGRES_USER:-core}"'\"", "DROP OWNED BY \"{{name}}\"", "DROP ROLE IF EXISTS \"{{name}}\""]' > "$_DB_REV_FILE"
    bao write database/config/app-db \
        plugin_name=postgresql-database-plugin \
        connection_url="postgresql://{{username}}:{{password}}@${POSTGRES_HOST:-postgres-app}:${POSTGRES_PORT:-5432}/${POSTGRES_DB:-modcore}?sslmode=disable" \
        allowed_roles="api-readwrite" \
        username="${POSTGRES_USER:-core}" \
        password="@$_DB_PW_FILE" >/dev/null
    bao write database/roles/api-readwrite \
        db_name=app-db \
        creation_statements='CREATE ROLE "{{name}}" WITH LOGIN PASSWORD '"'"'{{password}}'"'"' VALID UNTIL '"'"'{{expiration}}'"'"' IN ROLE "'"${POSTGRES_USER:-core}"'";' \
        revocation_statements="@$_DB_REV_FILE" default_ttl=1h max_ttl=24h >/dev/null
    rm -f "$_DB_REV_FILE" "$_DB_PW_FILE"
}

if [ -f "$DONE_MARKER" ]; then
    echo "openbao-init: bootstrap already completed on a prior run — skipping engine/setup (unseal-only run)."
    # A missing marker on a DONE install = version 0 (pre-marker install) → upgrade.
    STORED_VERSION="$(cat "$POLICY_VERSION_MARKER" 2>/dev/null || true)"
    case "$STORED_VERSION" in ''|*[!0-9]*) STORED_VERSION=0 ;; esac
    # The up-to-date and the upgrade paths share the token branches
    # below, so an up-to-date run can still seed the ingress ask_token slot
    # (policies + the version marker stay untouched when NEEDS_POLICY_UPGRADE=0).
    NEEDS_POLICY_UPGRADE=0
    # P6: the marker is NOT the only trigger — run the policy-list guard
    # on EVERY unsealed run, so a post-cutover install (marker still says 5, the
    # DATA volume lost the ACL registry on the 1.15 -> OpenBao copy) is detected
    # here instead of waiting for the next POLICY_VERSION bump. POLICY_GUARD_MISSING
    # is what the token branches below act on.
    ensure_policies "already-initialized run"
    # a DONE marker is NOT proof the AppRole pair is on disk
    # for the readers. If either file is missing/empty, the pair must be RE-ISSUED
    # through the SAME authenticated shape the fresh path uses (below, in the
    # token branches) - never silently skipped: without it every uid-10001 reader
    # starts with empty VAULT_APPROLE_* and my_ce_migrate exits 1.
    APPROLE_PAIR_MISSING=0
    # Missing/empty, OR present but NOT owned by the reader uid (the failing shape:
    # a prior root run left root:root 0600 files every uid-10001 reader `cat`s
    # empty) - either way the pair must be re-delivered, never silently kept.
    _PAIR_OWNER="$(ls -ln "$ROLE_ID_PATH" 2>/dev/null | awk '{print $3}' || true)"
    if [ ! -s "$ROLE_ID_PATH" ] || [ ! -s "$SECRET_ID_PATH" ] || [ "${_PAIR_OWNER:-}" != "$CE_STATE_OWNER_UID" ]; then
        APPROLE_PAIR_MISSING=1
        echo "openbao-init: DONE marker present but the core-api AppRole role_id/secret_id files are missing/empty or not owned by the reader uid $CE_STATE_OWNER_UID - re-issuing them the same way the bootstrap path does (needs a token on this run)."
    fi
    unset _PAIR_OWNER
    if [ "$STORED_VERSION" = "$POLICY_VERSION" ]; then
        echo "openbao-init: policy version $POLICY_VERSION already up-to-date — no op."
    else
        NEEDS_POLICY_UPGRADE=1
        echo "openbao-init: policy version ${STORED_VERSION} -> ${POLICY_VERSION}: re-writing OpenBao policies ..."
    fi
    # P6: the marker can be current while the DATA volume's ACL registry
    # is not — the marker lives in `openbao-init-state`, the registry lives in the
    # data volume, and the 1.15 -> OpenBao copy drops the registry (only builtins
    # survive) while the AppRole role still lists its token_policies, so AppRole
    # login 500s. A missing policy counts as an upgrade too, so the token branches
    # below re-write through the SAME guarded step (never a first-init-only path).
    if [ -n "$POLICY_GUARD_MISSING" ]; then
        NEEDS_POLICY_UPGRADE=1
        echo "openbao-init: policy-list guard: DATA-volume registry missing policies:$POLICY_GUARD_MISSING on a marker-current install (post-cutover shape) — re-installing them with the operator token below."
    fi
    if [ -n "${BAO_TOKEN:-}" ]; then
        # Operator-supplied token (policy-write rights): use it as-is, never revoke or print it.
        if [ "$NEEDS_POLICY_UPGRADE" = "1" ]; then
            # the upgrade re-writes the DB ROLE too (write_db_role),
            # so DONE installs get the revocation_statements trio. A failure keeps the
            # marker unwritten and never blocks stack start.
            if ( write_policies && write_db_role && umask 077 && printf '%s' "$POLICY_VERSION" > "$POLICY_VERSION_MARKER" ); then
                echo "openbao-init: policies + api-readwrite role upgraded to version $POLICY_VERSION (operator token)."
            else
                echo "openbao-init: policy upgrade with the supplied BAO_TOKEN FAILED (marker not updated; stack start not blocked). Re-run: docker compose run --rm -e BAO_TOKEN openbao-init." >&2
            fi
        fi
        if [ "$APPROLE_PAIR_MISSING" = "1" ]; then
            deliver_approle_pair || { echo "openbao-init: FATAL - the core-api AppRole pair could not be re-issued on this unseal-only run (operator BAO_TOKEN lacks approle read rights, or the role is gone); the stack cannot start without it." >&2; exit 1; }
        fi
        # seed the embedded-worker slot on an UPGRADE install (after the policy
        # upgrade above so the new policy-admin create grant is live first).
        seed_embedded_worker_token || true
        seed_ingress_ask_token
        exit 0
    fi
    if [ -n "${BAO_BREAKGLASS_ROLE_ID:-}" ] && [ -n "${BAO_BREAKGLASS_SECRET_ID:-}" ]; then
        # Break-glass AppRole login (the pair the fresh path
        # wrote to /breakglass; operator supplies it via compose -e, never printed).
        # The login token is revoked at the end of the run (trap).
        BGL_TOKEN="$(bao write -field=token auth/approle/login role_id="$BAO_BREAKGLASS_ROLE_ID" secret_id="$BAO_BREAKGLASS_SECRET_ID" || true)"
        if [ -z "$BGL_TOKEN" ]; then
            echo "openbao-init: break-glass AppRole login FAILED (bad/expired pair, or the role is missing on a pre-break-glass install); marker not updated; stack start not blocked. Re-issue the pair with: docker compose run --rm -e BAO_TOKEN openbao-init." >&2
            exit 0
        fi
        export BAO_TOKEN="$BGL_TOKEN"
        unset BGL_TOKEN
        trap 'if [ -n "${BAO_TOKEN:-}" ]; then bao token revoke -self >/dev/null 2>&1 || true; fi' EXIT
        if ( write_policies && write_db_role && umask 077 && printf '%s' "$POLICY_VERSION" > "$POLICY_VERSION_MARKER" ); then
            echo "openbao-init: policies + api-readwrite role upgraded to version $POLICY_VERSION (break-glass AppRole; login token revoked)."
        else
            echo "openbao-init: policy upgrade with the break-glass AppRole FAILED (marker not updated; stack start not blocked). Re-run: docker compose run --rm -e BAO_TOKEN openbao-init." >&2
        fi
        if [ "$APPROLE_PAIR_MISSING" = "1" ]; then
            deliver_approle_pair || { echo "openbao-init: FATAL - the core-api AppRole pair could not be re-issued on this unseal-only run (the break-glass token lacks approle read rights, or the role is gone); the stack cannot start without it." >&2; exit 1; }
        fi
        # same seed on the break-glass upgrade path (the policy-admin create
        # grant written above is what lets this login token kv put the slot).
        seed_embedded_worker_token || true
        seed_ingress_ask_token
        exit 0
    fi
    # no token on this run and the pair is NOT on disk ->
    # nothing can re-issue it and nothing can start. Fail loud (the old shape
    # exited 0 here, which left my_ce_migrate with empty VAULT_APPROLE_* and
    # the whole stack dead).
    if [ "$APPROLE_PAIR_MISSING" = "1" ]; then
        echo "openbao-init: FATAL - DONE marker present, core-api AppRole role_id/secret_id files missing/empty, and no token on this run to re-issue them. Re-run with an operator token (docker compose run --rm -e BAO_TOKEN openbao-init) or the break-glass pair (-e BAO_BREAKGLASS_ROLE_ID -e BAO_BREAKGLASS_SECRET_ID), or, if openbao-data is also fresh, wipe BOTH openbao-init-state and openbao-data (this destroys all OpenBao secrets; the stack re-bootstraps from zero)." >&2
        exit 1
    fi
    # An up-to-date run with no token has nothing to seed with —
    # say so (the slot may still be missing, which keeps edge_ready false) and
    # exit as today. Only a genuine version gap prints the operator guidance.
    if [ "$NEEDS_POLICY_UPGRADE" = "0" ]; then
        echo "infrastructure/ingress: no token on this unseal-only run — seed skipped (run openbao-init with BAO_TOKEN or the break-glass pair to seed it)."
        echo "infrastructure/embedded-worker: cannot be verified or seeded without a token on this unseal-only run — if mod-worker-embedded logs 'Worker bootstrap token required', re-run with BAO_TOKEN or the break-glass pair (see README 'Upgrading OpenBao policies')."
        exit 0
    fi
    echo "openbao-init: policy upgrade needs an operator: either export a token with policy-write rights as BAO_TOKEN, or supply the break-glass AppRole pair recorded at first init as BAO_BREAKGLASS_ROLE_ID + BAO_BREAKGLASS_SECRET_ID, then run: docker compose run --rm -e BAO_TOKEN openbao-init (or docker compose run --rm -e BAO_BREAKGLASS_ROLE_ID -e BAO_BREAKGLASS_SECRET_ID openbao-init) — see README \"Upgrading OpenBao policies\". The stack start is not blocked."
    echo "infrastructure/embedded-worker: cannot be verified or seeded without a token on this unseal-only run — if mod-worker-embedded logs 'Worker bootstrap token required', re-run with BAO_TOKEN or the break-glass pair (see README 'Upgrading OpenBao policies')."
    exit 0
fi

if [ -z "${ROOT_TOKEN:-}" ]; then
    echo "ERROR: bootstrap not marked done, but no root token is available from this run (OpenBao was already initialized by an earlier run that did not finish the engine/policy setup, and that run's root token is gone). Manual recovery needed: restore the openbao-init-state volume (unseal key) from backup; do not clear it while openbao-data is still initialized." >&2
    exit 1
fi

export BAO_TOKEN="$ROOT_TOKEN"
unset ROOT_TOKEN

_enable_secrets() { bao secrets list 2>/dev/null | grep -q "^$1/" || bao secrets enable -path="$1" "$2"; }
_enable_auth() { bao auth list 2>/dev/null | grep -q "^$1/" || bao auth enable "$1"; }

echo "enabling KV v2 at secret/ ..."
_enable_secrets secret "kv-v2"

echo "enabling AppRole auth ..."
_enable_auth approle approle

echo "writing core-api policy + role ..."
# Scoped to exactly the paths CE's api code touches (crud/secret_cud.py's
# tenants/{tenant_id}/secrets/{name} KV shape, utils/vault_client.py's
# transit key-name convention tenant-<id>, and the api-readwrite DB role) —
# no blanket secret/data/*, secret/metadata/*, or transit/* grants.
# P6: the fresh install goes through the SAME guarded step as every other
# unsealed run (force = write even when `bao policy list` cannot be read, because
# on a first init the registry is legitimately empty). A still-missing registry
# after the write aborts here — same abort-before-marking-done shape the unguarded
# write_policies call had under `set -eu`.
ensure_policies "fresh install" force
if [ -n "$POLICY_GUARD_MISSING" ]; then
    echo "ERROR: policy-list guard (fresh install): policies still missing after the write:$POLICY_GUARD_MISSING — refusing to mark the bootstrap done (a half-written registry leaves AppRole login 500ing). Re-run: docker compose run --rm openbao-init (policies are rewritten on the next run); list them with: docker compose exec openbao bao policy list." >&2
    exit 1
fi
bao write auth/approle/role/core-api policies="core-api" token_ttl=1h token_max_ttl=4h >/dev/null

ROLE_ID="$(bao read -field=role_id auth/approle/role/core-api/role-id)"
SECRET_ID="$(bao write -field=secret_id -f auth/approle/role/core-api/secret-id)"
if [ -z "$ROLE_ID" ] || [ -z "$SECRET_ID" ]; then
    echo "ERROR: failed to read back the core-api AppRole role_id/secret_id on the fresh install - aborting before delivery (a stack with no readable pair leaves every reader without Vault auth). Re-run before delivery: docker compose run --rm -e BAO_TOKEN openbao-init." >&2
    exit 1
fi
# deliver to the reader uid (see _deliver_secret above) - a
# root:root 0600 pair here is exactly the fresh-boot failure shape.
_deliver_secret "$ROLE_ID" "$ROLE_ID_PATH"
_deliver_secret "$SECRET_ID" "$SECRET_ID_PATH"
unset ROLE_ID SECRET_ID
echo "written"

echo "minting the break-glass policy-upgrade AppRole ..."
# Fresh-install only, while the root token is still live. The role's ONLY policy
# is policy-admin (sys/policies/acl rewrite rights — see write_policies above),
# 15m TTL, login token has no use-count limit (token_num_uses=0) and is revoked at end of run (EXIT trap), no default policy, no self-widening.
# Delivery: role_id + secret_id land in ONE 0400 file in the `openbao-breakglass`
# volume (mounted ONLY into openbao-init at /breakglass). Never printed.
if [ -f "$BREAKGLASS_FILE" ]; then
    echo "break-glass delivery file already present at $BREAKGLASS_FILE — never overwritten; keeping the previously recorded pair."
else
    bao write auth/approle/role/breakglass \
        token_policies="policy-admin" \
        token_ttl=15m token_max_ttl=30m token_num_uses=0 \
        secret_id_num_uses=0 secret_id_ttl=0 \
        token_no_default_policy=true bind_secret_id=true >/dev/null
    BGL_ROLE_ID="$(bao read -field=role_id auth/approle/role/breakglass/role-id)"
    BGL_SECRET_ID="$(bao write -field=secret_id -f auth/approle/role/breakglass/secret-id)"
    if [ -z "$BGL_ROLE_ID" ] || [ -z "$BGL_SECRET_ID" ]; then
        echo "ERROR: failed to read back the break-glass role_id/secret_id — aborting before delivery. Re-run: docker compose run --rm -e BAO_BREAKGLASS_ROLE_ID -e BAO_BREAKGLASS_SECRET_ID openbao-init." >&2
        exit 1
    fi
    (umask 077 && printf '%s\n%s\n' "$BGL_ROLE_ID" "$BGL_SECRET_ID" > "$BREAKGLASS_FILE")
    chmod 400 "$BREAKGLASS_FILE"
    unset BGL_ROLE_ID BGL_SECRET_ID
    echo "break-glass pair recorded at $BREAKGLASS_FILE (mode 0400, volume openbao-breakglass — mounted ONLY into openbao-init). Fetch it ONCE with:"
    echo "  docker compose run --rm --entrypoint cat openbao-init /breakglass/breakglass.txt"
    echo "store it OFFLINE (shell history and docker logs never see the values), then remove it with:"
    echo "  docker compose run --rm --entrypoint rm openbao-init /breakglass/breakglass.txt"
fi

echo "enabling database secrets engine (api-readwrite role against postgres-app) ..."
_enable_secrets database database
write_db_role

echo "provisioning the embedded-worker bootstrap token ..."
# Written with the ROOT token
# (infrastructure/* is root-write-only; the core-api AppRole only reads it).
# The API's startup seed (main.py) READS this and hashes it onto APPROVED
# WorkerEnrollment rows for mod-worker-embedded (+ the legacy alias mod-worker-ce); the Go worker
# reads the same path via GetInfraSecret("embedded-worker"). Idempotent: a
# re-run keeps the existing token (rotating it would orphan the enrollment
# hash the seed already wrote). Never printed.
# NOTE: `bao read`/`bao write` are the RAW API and do NOT translate to a kv-v2
# mount's data/ sub-path — against `secret/` (kv-v2) they fail with "Invalid
# path for a versioned K/V secrets engine", which under `set -eu` would abort
# this whole init script. Must use the `kv get`/`kv put` CLI sugar, exactly
# like the other kv get/kv put call sites in this script.
# the inline block moved into seed_embedded_worker_token() (above) so the
# DONE/upgrade branches seed it too. Fresh installs keep today's semantics: a
# failed put aborts the bootstrap (the stack cannot start without the slot).
seed_embedded_worker_token || exit 1

echo "seeding the stack-ingress ask token ..."
# The api reads the ingress ask token from OpenBao
# (secret/infrastructure/ingress ask_token, routers/persistent_stacks/_shared.py:4212),
# not from env; compose hands MOD_INGRESS_ASK_TOKEN to this init job so it can seed
# that slot. Same kv get/put sugar as the embedded-worker block above (the raw
# read/write API does not translate kv-v2 data/ paths). Value never printed; it
# goes through argv only in the existing `kv put key=value` shape. Keep-if-present:
# rotating it would orphan the edge the stack already configured. The seeding moved
# this into seed_ingress_ask_token() so the DONE/upgrade paths seed it too, and made
# an unset/empty env GENERATE one instead of writing nothing (edge_ready, c2-J2).
seed_ingress_ask_token

echo "enabling transit mount (no key created — dynamic/on-demand) ..."
_enable_secrets transit transit

touch "$DONE_MARKER"

# Fresh install: policies were written by write_policies above; record the version.
(umask 077 && printf '%s' "$POLICY_VERSION" > "$POLICY_VERSION_MARKER")

echo "attempting root token revoke-self ..."
if bao token revoke -self >/dev/null 2>&1; then
    echo "root token revoked."
else
    echo "NOTE: root token revoke-self did not succeed via the CLI (or is unsupported here) — root token remains valid until its TTL expires. Not fatal; not printed."
fi
unset BAO_TOKEN

echo "openbao-init: bootstrap complete."
