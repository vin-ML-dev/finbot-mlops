# Finbot MLOps — Components 0 to 3

This project builds the local Kubernetes foundation for Finbot, enables GitOps deployments with Argo CD, protects secrets in Git, and provides Redis as a shared state store.

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

Component 1 initially creates the `finbot` namespace. The root Application then discovers the Sealed Secrets and Redis child Applications from Git.

### Component 2 — Sealed Secrets

Sealed Secrets allows encrypted credentials to be stored safely in Git.

The component contains two Argo CD Applications:

- `sealed-secrets-controller` installs the controller and `SealedSecret` CRD from the vendored static manifest under `deploy/base/sealed-secrets/`.
- `sealed-secret-manifests` deploys encrypted SealedSecret files from `secrets/sealed/`.

The secret flow is:

```text
Plain Kubernetes Secret kept locally
  -> kubeseal encrypts it with the controller's public key
  -> encrypted SealedSecret is committed to Git
  -> Argo CD deploys it
  -> controller decrypts it inside Kubernetes
  -> pods use the generated Kubernetes Secret
```

Never commit plaintext secrets or `.env` files containing credentials.

### Component 3 — Redis

Redis provides shared state for the gateway and monitoring agent.

It runs as a one-replica StatefulSet with:

- Stable pod identity: `redis-0`
- A 1 GiB PersistentVolumeClaim mounted at `/data`
- A headless Service named `redis` for StatefulSet networking
- A ClusterIP Service named `redis-svc` for client connections on port `6379`
- Preferred node affinity for the node labeled `node-role=infra`

This setup is persistent but not highly available. Production HA would require replication and automatic failover.

### Where you stand now

You have a working foundation:

- ✅ Component 0 — 3-node cluster
- ✅ Component 1 — Argo CD GitOps
- ✅ Component 2 — Sealed Secrets controller and decrypted Kubernetes Secrets
- ✅ Component 3 — Redis stateful store

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
│   ├── apps/
│   │   ├── namespace.yaml
│   │   ├── sealed-secrets-controller.yaml
│   │   ├── sealed-secret-manifests.yaml
│   │   └── redis.yaml
│   ├── bootstrap/
│   │   ├── install.sh
│   │   ├── root-app.yaml
│   │   └── argocd-install.md
│   └── projects/finbot-project.yaml
├── secrets/
│   ├── examples/
│   ├── seal-secret.sh
│   └── sealed/
│       └── kustomization.yaml
└── deploy/base/
    ├── namespace/
    │   ├── kustomization.yaml
    │   └── namespace.yaml
    ├── sealed-secrets/
    │   └── controller.yaml
    └── redis/
        ├── kustomization.yaml
        ├── service.yaml
        └── statefulset.yaml
```

## Prerequisites

- Docker with at least 12 GB RAM available
- kind
- kubectl
- Terraform 1.5 or newer
- kubeseal CLI
- A Git repository accessible to Argo CD

Push the repository to Git before installing Argo CD. Argo CD reads from Git, not from files on your laptop.

> A fresh cluster receives a new Sealed Secrets key. Re-seal your secrets each time you recreate the cluster. Keep plaintext secrets safely outside the Git repository.

## Component 0 — Create the cluster

```bash
cd infra/terraform
terraform init
terraform apply -auto-approve
```

`terraform init` is required only the first time or after provider configuration changes.

Reserve the worker nodes:

```bash
kubectl taint node finbot-worker dedicated=model:NoSchedule --overwrite
kubectl taint node finbot-worker2 dedicated=agent:NoSchedule --overwrite
```

The control-plane node must remain schedulable for Argo CD and infrastructure. Remove its default taint if present:

```bash
kubectl taint node finbot-control-plane \
  node-role.kubernetes.io/control-plane- 2>/dev/null || true
```

Verify the nodes, labels and taints:

```bash
kubectl get nodes \
  -L node-role \
  -o custom-columns='NODE:.metadata.name,ROLE:.metadata.labels.node-role,TAINTS:.spec.taints[*].key'
