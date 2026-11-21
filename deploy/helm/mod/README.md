# MOD Core (Community Edition) — Helm chart

Kubernetes chart for MOD Core: the FastAPI **API**, the **Dashboard**, the
**WorkflowBuilder** (`workflows`), and the Go **worker** (DaemonSet driving a
node's Docker daemon, *or* k8s **executor mode** where the API submits node
tasks + persistent stacks as k8s workloads itself). The platform datastores —
**postgres**, **valkey**, **versitygw** (S3), **qdrant**, **keycloak**,
**vault** (OpenBao in CE) — each render as a simple internal StatefulSet+PVC or point at an
external/managed instance. The **KAI scheduler** is a separate Helm release (installed first); MOD only carries a
`kai.enabled` values key (default `false`).

The services, env and ports mirror the CE `docker-compose.yml` at the repo root
(api, mod-dashboard, workflow-builder, mod-worker, postgres, `redis`=valkey,
versitygw, keycloak, and `openbao` as the chart's `vault` store). qdrant has NO
CE compose service — the chart's internal qdrant StatefulSet is chart-only
(HELM-INSTALL-PROOF-2 gap 5).

## Layout

```
deploy/helm/mod/
├── Chart.yaml            # chart meta (no dependencies: — KAI is a separate Helm release)
├── values.yaml           # CE defaults: ghcr.io/modtim images, OpenBao, daemonset worker, all datastores internal
├── values-onprem.yaml    # overlay: external datastores + k8s executor mode
├── values-onprem-kai.yaml # overlay: on-prem + KAI scheduler
└── templates/            # deployments, daemonset, services, ingress, StatefulSets
```

## Prerequisites

- Kubernetes 1.28+, Helm 3.x.
- A default `StorageClass` (all internal datastores use PVCs via
  `volumeClaimTemplates`).
- Registry: `ghcr.io/modtim` (the same `imageRegistry`/`imageProject` the CE
  compose uses). **No image pull secret is needed once the CE packages are public; until then import the images locally (see the NOT TRUE TODAY note).** Once public, leave `global.imagePullSecrets`
  empty unless you point `global.imageRegistry` / `global.imageProject` at a
  private mirror. NOT TRUE TODAY: anonymous pulls from `ghcr.io/modtim` get 403 at
  the token endpoint / 401 on manifest GETs (HELM-INSTALL-PROOF-2, 11-10), so a
  first install must either `ctr images import` the CE images locally
  (`imagePullPolicy: IfNotPresent` already covers that) or wait until the CE
  packages are made genuinely public.
- Image tag: `global.imageTag` (default `latest` in a source checkout; a CE
  release stamps its release tag here, like the compose `CE_IMAGE_TAG`). Pin a
  release with `--set global.imageTag=<tag>`, or one component with
  `.<component>.image.tag` (or `.registry` / `.project`).
- `global.baseDomain` defaults to the placeholder `example.com`; set your own
  domain (the ingress hosts are `dashboard.`, `workflows.` and `api.` under it).
- GPU nodes for the worker DaemonSet: label them
  `kubectl label node <node> mod.tech/gpu=true` (the DaemonSet's `nodeSelector`,
  `worker.daemonset.nodeSelector`; nothing else reads this label).

## Required secrets

The chart renders **no Secret objects** and `values.yaml` contains **no
secret values** — every credential is read from a pre-existing Secret in the
release namespace. Create them before `helm install` (all values below are
placeholders):

| Secret name (values key)                    | Required keys |
|--------------------------------------------|---------------|
| `api.existingSecret` (default `mod-api-secrets`) | `VAULT_APPROLE_ROLE_ID`, `VAULT_APPROLE_SECRET_ID`, `KEYCLOAK_REALM_NAME`, `KEYCLOAK_DASHBOARD_CLIENT_ID`, `MOD_INGRESS_ASK_TOKEN` (optional), `SECRET_ENCRYPTION_KEY` — the api Secret does **not** carry `VAULT_BOOTSTRAP_TOKEN`; the bootstrap Job reads only the role/secret pair from it, and the token lives in the dedicated scoped Secret on the next row |
| `<fullname>-vault-bootstrap-secret` (fullname-scoped, one per tenant, created in the release namespace only) | `VAULT_BOOTSTRAP_TOKEN` — short-lived, limited to approle setup; consumed by the `mod-vault-approle-bootstrap` Job (`templates/vault-approle-bootstrap.yaml`). With the CE defaults (`vault.devRootToken` empty, non-dev server) this is the ONLY consumer: set it to the root token from your `bao operator init` (see "OpenBao init + Shamir unseal" below), so the Job authenticates against the server you initialized (no 403s, no token to copy out of the pod log). Only when you opt back into the legacy `devRootToken` dev path does the OpenBao pod also read it as its pinned dev root token |
| `worker.daemonset.existingSecret` (default `mod-worker-secrets`) | `KEYCLOAK_REALM_NAME`, `KEYCLOAK_SERVICE_CLIENT_ID`, `VAULT_APPROLE_ROLE_ID`, `VAULT_APPROLE_SECRET_ID`, `VERSITYGW_ROOT_ACCESS_KEY`, `VERSITYGW_ROOT_SECRET_KEY` |
| `postgres.secret.name` / `postgres.external.secretRef` (default `mod-postgres-secret`) | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `valkey.secret.name` / `valkey.external.secretRef` (default `mod-valkey-secret`) | `REDIS_PASSWORD` |
| `versitygw.secret.name` / `versitygw.external.secretRef` (default `mod-versitygw-secret`) | `VERSITYGW_ROOT_ACCESS_KEY`, `VERSITYGW_ROOT_SECRET_KEY` |
| `qdrant.secret.name` / `qdrant.external.secretRef` (default `mod-qdrant-secret`) | `QDRANT_API_KEY` |
| `keycloak.secret.name` / `keycloak.external.secretRef` (default `mod-keycloak-secret`) | `KEYCLOAK_DB_PASSWORD`, `KEYCLOAK_ADMIN_USER`, `KEYCLOAK_ADMIN_PASSWORD` |
| `ingress.tlsSecretName` (optional)         | standard `kubernetes.io/tls` Secret (tls.crt/tls.key) for the three subdomain hosts |

`KEYCLOAK_REALM_NAME` is not a free choice: it must equal `keycloak.realmName`
(CE default `modtex`, the realm `templates/realm-import.yaml` seeds). A Secret
naming any other realm points the api at an OIDC document that never exists, and
the api entrypoint retries that document for about 200 s (40 x 5 s), then exits 1,
so the pod restarts into CrashLoopBackOff.

Example:

```bash
umask 077
SECDIR=$(mktemp -d)
printf '%s' "$POSTGRES_PASSWORD" > "$SECDIR/POSTGRES_PASSWORD"
kubectl -n mod create secret generic mod-postgres-secret \
  --from-literal=POSTGRES_USER=core \
  --from-file=POSTGRES_PASSWORD="$SECDIR/POSTGRES_PASSWORD" \
  --from-literal=POSTGRES_DB=modtex
rm -f "$SECDIR/POSTGRES_PASSWORD"
rmdir "$SECDIR"
```

The install-time secret step must also generate `SECRET_ENCRYPTION_KEY` for the
api Secret: a Fernet-valid key (32 random bytes, base64 — e.g.
`python3 -I -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'`
written to a mode-600 file that is passed with `--from-file=SECRET_ENCRYPTION_KEY=<file>`
and removed with `rm -f` after (see the cleanup line), value never printed, never stored in the chart).

Create the **api Secret** the same way — every value comes from a mode-600 file
(`--from-file=KEY=<path>`; kubectl reads the PATH, so over ssh never use
`<(printf ...)` process substitution: it hands kubectl a `/dev/fd/NN` that does
not exist there — HELM-INSTALL-PROOF-2 hit `error reading /dev/fd/63`). Nothing
is echoed or committed, and the step fails visibly (`set -euo pipefail`, no
`|| true`) if any value is missing:

```bash
set -euo pipefail
umask 077
for _v in VAULT_APPROLE_ROLE_ID VAULT_APPROLE_SECRET_ID KEYCLOAK_DASHBOARD_CLIENT_ID; do
  [ -n "${!_v:-}" ] || { echo "missing env: $_v"; exit 1; }; done
# Private scratch dir (mode 700 under umask 077). Each value is written to its own
# mode-600 file here, so kubectl gets a real path (never a /dev/fd/NN process
# substitution). Values are never echoed and never passed as command arguments.
SECDIR=$(mktemp -d)
printf '%s' "$VAULT_APPROLE_ROLE_ID" > "$SECDIR/VAULT_APPROLE_ROLE_ID"
printf '%s' "$VAULT_APPROLE_SECRET_ID" > "$SECDIR/VAULT_APPROLE_SECRET_ID"
printf '%s' modtex > "$SECDIR/KEYCLOAK_REALM_NAME"
printf '%s' "$KEYCLOAK_DASHBOARD_CLIENT_ID" > "$SECDIR/KEYCLOAK_DASHBOARD_CLIENT_ID"
python3 -I -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())' > "$SECDIR/SECRET_ENCRYPTION_KEY"
# api.existingSecret, tenant-scoped: -n <release-ns> precedes the create, and
# this Secret is created ONLY in the release namespace (never shared cross-tenant).
# Values come from your shell (exported out-of-band); nothing here is echoed or
# committed.
kubectl -n mod create secret generic mod-api-secrets \
  --from-file=VAULT_APPROLE_ROLE_ID="$SECDIR/VAULT_APPROLE_ROLE_ID" \
  --from-file=VAULT_APPROLE_SECRET_ID="$SECDIR/VAULT_APPROLE_SECRET_ID" \
  --from-file=KEYCLOAK_REALM_NAME="$SECDIR/KEYCLOAK_REALM_NAME" \
  --from-file=KEYCLOAK_DASHBOARD_CLIENT_ID="$SECDIR/KEYCLOAK_DASHBOARD_CLIENT_ID" \
  --from-file=SECRET_ENCRYPTION_KEY="$SECDIR/SECRET_ENCRYPTION_KEY"
# optional, only if you use ingress ASK: also write
#   printf '%s' "$MOD_INGRESS_ASK_TOKEN" > "$SECDIR/MOD_INGRESS_ASK_TOKEN"
# and add
#   --from-file=MOD_INGRESS_ASK_TOKEN="$SECDIR/MOD_INGRESS_ASK_TOKEN"

# The vault-bootstrap token is NOT a key of the api Secret (that Secret keeps the
# role/secret pair only). It gets its OWN dedicated scoped Secret - fullname-scoped,
# one per tenant, created in the release namespace only, short-lived and limited to
# approle setup; the mod-vault-approle-bootstrap Job reads it and nothing else does.
# The vault-bootstrap token Secret name is FULLNAME-scoped: the templates read
# `{{ include "mod.fullname" . }}-vault-bootstrap-secret`, so with a release named
# `mod` it is `mod-vault-bootstrap-secret`; with any other release name (or
# fullnameOverride) use `<release>-vault-bootstrap-secret`.
# The token comes from your shell (exported out-of-band); checked here, not in the api loop.
[ -n "${VAULT_BOOTSTRAP_TOKEN:-}" ] || { echo "missing env: VAULT_BOOTSTRAP_TOKEN"; exit 1; }
printf '%s' "$VAULT_BOOTSTRAP_TOKEN" > "$SECDIR/VAULT_BOOTSTRAP_TOKEN"
kubectl -n mod create secret generic mod-vault-bootstrap-secret \
  --from-file=VAULT_BOOTSTRAP_TOKEN="$SECDIR/VAULT_BOOTSTRAP_TOKEN"
# Remove only the files this block wrote into $SECDIR, then the directory (no rm -r).
rm -f "$SECDIR/VAULT_APPROLE_ROLE_ID" "$SECDIR/VAULT_APPROLE_SECRET_ID" "$SECDIR/KEYCLOAK_REALM_NAME" "$SECDIR/KEYCLOAK_DASHBOARD_CLIENT_ID" "$SECDIR/SECRET_ENCRYPTION_KEY" "$SECDIR/MOD_INGRESS_ASK_TOKEN" "$SECDIR/VAULT_BOOTSTRAP_TOKEN"
rmdir "$SECDIR"
```

The OpenBao side of that pair is provisioned for you by the chart's
`mod-vault-approle-bootstrap` Job (`templates/vault-approle-bootstrap.yaml`,
gated on `vault.internal`): it enables `auth/approle`, writes the tenant-scoped
policies, creates the api role and mounts the database role `api-readwrite` —
so operators never hand-mount AppRole against the api Secret.

### OpenBao init + Shamir unseal (CE defaults: non-dev, sealed at start)

The CE defaults run the OpenBao server NON-dev (durable, file-backed storage on
the vault `data` PVC), so it starts **uninitialized and sealed** — the
bootstrap Job cannot provision until you bring the server up once, in the
release namespace, keeping every secret out of the repo (mode-600 file on the
box, deleted after use):

```bash
NS=<release-ns>; UM=<fullname>-vault            # e.g. NS=mod UM=mod-vault
umask 077; : > /tmp/bao-init.json               # mode-600 scratch, on the box

# wait until the pod is up — a SEALED pod counts as ready (bao status exits 2)
kubectl -n "$NS" exec -n "$UM-0" -c vault -- sh -c 'bao status'

# Shamir init: OpenBao default is 5 shares / threshold 3; the CE compose's
# one-shot flow uses 1/1 (single-key, evaluation-grade) — pick deliberately.
kubectl -n "$NS" exec -n "$UM-0" -c vault -- bao operator init \
  -key-shares=5 -key-threshold=3 -format=json > /tmp/bao-init.json

# unseal THRESHOLD times with the shares from the 600 file (substituted, never
# pasted), then prove with FIELDS ONLY (never print key/token values):
kubectl -n "$NS" exec -n "$UM-0" -c vault -- bao operator unseal \
  "$(grep -A1 '"unseal_keys_b64"' /tmp/bao-init.json | tail -1 | sed 's/^[[:space:]]*"//; s/"[[:space:]]*,\{0,1\}$//')"
kubectl -n "$NS" exec -n "$UM-0" -c vault -- bao status | grep -E 'Initialized|Sealed'
# expect: Initialized: true / Sealed: false

# hand the init root token to the bootstrap Job's scoped Secret (the Job has
# restartPolicy: OnFailure, so it retries until this lands):
kubectl -n "$NS" get secret <fullname>-vault-bootstrap-secret >/dev/null  # exists per "Required secrets"
shred -u /tmp/bao-init.json                                               # delete after use
```

`templates/NOTES.txt` prints the same steps after `helm install`. The unseal
key(s) are the operator's to keep (or move to `bao operator set` / a KMS seal
later); they are never stored in the repo, the chart, or values.

