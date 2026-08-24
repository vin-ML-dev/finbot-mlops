# Finbot MLOps — Components 0 and 1

This project builds the local Kubernetes foundation for Finbot and enables GitOps deployments with Argo CD.

## Components

### Component 0 — Kubernetes cluster

Terraform creates a three-node kind cluster:

| Node | Label | Purpose |
| --- | --- | --- |
| `finbot-control-plane` | `node-role=infra` | Argo CD, gateway, Redis and monitoring |
| `finbot-worker` | `node-role=model` | llama.cpp and the GGUF model |
| `finbot-worker2` | `node-role=agent` | LangGraph monitoring agent |

The model and agent nodes receive taints so unrelated pods cannot run on them.

### Component 1 — Argo CD GitOps

Argo CD watches the Git repository and keeps Kubernetes synchronized with it.

The project uses the App-of-Apps pattern:

```text
root application
  -> reads Application files from argocd/apps/
  -> each child Application points to its workload manifests
  -> Argo CD deploys those manifests to Kubernetes
```

Component 1 initially creates the `finbot` namespace. Later components add Redis, monitoring, model, gateway and agent Applications.

## Repository structure

```text
finbot-mlops/
├── infra/
│   ├── kind/kind-cluster.yaml
│   ├── scripts/
│   │   ├── cluster-up.sh
│   │   ├── cluster-down.sh
│   │   └── label-nodes.sh
│   └── terraform/
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       └── versions.tf
├── argocd/
│   ├── apps/namespace.yaml
│   ├── bootstrap/
│   │   ├── install.sh
│   │   ├── root-app.yaml
│   │   └── argocd-install.md
│   └── projects/finbot-project.yaml
└── deploy/base/namespace/
    ├── kustomization.yaml
    └── namespace.yaml
```

## Prerequisites

- Docker with approximately 10–12 GB RAM available
- kind
- kubectl
- Terraform 1.5 or newer
- A Git repository accessible to Argo CD

## 1. Create the cluster

```bash
cd infra/scripts
./cluster-up.sh
```

The script initializes Terraform, creates the cluster, and applies the model and agent node taints.

Verify the nodes:

```bash
kubectl --context kind-finbot get nodes -o wide
```

Verify labels and taints:

```bash
kubectl --context kind-finbot get nodes \
  -L node-role \
  -o custom-columns='NODE:.metadata.name,ROLE:.metadata.labels.node-role,TAINTS:.spec.taints[*].key'
```

All three nodes should be `Ready`.

> The `tehcyx/kind` provider expects an inline `kind_config` block in `main.tf`; it does not support `kind_config_path`.

## 2. Configure the Git repository

Replace the example repository URL with your real Git URL in:

- `argocd/bootstrap/root-app.yaml`
- `argocd/projects/finbot-project.yaml`
- `argocd/apps/namespace.yaml`

Commit and push the files before installing Argo CD. Argo CD reads from Git, not from local files on the laptop.

## 3. Install Argo CD

```bash
cd argocd/bootstrap
./install.sh
```

The script:

1. Creates the `argocd` namespace.
2. Installs Argo CD.
3. Waits for the Argo CD server.
4. Applies the Finbot AppProject.
5. Applies the root Application.

## 4. Verify GitOps

```bash
kubectl --context kind-finbot -n argocd get pods
kubectl --context kind-finbot -n argocd get appproject finbot
kubectl --context kind-finbot -n argocd get applications
kubectl --context kind-finbot get namespace finbot
```

Check the namespace Application:

```bash
kubectl --context kind-finbot -n argocd get application namespace \
  -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
```

Expected result:

```text
Synced Healthy
```

## Argo CD UI

Start port forwarding:

```bash
kubectl --context kind-finbot -n argocd port-forward svc/argocd-server 8081:443
```

Open `https://localhost:8081` and log in as `admin`.

Get the initial password:

```bash
kubectl --context kind-finbot -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

## GitOps workflow

For an existing component:

1. Change its manifests under `deploy/` or its Helm configuration.
2. Commit and push.
3. Argo CD detects and deploys the change.

For a new component:

1. Add its workload manifests under `deploy/` or configure its Helm chart.
2. Add its child Application under `argocd/apps/`.
3. Commit and push.
4. Argo CD registers and deploys it.

Paths not referenced by an Argo CD Application are ignored.

## Delete the local environment

Because Terraform created the cluster, destroy it through Terraform:

```bash
cd infra/scripts
./cluster-down.sh
```

This deletes the kind cluster, including Argo CD and all workloads inside it.
