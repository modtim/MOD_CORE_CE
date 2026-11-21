---
sidebar_position: 5.7
title: Kubernetes (k3s) setup for MOD CE
description: Set up a single-node k3s cluster, register it once in MOD Community Edition, and run node tasks as Kubernetes Jobs.
---

# Kubernetes (k3s) setup for MOD CE

MOD can run node tasks on a Kubernetes cluster instead of on the worker's own
container runtime. For **Community Edition** the shape is deliberate: **whole-GPU
only on the nodes you run**. This page uses
[k3s](https://docs.k3s.io/), a single-node distribution that
runs on one Linux host.

## What you get

- **Clusters you own.** A CE install can register more than one cluster: the cluster
  registry has no count limit on a self-hosted install. k3s itself is a single-node
  distribution, so one k3s install gives you one cluster — add another cluster by
  installing another Kubernetes on another host.
- Node tasks run as Kubernetes **Jobs** (one pod each) in the `mod-tasks` namespace.
  The card shows the name, an Active badge, the mode (in-cluster or kubeconfig
  reference), and the probe summary. Everything else — the DAG, the task queue, the
  worker heartbeat — is unchanged from the [worker setup](worker-setup.md).

## Requirements

- **One Linux host.** A single node is all CE needs; k3s brings its own containerd, so
  Docker is not required for k3s itself.
- **The backend setting.** Kubernetes runs when `MOD_EXECUTOR_BACKEND` / `MOD_STACK_BACKEND`
  is set to `k8s`; without that setting the effective backend stays `docker`. The Kubernetes
  backend is enabled with the `k8s` setting alone, so nothing extra to set up on CE — do not
  try to force the backend with any other variable.
- **GPU is optional.** Whole-GPU tasks need the NVIDIA container toolkit on the host plus the
  NVIDIA device plugin on the cluster; fractional GPU is out of scope ([Limits today](#limits-today)).

## Install k3s

Follow the k3s single-node install instructions and **pin the release** — pass
`INSTALL_K3S_VERSION=<k3s-version>` so the install is reproducible, and do not run an
unpinned install script. The kubeconfig lands at `/etc/rancher/k3s/k3s.yaml`; copy it to a
private file (mode `600`) and point `KUBECONFIG` at that copy. `kubectl get nodes -o wide`
then shows one node in state Ready. Verified on a single-node k3s install.

## Create `mod-tasks` and apply the worker RBAC

```bash
kubectl create namespace mod-tasks
TENANT_NAMESPACE=mod-tasks envsubst < CORE/Worker/k8s/rbac.yaml | kubectl apply -f -
```

`CORE/Worker/k8s/rbac.yaml` grants permissions only and creates no namespace (so create it
first). Its Role is namespaced to the tenant namespace (`mod-tasks` in this guide) and grants
Jobs, Deployments, Services, Pods, Secrets and more there. The same manifest also binds
ClusterRoles cluster-wide through ClusterRoleBindings: nodes get/list; namespaces list,
deployments list/patch, pods list, and pods metrics get/list. The grant is therefore not
limited to one namespace. Smoke one task:

```bash
kubectl -n mod-tasks create job smoke --image=busybox:1.36 -- sh -c 'echo mod-k3s-ok'
kubectl -n mod-tasks logs job/smoke
kubectl -n mod-tasks delete job smoke
```

The Job reaches `Completed` and the log prints the line. Verified on a single-node k3s install.

## GPU (optional, whole-GPU only)

k3s detects the NVIDIA container toolkit and configures its own containerd runtime. Install
the device plugin from the standalone `nvidia/nvidia-device-plugin` Helm chart. Then:

```bash
kubectl get node -o jsonpath='{.items[0].status.allocatable}'   # shows nvidia.com/gpu
```

A Job with `limits: nvidia.com/gpu: 1` gets the whole card. Verified on a single-node k3s install.

## Store the kubeconfig in Vault, register, and probe

1. Put the kubeconfig file into the Vault system and keep **only a reference** to it — MOD
   never needs the raw file on the tenant side.
2. In the Dashboard, open **Settings -> Integrations -> Kubernetes clusters** and use **Add
   cluster**: the name, and the **Vault reference of the kubeconfig** field holding that reference. Check
   **or use the API process in-cluster ServiceAccount (no kubeconfig)** instead when MOD itself runs as pods in that cluster.
3. The form rejects anything that looks like kubeconfig content before it reaches the API, and
   the card only ever shows the reference. Never paste a kubeconfig into a chat, a ticket, or
   an issue.

Press **Probe** on the card. A green probe fills the cluster caps: GPU vendor, node count, and
GPU memory, with a probed-at timestamp. A failing probe shows one fixed message ("Probe failed - capabilities shown are a
conservative default."), never the raw cluster error.

## Optional: install MOD itself on the cluster

When MOD runs as pods in the cluster, the in-cluster ServiceAccount replaces the kubeconfig and
the chart renders its own least-privilege RBAC. Keep `worker.mode: executor`, and set the task-pod
NetworkPolicy values to your cluster's real pod and service ranges (`<pod-cidr>` / `<service-cidr>`;
the chart defaults are 10.244.0.0/16 (pods) and 10.96.0.0/12 (services), `deploy/helm/mod/templates/rbac.yaml:368-369`):

```bash
helm upgrade --install mod deploy/helm/mod -n mod --create-namespace \
  -f deploy/helm/mod/values-onprem.yaml \
  --set worker.taskPods.networkPolicy.clusterPodCIDR=<pod-cidr> \
  --set worker.taskPods.networkPolicy.clusterServiceCIDR=<service-cidr>
```

`mod-tasks` must already exist. The install command and the two NetworkPolicy value names
(`worker.taskPods.networkPolicy.clusterPodCIDR` / `clusterServiceCIDR`) come from the chart
(`deploy/helm/mod`, `templates/rbac.yaml`). This Helm step is not yet covered by the live k3s proof.

## Your organisation

MOD manages the k3s cluster(s) you register for your organisation. Clusters, RBAC and task
pods on this page are scoped to that organisation.

## Limits today

- **No cluster-count limit** in CE: a self-hosted install may register several clusters,
  each a Kubernetes you run yourself (k3s itself is one node per host).
- **Whole-GPU only** — a task takes a whole `nvidia.com/gpu` resource.

## Troubleshooting

- **Probe fails right after Add cluster** — the Vault reference does not resolve to a file the
  API can read, or the kubeconfig's user identity cannot list nodes.
- **Jobs stay pending and never start** — `mod-tasks` is missing, or
  `CORE/Worker/k8s/rbac.yaml` was never applied in it.
- **Task pods blocked from the network they need** — the `clusterPodCIDR` / `clusterServiceCIDR` NetworkPolicy values do not match the cluster's real ranges.