(For `onprem` mode you can point the `*.external.secretRef` names at your
existing managed-service credential Secrets — the key names above must
match, or create a Secret with exactly those keys.)

## Install — default (all datastores internal)

```bash
helm install mod ./deploy/helm/mod -n mod --create-namespace \
  --set global.baseDomain='<your-domain>'
```

`DEPLOYMENT_MODE=onprem`, all six datastores internal (StatefulSet+PVC), worker
as a DaemonSet on `mod.tech/gpu: "true"` nodes, one replica per app tier. Create
the Secrets from "Required secrets" in the `mod` namespace first (or right after
the install: the pods wait for them). On a single-node trial cluster you can
label the node itself: `kubectl label node <node> mod.tech/gpu=true`.
On a single-node trial cluster you can instead drop the selector for the proof
install (no cluster-wide label needed): `--set worker.daemonset.nodeSelector=null`
(HELM-INSTALL-PROOF-2: renders the worker DaemonSet with 0 `mod.tech/gpu` in the
render).

## Install — on-prem (external datastores, executor mode)

```bash
helm install mod ./deploy/helm/mod -f deploy/helm/mod/values-onprem.yaml \
  -n mod --create-namespace
```

`DEPLOYMENT_MODE=onprem`; each datastore is `internal: false` and the API
env points at `*.external.host`/`port` (fill in real hosts in the overlay or
`--set`); **no worker DaemonSet** — the API gets
`MOD_EXECUTOR_BACKEND=k8s` + `MOD_STACK_BACKEND=k8s` (the env
`CORE/API/utils/k8s_backend_config.py` reads) and submits node tasks +
persistent stacks to the cluster itself. The API pod's in-cluster
ServiceAccount token is used. The Kubernetes backend is included in Community
Edition.

