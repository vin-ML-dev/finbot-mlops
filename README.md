# Finbot MLOps — Components 0 to 5

This project builds the local Kubernetes foundation for Finbot, enables GitOps deployments with Argo CD, protects secrets in Git, provides Redis as a shared state store, runs the Prometheus monitoring stack, and serves a fine-tuned model with llama.cpp.

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

Component 1 initially creates the `finbot` namespace. The root Application then discovers the remaining child Applications from Git.

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

### Component 4 — Monitoring stack

The `kube-prometheus-stack` Helm chart provides Prometheus, Alertmanager and (optionally) Grafana in a dedicated `monitoring` namespace.

The component contains two Argo CD Applications:

- `monitoring-namespace` (sync-wave 0) creates the `monitoring` namespace first, so both the stack and the deferred Alertmanager Secret have somewhere to land.
- `monitoring` (sync-wave 1) installs the chart through a multi-source Application: the chart comes from the `prometheus-community` Helm repository, while the values file lives in this repository at `helm/kube-prometheus-stack/values-kind.yaml` and is referenced with `$values`.

The values file carries the settings tuned for local kind:

- `fullnameOverride: kps` gives predictable service names (`kps-prometheus`, `kps-alertmanager`).
- `defaultRules.create: false` disables the noisy built-in alert rules.
- The ServiceMonitor and rule selectors are set so Prometheus also discovers this project's own ServiceMonitors and rules.
- Grafana is capped and can be disabled to save memory.
- The Alertmanager Slack Secret is mounted for the receiver used in Component 7.

Creating the `monitoring` namespace also lets the previously deferred Alertmanager Slack SealedSecret decrypt.

### Component 5 — Model serving

The fine-tuned model runs with llama.cpp on the model node (Node B).

It runs as a Deployment with:

- An init container that downloads the GGUF once into a PersistentVolumeClaim and skips the download if the file already exists, so a pod or node restart reuses the cache
- Node pinning to Node B: `nodeSelector: node-role=model` plus a toleration for the `dedicated=model` taint
- The `Recreate` strategy, because the model cache PVC is ReadWriteOnce and cannot be shared during a rolling update
- A generous startup probe, because loading the model is slow
- CPU-only inference tuned for local testing (`--ctx-size 1024`, `--threads 2`)
- A ClusterIP Service `llama-cpp-svc:8080` and a ServiceMonitor exposing `/metrics`

### Where you stand now

You have a working platform foundation and serving model:

- ✅ Component 0 — 3-node cluster
- ✅ Component 1 — Argo CD GitOps
- ✅ Component 2 — Sealed Secrets controller and decrypted Kubernetes Secrets
- ✅ Component 3 — Redis stateful store
- ✅ Component 4 — Monitoring stack (Prometheus and Alertmanager)
- ✅ Component 5 — Model serving with llama.cpp

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
│   │   ├── redis.yaml
│   │   ├── monitoring-namespace.yaml
│   │   ├── monitoring.yaml
│   │   └── model.yaml
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
├── helm/
│   └── kube-prometheus-stack/
│       └── values-kind.yaml
└── deploy/base/
    ├── namespace/
    │   ├── kustomization.yaml
    │   └── namespace.yaml
    ├── sealed-secrets/
    │   └── controller.yaml
    ├── redis/
    │   ├── kustomization.yaml
    │   ├── service.yaml
    │   └── statefulset.yaml
    ├── monitoring-namespace/
    │   ├── kustomization.yaml
    │   └── namespace.yaml
    └── model/
        ├── kustomization.yaml
        ├── pvc.yaml
        ├── deployment.yaml
        ├── service.yaml
        └── servicemonitor.yaml
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

## Component 4 — Deploy the monitoring stack

Re-enable the deferred Alertmanager Slack Secret now that the `monitoring` namespace will exist. Uncomment its line in `secrets/sealed/kustomization.yaml`:

```yaml
resources:
  - model-sealed.yaml
  - gateway-sealed.yaml
  - agent-sealed.yaml
  - alertmanager-slack-sealed.yaml
```

If you recreated the cluster since sealing, re-seal the Alertmanager Secret against the `monitoring` namespace.

Confirm the AppProject allows the `prometheus-community` Helm repository and the admission webhook resources the chart installs. The `clusterResourceWhitelist` must include:

```yaml
- group: admissionregistration.k8s.io
  kind: MutatingWebhookConfiguration
- group: admissionregistration.k8s.io
  kind: ValidatingWebhookConfiguration
```

Push the monitoring manifests and Applications:

```bash
git add helm/ deploy/base/monitoring-namespace/ \
        argocd/apps/monitoring.yaml argocd/apps/monitoring-namespace.yaml \
        argocd/projects/finbot-project.yaml \
        secrets/sealed/kustomization.yaml
git commit -m "Component 4: monitoring stack"
git push

# Project changes require a manual apply.
kubectl apply -f argocd/projects/finbot-project.yaml

kubectl -n argocd patch application finbot-root --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

The stack is large; the first sync installs CRDs and several pods and takes a few minutes.

Verify:

```bash
kubectl -n argocd get application monitoring \
  -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'

kubectl get namespace monitoring
kubectl -n monitoring get pods
kubectl -n monitoring get secret alertmanager-slack
```

Expected:

- `monitoring` Application: `Synced Healthy`
- Prometheus and Alertmanager pods `Running`
- The `alertmanager-slack` Secret present in the `monitoring` namespace

Reach the user interfaces. Use the `kps-` service names:

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 9090:9090     # http://localhost:9090
kubectl -n monitoring port-forward svc/kps-alertmanager 9093:9093   # http://localhost:9093
```

> Grafana is the heaviest single component and is not in the alert path. On a memory-constrained laptop, set `grafana.enabled: false` in `helm/kube-prometheus-stack/values-kind.yaml` to reclaim memory for the model. Prometheus and Alertmanager continue to provide the reliable alert path.

## Component 5 — Deploy the model

Confirm the GGUF filename matches your Hugging Face file. The default expects `finbot-qwen3-1.7b-baseline-q8_0.gguf`. It appears in two places in `deploy/base/model/deployment.yaml`: the init container's `MODEL_FILE` value and the server's `-m` argument.

```bash
grep -n "gguf" deploy/base/model/deployment.yaml   # both lines must match your file name
```

Push the model manifests and child Application:

```bash
git add deploy/base/model/ argocd/apps/model.yaml
git commit -m "Component 5: model server"
git push

kubectl -n argocd patch application finbot-root --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

Watch the first run. It downloads roughly 2 GB, then loads the model, so it is slow:

```bash
kubectl -n finbot get pods -l app=llama-cpp -o wide       # should land on the model node
kubectl -n finbot logs -l app=llama-cpp -c fetch-model    # download progress (first run only)
kubectl -n finbot logs -l app=llama-cpp -c llama-cpp      # server loading the model
```

The server logs should show `model loaded` and `listening on http://0.0.0.0:8080`.

Verify:

```bash
kubectl -n argocd get application model \
  -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'

kubectl -n finbot get pods -l app=llama-cpp     # 1/1 Running
kubectl -n finbot get pvc model-cache           # Bound
```

Expected:

- `model` Application: `Synced Healthy`
- Pod `1/1 Running` on the model node
- PVC `model-cache` in `Bound` state

> If the Application briefly shows `Degraded` while the model downloads and loads, that is expected; it clears when the startup probe passes. If it stays `Degraded` after the pod is `1/1 Running`, force a refresh or restart the Argo CD application controller.

### Test the model API

Port 8080 on the host is used by kind, so forward to a different local port and force IPv4:

```bash
kubectl -n finbot port-forward --address 127.0.0.1 svc/llama-cpp-svc 9080:8080
```

