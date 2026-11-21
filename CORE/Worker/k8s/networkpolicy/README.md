# Task-pod NetworkPolicy (T9)

Two files here translate the two Docker task-egress postures in
`CORE/Worker/internal/executor/netpol.go` (`SelectNetworkMode`) into
Kubernetes `NetworkPolicy` manifests for task Job pods:

- `default-deny-egress.yaml` — the `userNetwork` posture (default branch of
  `SelectNetworkMode`: no `PLATFORM_TASK`/`MOD_STACK_NETWORK`/`API_KEY`/
  `MOD_TASK_TOKEN_URL`/`NIM_API_KEY`/`NEEDS_INTERNET`/`AllowedEgress` on the
  payload). Zero outbound beyond DNS + the two fixed in-cluster platform
  endpoints a task's own I/O staging needs.
- `user-egress.yaml` — the `egressNetwork` posture (`NEEDS_INTERNET` set, or
  `AllowedEgress` non-empty). Same fixed allow-list, plus general outbound
  with the cluster's own internal address space carved out.

Both are `${VAR}`-style templates (see the header comment in each file for
the full variable list) — render with `envsubst` or fold into whatever
manifest pipeline T2 (`K8sBackend`)/T3 (`k8s_stack_render.py`)/T8 (Helm
chart) end up using. Not wired into a render/deploy path here — that's
those tasks' job; this change only produces and documents the templates.
Both parse as valid YAML (`python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('*.yaml')]"` — see "Validation" below).

## Allowlist egress (GB.EGR): a third, per-task policy

`default-deny-egress.yaml`/`user-egress.yaml` above are the two *static*,
chart-rendered policies every task pod gets one of, based on its
`mod.tech/egress-mode` label (`deny` or `user-egress`). They cannot express
"only these hostnames" — standard `networking.k8s.io/v1` NetworkPolicy has no
FQDN peer type, only IP/CIDR.

When a task's payload carries a non-empty allowlist (`AllowedEgress`, and no
`NEEDS_INTERNET`), `TaskSpec.Egress` becomes `EgressAllowlist`
(`internal/executor/backend.go`, `k8s_taskspec.go`). The K8s backend then adds
a **third, dynamically-created, per-task policy** on top of the two static
ones: a Cilium `CiliumNetworkPolicy` (`cilium.io/v2`), one per task Job,
rendered by `internal/executor/k8s_fqdn_policy.go` `BuildFQDNPolicy` and
created by `K8sBackend.Submit` right after the Job — never checked into this
directory as a static file, since it is unique per task (`toFQDNs`/`toCIDR`
entries derived from that task's own `AllowedEgress` list, name `<job>-fqdn`,
`endpointSelector` on that job's pod label, `ownerReference` to the Job for
GC).

**This needs the cluster to run Cilium.** `MOD_K8S_EGRESS_FQDN` (env; Helm
`worker.taskPods.networkPolicy.fqdn.mode`) controls it, default `auto`:

| Mode | Behaviour |
|---|---|
| `auto` (default) | Probe for the `ciliumnetworkpolicies.cilium.io/v2` CRD at worker start. Present → same as `cilium`. Absent/error → same as `off`, plus one WARN log. Fail-open. |
| `cilium` | Require Cilium. Missing CRD or probe error fails worker startup. |
| `off` | Never probes; allowlisted tasks always run on plain `user-egress` (today's behaviour). |

With the effective mode `cilium`, the pod is labelled `mod.tech/egress-mode:
deny` (the deny policy in this directory already covers its DNS/API/S3
staging needs) and the CNP narrows it further to exactly the allowlisted
hosts, each parsed by the shared `internal/egress` grammar (exact host,
`*.suffix` wildcard, `host:port`, IP/CIDR literal) that the Docker-worker
egress proxy also uses. If the CNP create call fails on a Cilium cluster, the
Job is deleted and the task errors — **fail-closed**, because the cluster can
enforce and something went wrong.

With the effective mode `off` (explicit, or `auto` with no Cilium detected),
the pod is labelled `mod.tech/egress-mode: user-egress` and annotated
`mod.tech/egress-allowlist: unenforced`; the task still runs — **not**
fail-closed, because a cluster without Cilium has no way to enforce an FQDN
allowlist in the first place, so refusing to run buys nothing.

**RBAC:** the worker's task-namespace Role needs `get`/`create`/`delete` on
`cilium.io` `ciliumnetworkpolicies` (no `list`/`watch`: the worker only ever
touches the CNP it just created, by name). `../rbac.yaml` in this chart
carries that rule unconditionally (harmless without the CRD — the verbs are
simply never exercised); the Helm chart (`CORE/deploy/k8s/mod/templates/
rbac.yaml`) renders the same rule only when `worker.taskPods.networkPolicy
.fqdn.mode` is `cilium` or `auto`.

Full writeup: `CORE/docs/k8s-backend.md` §"Allowlist egress (Cilium FQDN
policy)".

## S3 (versitygw) task I/O — confirmed, no PVC needed