Mixed shapes are fine: flip any single datastore between `internal` /
`external` independently.

### Datastore reference (internal)

| Store | Image (values) | Ports | Probe |
|---|---|---|---|
| postgres | `postgres:17.11@sha256:b8faef…` (digest-pinned; see "Datastore images") | 5432 | `pg_isready` |
| valkey | `valkey/valkey:8-alpine` | 6379 | `valkey-cli ping` |
| versitygw | `ghcr.io/versity/versitygw:v1.7.0` | 7070 (S3), 7071 (IAM) | TCP 7070 |
| qdrant | `qdrant/qdrant:v1.9.4` | 6333, 6334 | TCP 6333 |
| keycloak (+ its postgres) | `keycloak/keycloak:26.3@sha256:357829…` / `postgres:17.11@sha256:b8faef…` (both digest-pinned, same refs as the CE compose) | 8080 / 5432 | `management` 9000: `/health/ready` |
| vault (OpenBao) | `openbao/openbao:2.6.4` (digest-pinned, same as the CE compose) | 8200 | `bao status` (sealed counts as up) |

Every internal store gets a headless Service `<release>-<store>`.

## Worker modes

- `worker.mode: daemonset` (default) — DaemonSet with
  `nodeSelector`/`tolerations` for GPU nodes; mounts the host Docker socket
  (`worker.daemonset.hostPathDockerSocket`, on by default (`true`); set it `false` to opt out; `dockerSocketPath` sets the
  HOST side only — the in-pod mount stays `/var/run/docker.sock`, so set it only
  when your host socket lives elsewhere). Needs `mod-worker-secrets` + `mod-valkey-secret` +
  `mod-versitygw-secret`.