In another terminal:

```bash
curl http://127.0.0.1:9080/v1/models

curl http://127.0.0.1:9080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hello in 3 words."}],"max_tokens":16}'
```

A generated reply confirms the model is serving.

> If curl reports `Connection reset by peer` with `localhost`, it is an IPv4/IPv6 mismatch. Use `--address 127.0.0.1` on the port-forward as above, or query the IPv6 form: `curl http://[::1]:9080/v1/models`.

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

1. Change its manifests under `deploy/` or its Helm values.
2. Commit and push.
3. Argo CD detects and deploys the change.

For a new component:

1. Add its manifests under `deploy/` or its Helm configuration.
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

A new cluster has no Argo CD installation and receives a new Sealed Secrets key. To start again, repeat Components 0 through 5 and re-seal the secrets.

### Recovery when Terraform destroy fails

Use this only for a broken kind cluster that Terraform cannot destroy normally, or when worker containers show `Dead`:

```bash
kind delete cluster --name finbot
docker ps -a --filter "label=io.x-k8s.kind.cluster=finbot" -q \
  | xargs -r docker rm -f
terraform state rm kind_cluster.finbot 2>/dev/null
docker system prune -f
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
| Worker nodes go `NotReady` or containers show `Dead` | Memory or disk pressure; give Docker at least 12 GB, run `docker system prune -f`, and recreate. |
| Sealed Secrets chart returns `404` or OCI `403` | Use the vendored static controller manifest instead of Helm/OCI. |
| Kustomize reports `did not find expected key` | Replace `resources: []` with a normal YAML resource list. |
| Argo CD still shows an old commit | Hard-refresh the Application or use Refresh in the UI. |
| `sealed-secret-manifests` is `OutOfSync` | Keep the Alertmanager Secret disabled until the `monitoring` namespace exists. |
| `redis-0` remains `Pending` | Check `kubectl get storageclass`; kind normally provides the `standard` StorageClass. |
| Monitoring Application is `Unknown` with a chart fetch error | Allow the `prometheus-community` repository in the AppProject. |
| Monitoring sync rejected for webhook resources | Add `MutatingWebhookConfiguration` and `ValidatingWebhookConfiguration` to the AppProject `clusterResourceWhitelist`. |
| Grafana `CrashLoopBackOff` or OOM | Raise its memory limit above 512Mi, or set `grafana.enabled: false`. |
| Monitoring port-forward reports `service not found` | Use the `kps-` service names, not `*-operated`. |
| `model` Application stays `Degraded` after the pod is `Running` | Stale health; hard-refresh, or run `kubectl -n argocd rollout restart statefulset argocd-application-controller`. |
| curl reports `Connection reset by peer` on localhost | IPv4/IPv6 mismatch; use `--address 127.0.0.1` on the port-forward, or curl `http://[::1]:PORT`. |
| Local port 8080 already in use | It is the kind port mapping; forward to a different local port such as `9080:8080`. |
| model pod remains `Pending` | The model node is `NotReady`, or the PVC cannot bind; check the nodes and the StorageClass. |

## Done through Component 5

- Three nodes are `Ready`: infra control-plane without a taint, model and agent workers with `dedicated` taints.
- Argo CD Applications are `Synced` and `Healthy`.
- `kubectl -n finbot get secrets` lists decrypted gateway, model and agent Secrets.
- `redis-0` is running on the infra node, its PVC is `Bound`, and `redis-cli ping` returns `PONG`.
- Prometheus and Alertmanager are running in the `monitoring` namespace, and the Alertmanager Slack Secret has decrypted.
- The model pod is `1/1 Running` on the model node, its cache PVC is `Bound`, and `/v1/chat/completions` returns generated text.

Next: **Component 6 — API gateway**, the policy and resilience layer in front of the model, with authentication, rate limiting, caching, timeouts, bounded retries and a circuit breaker.