**Claim to verify:** task inputs/outputs reuse S3 (versitygw) exactly as in
the Docker path, so a K8s task Job needs no `PersistentVolumeClaim` for I/O —
just S3 creds + `API_URL` as env.

**Confirmed.** Evidence:

1. **Credentials are minted per-task by the API, not baked into the image.**
   `CORE/API/routers/worker_tasks.py` `POST /workers/tasks/{task_id}/credentials`
   is, per its own comment, "the SINGLE credential source on
   ALL paths": it mints versitygw tenant-scoped creds (or resolves the
   tenant's BYO-S3 connection) and returns
   `{access_key, secret_key, endpoint, mc_host, bucket, session_token}`
   (lines ~1998-2012). This is a plain HTTPS call — nothing filesystem- or
   volume-shaped about it.

2. **The Go worker fetches those creds and pushes them into the container's
   environment, not a mount.** `CORE/Worker/cmd/worker/main.go`:
   `creds, err = tp.GetCredentials(ctx, task.ID)`, immediately after
   `ReportStarted`. `CORE/Worker/internal/transport/api.go`
   implements the call: `POST {base}/workers/tasks/{id}/credentials`.

3. **Byte movement happens over the S3 wire protocol via short-lived
   `mc`-client sidecar containers, into/out of an *ephemeral* volume — never
   a persistent one.** `CORE/Worker/internal/executor/executor.go`:
   - `downloadSidecar`/`buildDownloadCmd` (lines ~1057-1132) run `mc cp
     myminio/<bucket>/<content_hash> /data/input/...` before the task
     container starts.
   - `uploadSidecar` (lines ~1135-1206) run `mc cp /data/output/<file>
     myminio/<bucket>/<sha256>` after it exits.
   - `runSidecarWithOutput` injects the creds as env — only
     into these mc sidecars, never as files:
     `MC_HOST_myminio`, `MINIO_ENDPOINT`, `MINIO_ACCESS`, `MINIO_SECRET`
     (plus `MINIO_SESSION_TOKEN` when present).
   - The volume they share with the task container (`volName`, bound
     `HostConfig.Binds: volName + ":/data:rw"`) is a
     **task-scoped Docker volume created and destroyed with the task**
     (see `cleanupVolume`, referenced in the package comment above
     `prepareDataVolume`: "the volume is task-scoped and gets
     nuked after upload"). It is scratch space for one task's lifetime, the
     conceptual equivalent of a K8s `emptyDir` — not durable storage.
   - The task container's own code never talks to S3 directly for this
     path: it reads `/data/input/*` and writes `/data/output/*` on local
     disk. Confirmed by `CORE/DockerContainers/image-input/validate.py`,
     which resolves cog metadata via `GET {API_URL}/v1/workers/tasks/
     resolve-cog-hash/{cog_id}` and writes a manifest whose
     `location`/`file_path` are `<scheme>://<bucket>/<content_hash>`
     *references* (lines ~200-210) — not fetched bytes. A repo-wide grep
     for `boto3`/`minio.Minio`/S3-client imports across
     `CORE/DockerContainers/**/*.py` (image-input, LoRAExport, ReturnExport,
     RAGBuilder, etc.) turned up none: no node container ships its own S3
     client.
   - the k8s-reduction-engine design note (not shipped in CE) states the target K8s design the same way:
     "Inputs/outputs are staged via S3 (versitygw) exactly as today — the
     pod gets the same cog S3 creds + `API_URL` env; NO PVC needed for task
     I/O," and "Collect = outputs already pushed to S3 by the node (no
     change)".

4. **`API_URL` is likewise env, not config baked into an image or a mount.**
   `main.go` ("`APIURL` is passed so the executor can rewrite
   container_env.API_URL to..." — the in-cluster/reachable API address) and
   the default `API_URL = os.environ.get("API_URL", "http://api:80")` in
   every input-validator container.

**Conclusion:** a K8s task Job pod needs exactly three things injected as
`env`/`Secret`-backed env — S3 creds (`access_key`/`secret_key`/`endpoint`/
`bucket`/`session_token`), and `API_URL` — plus one `emptyDir` scratch volume
for `/data/input` and `/data/output` shared between whichever
initContainer/sidecar does the `mc cp` staging and the main task container.
No `PersistentVolumeClaim`. This is a straight port of the existing
Docker-volume-per-task pattern, not a new mechanism.

## The in-cluster allow-list the deny policy must keep open

Both policies (deny and user-egress) allow these three platform destinations,
because the pod's own I/O staging step needs them regardless of which egress
mode the *task's own code* got:

| Purpose | Destination | Port | Evidence |
|---|---|---|---|
| DNS (Service-name resolution) | cluster DNS (kube-dns/CoreDNS, `kube-system`) | 53/UDP, 53/TCP | standard; every other rule targets a Service by name |
| Credential mint + cog resolve | in-cluster API Service | 80 (container port; see `docker-compose.yml` `api:` service `127.0.0.1:8000:80`) | `worker_tasks.py` `/workers/tasks/{id}/credentials`, `/v1/workers/tasks/resolve-cog-hash/{id}` |
| Cog input/output staging | in-cluster versitygw Service | 7070 (S3 data port; `docker-compose.yml` `versitygw:` `--port :7070`) | `executor.go` `downloadSidecar`/`uploadSidecar` via `MC_HOST_myminio` |

A fourth destination (k8s-reduction-engine W10) lets stack-consuming nodes
(VLM/SDXL, which get the stack's Service DNS injected as e.g. `VLM_URL`) reach
**stack pods** (`mod.techlabs/stack` label, any port): in per-tenant mode any
namespace labelled `app.kubernetes.io/managed-by=mod` (the API-created
`tenant-<id>` namespaces); in single-namespace mode the namespace named by
`MOD_STACK_NAMESPACE` (switch the commented selector line). Egress cannot scope
this per tenant from the shared task namespace — the stack side's
`mod-tenant-isolation` ingress policy (W9) admits only task pods whose
`mod.tech/tenant-id` matches. Single-namespace mode has no such boundary.

Explicitly **not** allow-listed for task pods: the versitygw **admin** port
7071 (`--admin-port :7071` in `docker-compose.yml`) — that's called only by
the API itself when minting per-tenant credentials
(`utils/storage_iam.py`), never by a task pod — and every other platform
service (Vault, Keycloak, Postgres, Valkey/Redis). Neither policy opens
those; `user-egress.yaml`'s catch-all rule explicitly excludes the cluster's
Pod/Service CIDRs so "outbound allowed" can't be used to reach them by IP
either.

## Why the deny policy still allows API + S3

Docker's `userNetwork` (true default-deny) container reaches *zero* outbound
sockets, full stop — including the platform API — because the S3 staging
that populates its `/data` volume runs in a **separate mc-sidecar container
attached to the worker's own main/platform Docker network**
(`e.network`, see `executor.go`
`NetworkMode: container.NetworkMode(e.network)`, and the call site
`SelectNetworkMode(payload, e.network, e.userNetwork, e.egressNetwork)` at
the sidecar never goes through `SelectNetworkMode` at all).
Each Docker *container* gets its own network attachment, so the untrusted
main container and the trusted staging sidecar can sit on two different
networks even though they share a volume.

Kubernetes `NetworkPolicy` has no per-container equivalent: it selects a
*Pod*, and every container in that pod (initContainers, sidecars, the main
container) shares one pod IP and one set of applicable NetworkPolicies. If
the K8s Job pod does its S3 staging in-pod (an initContainer for download,
a container for upload — the natural port of the sidecar pattern into one
Job pod, per the k8s-reduction-engine design note (not shipped in CE)'s "the pod gets the same cog S3 creds + `API_URL`
env"), then whatever egress rule permits that staging traffic necessarily
also applies to the main task container's network namespace.

Given that, `default-deny-egress.yaml` allows the minimum a pod's own
staging step needs (DNS + API + S3-data-port) and nothing else — the closest
achievable equivalent to Docker's guarantee at K8s's coarser (pod, not
container) enforcement granularity. The task's own code still cannot reach
Vault, Keycloak, Postgres, another tenant's pods, or the internet. If a
future task-pod design genuinely needs Docker's stricter "zero platform
access, not even S3, for this container" property, that requires splitting
stage → run → collect into separate Jobs/pods with different NetworkPolicies
per pod (so the untrusted run-pod carries no S3/API allow at all) rather than
one pod with mixed-trust containers — flagging this here for T2's Job-shape
design rather than deciding it in this task.

## Rendering

Example (`envsubst`, illustrative — the real render step belongs to
T2/T3/T8):

```bash
export MOD_TASK_NAMESPACE=tenant-42
export MOD_TASK_POD_SELECTOR_KEY=mod.tech/task-pod
export MOD_TASK_POD_SELECTOR_VALUE="true"
export MOD_DNS_NAMESPACE=kube-system
export MOD_API_NAMESPACE=mod-platform
export MOD_API_POD_SELECTOR_KEY=app.kubernetes.io/name
export MOD_API_POD_SELECTOR_VALUE=mod-api
export MOD_API_PORT=80
export MOD_S3_NAMESPACE=mod-platform
export MOD_S3_POD_SELECTOR_KEY=app.kubernetes.io/name
export MOD_S3_POD_SELECTOR_VALUE=versitygw
export MOD_S3_DATA_PORT=7070
export MOD_CLUSTER_POD_CIDR=10.244.0.0/16
export MOD_CLUSTER_SERVICE_CIDR=10.96.0.0/12
export MOD_STACK_NAMESPACE=mod-stacks   # single-namespace mode only

envsubst < default-deny-egress.yaml | kubectl apply -f -
envsubst < user-egress.yaml        | kubectl apply -f -
```

## Validation

Both files parse as valid YAML once rendered (checked with PyYAML against
the templates directly — `${VAR}` placeholders parse fine as plain scalar
strings, so this validates structure/indentation, not final values):

```bash
python3 -c "
import yaml
for f in ('default-deny-egress.yaml', 'user-egress.yaml'):
    with open(f) as fh:
        yaml.safe_load(fh)
    print(f, 'OK')
"
```