- `worker.mode: executor` — the DaemonSet renders nothing; the API is wired
  for the k8s backend (see above). Use this on GPU clusters where node
  containers should be plain k8s Jobs/Deployments.

## Ingress

Three hosts, mirroring the compose BASE_DOMAIN subdomain scheme:

- `<DASHBOARD_SUBDOMAIN>.<BASE_DOMAIN>` → dashboard :80
- `<WORKFLOWS_SUBDOMAIN>.<BASE_DOMAIN>` → workflow-builder :3000
- `<API_SUBDOMAIN>.<BASE_DOMAIN>` → api :80

TLS: set `ingress.tlsSecretName` (existing `kubernetes.io/tls` Secret
covering the three hosts) or add cert-manager annotations under
`ingress.annotations`.

## KAI scheduler (optional)

`kai` is a plain values key in `values.yaml` (`kai.enabled`, default `false`); when enabled, every MOD pod
gets `schedulerName: <kai.schedulerName>`.

KAI Scheduler is a **separate Helm release** (not a MOD subchart). MOD only
emits `schedulerName: kai-scheduler` on its pods. Customers install their own
KAI chart at `--version v0.18.7`; MOD's pods receive the `schedulerName` field
so the KAI scheduler picks them up.

To install the KAI chart:
```
helm install kai-scheduler <KAI_CHART_REPO_URL>/kai-scheduler \
  --version v0.18.7 \
  -n <NAMESPACE>
```

