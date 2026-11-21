#!/usr/bin/env bash
# k8s-reduction-engine T12 — run the K8s backend integration suites against a
# real cluster (normally Docker Desktop's built-in Kubernetes).
#
#   1. creates (or reuses) a throwaway namespace
#   2. renders CORE/Worker/k8s/networkpolicy/*.yaml (envsubst) into it
#   3. runs the Go K8sBackend suite   (go test -tags k8sintegration)
#   4. runs the Py K8sStackBackend suite (pytest, MOD_K8S_INTEGRATION=1)
#   5. deletes the namespace (unless KEEP_NS=1)
#
# Idempotent: re-running reuses/re-applies everything; cleanup runs on any exit.
#
# Env overrides:
#   KUBECONFIG                 kubeconfig (default ~/.kube/config)
#   KUBE_CONTEXT               kubectl context (default docker-desktop if it exists)
#   MOD_K8S_IT_NAMESPACE       namespace (default mod-it-<rand>)
#   KEEP_NS=1                  keep the namespace after the run
#   SKIP_GO=1 / SKIP_PY=1      skip a suite
#   MOD_IT_CILIUM=1            GB.EGR.8a: kind cluster (disableDefaultCNI) +
#                              Cilium, then the Go -run IntegrationCilium FQDN
#                              egress suite only
#   GO_RUNNER=docker|local     docker = golang:1.25 container (default if no `go`)
#   PY_RUNNER=local|<container> pytest locally (default) or `docker exec <container>`
#   PY_EXTRA_ARGS              extra pytest args (e.g. --noconftest)
#   MOD_CLUSTER_POD_CIDR       default 10.1.0.0/16   (Docker Desktop)
#   MOD_CLUSTER_SERVICE_CIDR   default 10.96.0.0/12  (Docker Desktop)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
API_DIR="$(cd "${WORKER_DIR}/../API" && pwd)"
NETPOL_DIR="${WORKER_DIR}/k8s/networkpolicy"