```

Expected:

- `finbot-control-plane`: role `infra`, no taint
- `finbot-worker`: role `model`, taint `dedicated`
- `finbot-worker2`: role `agent`, taint `dedicated`

> The `tehcyx/kind` provider expects an inline `kind_config` block in `main.tf`; it does not support `kind_config_path`.

## Component 1 — Install Argo CD

Replace the example repository URL with your real Git URL in:

- `argocd/bootstrap/root-app.yaml`
- `argocd/projects/finbot-project.yaml`
- All Git-based Application files under `argocd/apps/`

Commit and push the files before installing Argo CD. Argo CD reads from Git, not from local files on the laptop.

Create the namespace and install Argo CD:

```bash
kubectl create namespace argocd

# Server-side apply avoids the "annotations: Too long" CRD error.
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
```

Hand deployment control to Git:

```bash
kubectl apply -f argocd/projects/finbot-project.yaml
kubectl apply -f argocd/bootstrap/root-app.yaml
```

Verify Argo CD and GitOps:

```bash
kubectl -n argocd get pods
kubectl -n argocd get appproject finbot
kubectl -n argocd get applications
kubectl get namespace finbot
```

Check the namespace Application:

```bash
kubectl -n argocd get application namespace \
  -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
```

Expected result:

```text
Synced Healthy
```

### Argo CD UI

Get the initial password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

Start port forwarding:

```bash
kubectl -n argocd port-forward svc/argocd-server 8081:443
```

Open `https://localhost:8081` and log in as `admin`.

## Component 2 — Install Sealed Secrets

Install `kubeseal` once per laptop on Linux AMD64:

```bash
KUBESEAL_VERSION=0.27.1

curl -fsSL -o kubeseal.tar.gz \
  "https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz"

tar xzf kubeseal.tar.gz kubeseal
sudo install kubeseal /usr/local/bin/
rm kubeseal kubeseal.tar.gz

kubeseal --version
```

Push the Component 2 Applications, project changes, static controller manifest and secrets structure:

```bash
git add argocd/ deploy/base/sealed-secrets/ secrets/
git commit -m "Component 2: Sealed Secrets"
git push

# AppProject changes require a manual apply.
kubectl apply -f argocd/projects/finbot-project.yaml

# Make the root app read Git immediately.
kubectl -n argocd patch application finbot-root --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

The controller uses the vendored manifest at `deploy/base/sealed-secrets/controller.yaml`; it does not download a Helm or OCI chart.

Confirm that the controller and CRD exist:

```bash
kubectl get pods -A | grep -i sealed
kubectl get crd sealedsecrets.bitnami.com
```

### Seal the application secrets

Create plaintext Secrets outside the repository. Example gateway Secret:

```bash
cat > /tmp/gateway-secret.yaml <<'YAML'
apiVersion: v1
kind: Secret
metadata: { name: gateway-secret, namespace: finbot }
type: Opaque
stringData: { GATEWAY_API_KEY: "your-real-strong-key" }
YAML

cd secrets
./seal-secret.sh /tmp/gateway-secret.yaml sealed/gateway-sealed.yaml
rm /tmp/gateway-secret.yaml
cd ..
```

If your plaintext files are stored in the project-level `tmp/` folder, run from `secrets/`:

```bash
./seal-secret.sh ../../tmp/model-secret.yaml sealed/model-sealed.yaml
./seal-secret.sh ../../tmp/agent-secret.yaml sealed/agent-sealed.yaml
./seal-secret.sh ../../tmp/gateway-secret.yaml sealed/gateway-sealed.yaml
```

Do not seal the Alertmanager Slack Secret yet. It targets the `monitoring` namespace, which arrives with Component 4.

List encrypted files in `secrets/sealed/kustomization.yaml`. Use a YAML list, not `resources: []` followed by list items:

```yaml
resources:
  - model-sealed.yaml
  - gateway-sealed.yaml
  - agent-sealed.yaml
  # - alertmanager-slack-sealed.yaml  # enable with Component 4
```

Commit only the encrypted files:

```bash
git add secrets/sealed/
git commit -m "Component 2: sealed secrets"
git push

kubectl -n argocd patch application sealed-secret-manifests --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

Verify the decrypted Kubernetes Secrets:

```bash
kubectl -n finbot get secrets
```

Expected Secrets include `gateway-secret`, `model-secret`, and `agent-secret`.