With `kai.enabled=false` (the default), MOD pods are scheduled by the
default Kubernetes scheduler.

## KAI install (on-prem with scheduler)

> **Not a working install path for external datastores.** `values-onprem-kai.yaml`
> sets the datastore keys as `postgres.host`, `valkey.host`, `qdrant.host`,
> `keycloak.realm`/`adminUrl`, `vault.existingSecret`, `versitygw.host` (and the
> rest of its block), but no template reads those paths: the chart reads
> `<store>.external.host` / `.port` / `.secretRef` and `<store>.internal`. With that
> file all six datastores stay INTERNAL and its host values are silently ignored.
> To use external datastores, set `<store>.internal: false` and the
> `<store>.external.*` keys (for example `--set postgres.internal=false
> --set postgres.external.host=<host>`), or point the install at a values file
> that uses the `external:` form; verify with the `helm template` check below.

1. Install the separate KAI release into your Kubernetes cluster:
   ```
   helm install kai-scheduler <KAI_CHART_REPO_URL>/kai-scheduler \
     --version v0.18.7 \
     -n kai-scheduler
   ```
2. Install the MOD release with the KAI overlay:
   ```
   helm install mod ./deploy/helm/mod \
     -f deploy/helm/mod/values-onprem-kai.yaml \
     --set kai.enabled=true
   ```
3. Verify every pod has `schedulerName: kai-scheduler` via:
   ```
   helm template mod ./deploy/helm/mod \
     -f deploy/helm/mod/values-onprem-kai.yaml \
     --set kai.enabled=true | grep schedulerName
   ```
   (expect 11 occurrences — api, dashboard, workflows, worker, postgres, valkey,
   vault, qdrant, keycloak, keycloak-db, versitygw).

## Notes / known deltas vs the compose stack

- **No at-rest encryption in the Helm install**: the CE compose ships the
  encrypted sidecars `ghcr.io/modtim/valkey-enc` and `ghcr.io/modtim/versitygw-enc`
  (`docker-compose.yml:151` / `:219`), while this chart's internal stores are the
  upstream `valkey/valkey:8-alpine` and `ghcr.io/versity/versitygw:v1.7.0`. The
  Helm path therefore does NOT get at-rest encryption; point `valkey.image` /
  `versitygw.image` at the `-enc` builds (or an external managed store) to match
  the compose.