log() { printf '[k8s-it] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

command -v kubectl >/dev/null || die "kubectl not found"
command -v envsubst >/dev/null || die "envsubst not found (apt install gettext-base)"

KCTL=(kubectl)
if [[ -n "${KUBE_CONTEXT:-}" ]]; then
  KCTL+=(--context "${KUBE_CONTEXT}")
elif kubectl config get-contexts -o name 2>/dev/null | grep -qx docker-desktop; then
  KUBE_CONTEXT=docker-desktop
  KCTL+=(--context docker-desktop)
fi

# ---- GB.EGR.8a: opt-in Cilium FQDN-egress suite (kind cluster + Cilium) ----
# Self-contained: builds a throwaway kind cluster with Cilium as the only
# CNI (disableDefaultCNI: true), installs Cilium, runs ONLY the Go
# -run IntegrationCilium suite against it, and always deletes the cluster on
# exit. The default path below is untouched.
if [[ "${MOD_IT_CILIUM:-0}" == "1" ]]; then
  command -v kind >/dev/null || die "MOD_IT_CILIUM=1 but the kind CLI is not on PATH — install kind (https://kind.sigs.k8s.io/) and retry"
  command -v cilium >/dev/null || die "MOD_IT_CILIUM=1 but the cilium CLI is not on PATH — install the Cilium CLI (https://docs.cilium.io/en/stable/gettingstarted/install/) and retry"

  KIND_CLUSTER="mod-it-cilium-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  KIND_CONFIG="$(mktemp)"
  KIND_KUBECONFIG="$(mktemp)"
  kind_cleanup() {
    log "deleting kind cluster ${KIND_CLUSTER}"
    kind delete cluster --name "${KIND_CLUSTER}" >/dev/null 2>&1 || true
    rm -f "${KIND_CONFIG}" "${KIND_KUBECONFIG}"
  }
  trap kind_cleanup EXIT

  log "kind cluster ${KIND_CLUSTER} (Cilium as sole CNI)"
  cat > "${KIND_CONFIG}" <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
EOF
  kind create cluster --name "${KIND_CLUSTER}" --config "${KIND_CONFIG}" \
    || die "kind create cluster ${KIND_CLUSTER} failed"
  kind export kubeconfig --name "${KIND_CLUSTER}" > "${KIND_KUBECONFIG}"
  chmod 600 "${KIND_KUBECONFIG}"

  log "cilium install --wait"
  KUBECONFIG="${KIND_KUBECONFIG}" cilium install --wait \
    || die "cilium install --wait failed — Cilium agents did not become ready"

  log "Go Cilium FQDN-egress suite"
  GO_CMD='go test -tags k8sintegration ./internal/executor/ -run IntegrationCilium -count=1 -v'
  GO_RUNNER="${GO_RUNNER:-$(command -v go >/dev/null && echo local || echo docker)}"
  rc=0
  if [[ "${GO_RUNNER}" == "local" ]]; then
    (cd "${WORKER_DIR}" && KUBECONFIG="${KIND_KUBECONFIG}" MOD_IT_CILIUM=1 bash -c "${GO_CMD}") || rc=1
  else
    # --network host so the kind apiserver at 127.0.0.1 resolves as on the host.
    docker run --rm --network host \
      -v "${WORKER_DIR}:/src" -w /src -v modgocache:/go/pkg/mod \
      -v "${KIND_KUBECONFIG}:/kubeconfig:ro" -e KUBECONFIG=/kubeconfig \
      -e MOD_IT_CILIUM=1 \
      golang:1.25 sh -c "${GO_CMD}" || rc=1
  fi
  if [[ "${rc}" -eq 0 ]]; then log "PASS"; else log "FAIL"; fi
  exit "${rc}"
fi

log "probing cluster (${KUBE_CONTEXT:-current context})"
if ! timeout 15 "${KCTL[@]}" get ns >/dev/null 2>&1; then
  die "cluster not reachable — enable Kubernetes in Docker Desktop (Settings > Kubernetes) and retry"
fi

# Resolve the kubeconfig the suites should use; pin the chosen context so the
# Go/Py loaders (which use current-context) hit the same cluster.
KUBECONFIG_SRC="${KUBECONFIG:-${HOME}/.kube/config}"
TMP_DIR="$(mktemp -d)"
IT_KUBECONFIG="${TMP_DIR}/kubeconfig"
"${KCTL[@]}" config view --raw --minify --flatten > "${IT_KUBECONFIG}"
chmod 600 "${IT_KUBECONFIG}"

NS="${MOD_K8S_IT_NAMESPACE:-mod-it-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
export MOD_K8S_IT_NAMESPACE="${NS}"

cleanup() {
  local rc=$?
  if [[ "${KEEP_NS:-0}" != "1" ]]; then
    log "deleting namespace ${NS}"
    "${KCTL[@]}" delete namespace "${NS}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  else
    log "KEEP_NS=1 — leaving namespace ${NS}"
  fi
  rm -rf "${TMP_DIR}"
  exit "${rc}"
}
trap cleanup EXIT

log "namespace ${NS}"
"${KCTL[@]}" create namespace "${NS}" --dry-run=client -o yaml | "${KCTL[@]}" apply -f - >/dev/null
"${KCTL[@]}" label namespace "${NS}" mod.tech/it=true --overwrite >/dev/null

# ---- render + apply NetworkPolicies ----------------------------------------
export MOD_TASK_NAMESPACE="${NS}"
export MOD_TASK_POD_SELECTOR_KEY="${MOD_TASK_POD_SELECTOR_KEY:-mod.tech/task-pod}"
export MOD_TASK_POD_SELECTOR_VALUE="${MOD_TASK_POD_SELECTOR_VALUE:-true}"  # manifests quote the value
export MOD_DNS_NAMESPACE="${MOD_DNS_NAMESPACE:-kube-system}"
export MOD_API_NAMESPACE="${MOD_API_NAMESPACE:-${NS}}"
export MOD_API_POD_SELECTOR_KEY="${MOD_API_POD_SELECTOR_KEY:-app}"
export MOD_API_POD_SELECTOR_VALUE="${MOD_API_POD_SELECTOR_VALUE:-mod-api}"
export MOD_API_PORT="${MOD_API_PORT:-80}"
export MOD_S3_NAMESPACE="${MOD_S3_NAMESPACE:-${NS}}"
export MOD_S3_POD_SELECTOR_KEY="${MOD_S3_POD_SELECTOR_KEY:-app}"
export MOD_S3_POD_SELECTOR_VALUE="${MOD_S3_POD_SELECTOR_VALUE:-versitygw}"
export MOD_S3_DATA_PORT="${MOD_S3_DATA_PORT:-7070}"
export MOD_CLUSTER_POD_CIDR="${MOD_CLUSTER_POD_CIDR:-10.1.0.0/16}"
export MOD_CLUSTER_SERVICE_CIDR="${MOD_CLUSTER_SERVICE_CIDR:-10.96.0.0/12}"

# Explicit variable list: only these are substituted (comments mention ${VAR}).
SUBST_VARS='${MOD_TASK_NAMESPACE} ${MOD_TASK_POD_SELECTOR_KEY} ${MOD_TASK_POD_SELECTOR_VALUE}
${MOD_DNS_NAMESPACE} ${MOD_API_NAMESPACE} ${MOD_API_POD_SELECTOR_KEY} ${MOD_API_POD_SELECTOR_VALUE}
${MOD_API_PORT} ${MOD_S3_NAMESPACE} ${MOD_S3_POD_SELECTOR_KEY} ${MOD_S3_POD_SELECTOR_VALUE}
${MOD_S3_DATA_PORT} ${MOD_CLUSTER_POD_CIDR} ${MOD_CLUSTER_SERVICE_CIDR}'

for f in "${NETPOL_DIR}"/*.yaml; do
  out="${TMP_DIR}/$(basename "${f}")"
  envsubst "${SUBST_VARS}" < "${f}" > "${out}"
  if grep -v '^[[:space:]]*#' "${out}" | grep -q '\${MOD_'; then
    die "unrendered placeholder left in $(basename "${f}")"
  fi
  log "apply $(basename "${f}")"
  "${KCTL[@]}" apply -n "${NS}" -f "${out}" >/dev/null
done

# ---- Go suite ----------------------------------------------------------------
rc=0
if [[ "${SKIP_GO:-0}" != "1" ]]; then
  GO_RUNNER="${GO_RUNNER:-$(command -v go >/dev/null && echo local || echo docker)}"
  log "Go suite (${GO_RUNNER})"
  GO_CMD='go test -tags k8sintegration ./internal/executor/ -run Integration -count=1 -v'
  if [[ "${GO_RUNNER}" == "local" ]]; then
    (cd "${WORKER_DIR}" && KUBECONFIG="${IT_KUBECONFIG}" bash -c "${GO_CMD}") || rc=1
  else
    # --network host so kubernetes.docker.internal / 127.0.0.1:6443 resolve as on the host.
    docker run --rm --network host \
      -v "${WORKER_DIR}:/src" -w /src -v modgocache:/go/pkg/mod \
      -v "${IT_KUBECONFIG}:/kubeconfig:ro" -e KUBECONFIG=/kubeconfig \
      -e MOD_K8S_IT_NAMESPACE="${NS}" \
      golang:1.25 sh -c "${GO_CMD}" || rc=1
  fi
fi

# ---- Python suite ------------------------------------------------------------
if [[ "${SKIP_PY:-0}" != "1" ]]; then
  PY_RUNNER="${PY_RUNNER:-local}"
  log "Py suite (${PY_RUNNER})"
  PY_CMD=(python3 -m pytest tests/integration/test_k8s_stack_integration.py -q -rs -p no:cacheprovider)
  # e.g. PY_EXTRA_ARGS=--noconftest in a slim container without the app deps
  # (tests/conftest.py imports models/sqlalchemy; this suite needs none of it).
  # shellcheck disable=SC2206
  [[ -n "${PY_EXTRA_ARGS:-}" ]] && PY_CMD+=(${PY_EXTRA_ARGS})
  if [[ "${PY_RUNNER}" == "local" ]]; then
    (cd "${API_DIR}" && MOD_K8S_INTEGRATION=1 MOD_K8S_IT_KUBECONFIG="${IT_KUBECONFIG}" \
      KUBECONFIG="${IT_KUBECONFIG}" "${PY_CMD[@]}") || rc=1
  else
    docker cp "${IT_KUBECONFIG}" "${PY_RUNNER}:/tmp/mod-it-kubeconfig"
    docker exec -e MOD_K8S_INTEGRATION=1 -e MOD_K8S_IT_NAMESPACE="${NS}" \
      -e MOD_K8S_IT_KUBECONFIG=/tmp/mod-it-kubeconfig -e KUBECONFIG=/tmp/mod-it-kubeconfig \
      "${PY_RUNNER}" "${PY_CMD[@]}" || rc=1
    docker exec "${PY_RUNNER}" rm -f /tmp/mod-it-kubeconfig || true
  fi
fi

if [[ "${rc}" -eq 0 ]]; then log "PASS"; else log "FAIL"; fi
exit "${rc}"