## Component 3 — Deploy Redis

Push the Redis manifests and child Application:

```bash
git add deploy/base/redis/ argocd/apps/redis.yaml
git commit -m "Component 3: Redis stateful store"
git push

kubectl -n argocd patch application finbot-root --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

Verify Redis:

```bash
kubectl -n argocd get application redis \
  -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'

kubectl -n finbot get pods -l app=redis -o wide
kubectl -n finbot get pvc
kubectl -n finbot exec -it redis-0 -- redis-cli ping
```

Expected:

- Redis Application: `Synced Healthy`
- Pod: `redis-0` running on the infra node
- PVC: `data-redis-0` in `Bound` state
- Redis response: `PONG`

### Optional persistence test

```bash
kubectl -n finbot exec -it redis-0 -- redis-cli set canary hello
kubectl -n finbot delete pod redis-0
kubectl -n finbot exec -it redis-0 -- redis-cli get canary
```

The final command should return:

```text
hello
```

## Useful Argo CD commands

### Force an Application to refresh now

```bash
kubectl -n argocd patch application <app-name> --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

You can also click **Refresh** or **Sync** in the Argo CD UI.

### Check all Applications

```bash
kubectl -n argocd get applications \
  -o custom-columns='APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
```

### Display an Application error

```bash
kubectl -n argocd get application <app-name> \
  -o jsonpath='{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}'
```

## GitOps workflow

For an existing component:

1. Change its manifests under `deploy/`.
2. Commit and push.
3. Argo CD detects and deploys the change.

For a new component:

1. Add its manifests under `deploy/`.
2. Add its child Application under `argocd/apps/`.
3. Commit and push.
4. Argo CD registers and deploys it.

Paths not referenced by an Argo CD Application are ignored.

## Stop and recreate the environment

Destroy the cluster to release Docker memory:

```bash
cd infra/terraform
terraform destroy -auto-approve
```

A new cluster has no Argo CD installation and receives a new Sealed Secrets key. To start again, repeat Components 0 through 3 and re-seal the secrets.

### Recovery when Terraform destroy fails

Use this only for a broken kind cluster that Terraform cannot destroy normally:

```bash
kind delete cluster --name finbot
docker ps -a --filter "label=io.x-k8s.kind.cluster=finbot" -q \
  | xargs -r docker rm -f
terraform state rm kind_cluster.finbot 2>/dev/null
terraform apply -auto-approve
```

The final `terraform apply` recreates the cluster; it is not part of deletion-only cleanup.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Terraform configuration change is not reflected | Some kind settings apply only during cluster creation; recreate the cluster. |
| Argo CD install reports `annotations: Too long` | Use `kubectl apply --server-side --force-conflicts`. |
| All Argo CD pods remain `Pending` | Remove the control-plane `NoSchedule` taint. |
| `terraform destroy` fails while removing Docker containers | Use the broken-cluster recovery commands above. |
| Terraform reports that nodes already exist | Remove leftover kind containers before applying again. |
| Sealed Secrets chart returns `404` or OCI `403` | Use the vendored static controller manifest instead of Helm/OCI. |
| Kustomize reports `did not find expected key` | Replace `resources: []` with a normal YAML resource list. |
| Argo CD still shows an old commit | Hard-refresh the Application or use Refresh in the UI. |
| `sealed-secret-manifests` is `OutOfSync` | Keep the Alertmanager Secret disabled until the `monitoring` namespace exists. |
| `redis-0` remains `Pending` | Check `kubectl get storageclass`; kind normally provides the `standard` StorageClass. |

## Done through Component 3

- Three nodes are `Ready`: infra control-plane without a taint, model and agent workers with `dedicated` taints.
- Argo CD Applications are `Synced` and `Healthy`, except the intentionally deferred Alertmanager Secret.
- `kubectl -n finbot get secrets` lists decrypted gateway, model and agent Secrets.
- `redis-0` is running on the infra node, its PVC is `Bound`, and `redis-cli ping` returns `PONG`.

Next: **Component 4 — monitoring** with Prometheus, Alertmanager and Grafana. It creates the `monitoring` namespace and allows the deferred Alertmanager Secret to decrypt.