- **PgBouncer is not part of this chart.** The API connects directly to
  postgres (`POSTGRES_HOST`); the API's `POSTGRES_POOLER_HOST` mechanism is
  left unset. Add a pgbouncer Deployment later if the connection budget
  requires it.
- **Keycloak theme/entrypoint**: the compose stack mounts a custom theme
  dir and wrapper entrypoint; the chart uses the stock image `start`
  command (branding not carried). `KC_HOSTNAME` =
  `keycloak.externalUrl` (default `<protocol>://<keycloak.keycloakSubdomain>.<baseDomain>`)
  — route that host to keycloak (ingress/edge) so the issuer stays valid.
- **Secrets server (OpenBao) — NON-DEV, durable, starts SEALED**: FIX-HELM-OPENBAO-NON-DEV-1113
  (owner 11-13 14:00: CE must ship OpenBao in non-dev mode with Shamir unseal,
  no dev-mode root token). The CE defaults leave `vault.devRootToken` empty, so
  the chart runs the image with `command: server` (NOT `server -dev`) plus a
  file-backed `BAO_LOCAL_CONFIG` — the same shape the CE compose proves on 2.6.4
  — with storage on the vault StatefulSet's `data` PVC: secrets and the AppRole
  provisioning SURVIVE pod restarts. The server starts **uninitialized and
  sealed**; nothing in the chart initializes or unseals it, and no unseal key or
  root token value ever renders. Bring it up once with the documented operator
  steps in `templates/NOTES.txt` (and the "OpenBao init + Shamir unseal" block
  below): `bao operator init` (Shamir; OpenBao default 5 shares / threshold 3 —
  the CE compose's one-shot flow uses 1/1, evaluation-grade), unseal with the
  share(s), then set the `VAULT_BOOTSTRAP_TOKEN` key of the
  `<fullname>-vault-bootstrap-secret` Secret to the root token from that init
  so the `mod-vault-approle-bootstrap` Job and the server agree by construction.
  Keep the init JSON in a mode-600 file on the box, use it, delete it; the keys
  are never stored in the repo, the chart, or values. The readiness probe
  accepts a sealed pod (`bao status` exit 0 or 2), so a sealed server never
  crash-loops the release. The LEGACY pinned-`server -dev` path (in-memory,
  evaluation-only) still renders when you set `vault.devRootToken` non-empty —
  backwards compatible, no longer the CE default. For a fully external server
  keep `vault.internal=false` with `vault.external.addr`.
- **Datastore images**: the CE values pin the chart's internal postgres to the
  same digest-pinned PostgreSQL **17.11** base the CE compose pulls
  (`postgres:17.11@sha256:b8faef…`, the ref its `keycloak-db` / `db-owner-repair`
  services use) — no more chart/compose major-version skew. The compose's app DB
  itself is `postgres-app`, the pg_tle BUILD of that same 17 base
  (`ghcr.io/modtim/postgres-app`); that ref is not anonymously pullable until the
  CE packages are made public, so the chart pins the digest-pinned base instead.
  Set `postgres.image` to your `postgres-app` build to match the compose exactly
  (the chart's `pg_isready` probe and the bootstrap Job's database engine work
  either way).
- **runAsNonRoot**: applied to workflow-builder (image `USER node`). The
  dashboard image runs as root (nginx base, no `USER` line) and listens on
  :80 in-container; the API image sets `USER 10001` and the worker image sets
  `USER 10001:10001`. The chart still sets `runAsNonRoot: false` on the api,
  dashboard and worker pods, deliberately.
- **Worker DaemonSet networking**: node containers spawned on the node's
  Docker daemon reach the worker token endpoint via `MOD_TASK_TOKEN_HOST`
  (pod name); on multi-node clusters consider pinning the worker to a
  network reachable from the node's container runtime (e.g. hostNetwork or
  a per-node Service).

## Verifying a render

```bash
helm lint deploy/helm/mod
helm template mod deploy/helm/mod | grep -E '^\s+image:'   # ghcr.io/modtim/... plus the upstream datastore images
helm template mod deploy/helm/mod -f deploy/helm/mod/values-onprem.yaml | less
```

With the defaults every MOD image is `ghcr.io/modtim/<name>:<global.imageTag>`, no
`imagePullSecrets` render, and the secrets server is the OpenBao image.
