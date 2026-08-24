# Component 1 — Argo CD bootstrap (the GitOps control plane)

This installs **Argo CD** into the cluster and hands deployment control to Git. After this component,
the workflow changes fundamentally: you stop running `kubectl apply` for workloads. Instead you
**commit a manifest and Argo CD syncs it** — automatically, and it heals drift back to what Git says.

## The core idea (App-of-Apps)

You apply **one** manifest by hand, ever: the **root app** (`argocd/bootstrap/root-app.yaml`). It points
Argo at the `argocd/apps/` folder. Every file in that folder is itself an Argo `Application` — one per
platform component. So applying the root app makes Argo discover and sync **all** of them, in order.

```
you: kubectl apply -f argocd/bootstrap/root-app.yaml   (once)
         │
         ▼
   root app  ──watches──▶  argocd/apps/*.yaml
         │                      ├── namespace.yaml   (wave -1)  ← this component
         │                      ├── redis.yaml       (wave 1)   ← later components
         │                      ├── model.yaml       (wave 2)
         │                      ├── gateway.yaml      (wave 3)
         │                      └── agent.yaml        (wave 4)
         ▼
   each child app ──syncs──▶ its Kustomize/Helm path in Git
```

From now on: **add a component = add its `Application` file to `argocd/apps/` and push.** Argo does the rest.

## What this component contains

- **`argocd/projects/finbot-project.yaml`** — an `AppProject` guardrail. It limits which Git repo Argo
  may pull from, which namespaces it may deploy to (`finbot`, `monitoring`, `argocd`), and which
  cluster-scoped resources it may create. Production hygiene: the project can't deploy random manifests
  into `kube-system`.
- **`argocd/bootstrap/root-app.yaml`** — the App-of-Apps described above. `prune: true` +
  `selfHeal: true` mean Argo removes what you delete from Git and reverts manual `kubectl` drift.
- **`argocd/apps/namespace.yaml`** — the first child app (sync-wave `-1`): it creates the `finbot`
  namespace. It's both a real dependency (everything deploys there) and the proof that the whole chain
  works before we add heavy workloads.
- **`deploy/base/namespace/`** — the Kustomize base the namespace app points at.
- **`argocd/bootstrap/install.sh`** + **`argocd-install.md`** — install Argo and apply the root app.

## Before you run

- **Component 0 must be up** — `kubectl get nodes` shows the three `Ready` nodes (context `kind-finbot`).
- **Set your Git repo URL.** The manifests reference `https://github.com/vin-ML-dev/finbot-mlops.git`.
  Argo pulls from Git, so this repo must exist and contain these files. Replace the placeholder URL in
  `root-app.yaml`, `finbot-project.yaml`, and `argocd/apps/namespace.yaml` with your real repo, and
  push them before applying. (Argo reads from Git, not your laptop — local files it can't see.)

## Run it

```bash
cd argocd/bootstrap
./install.sh
```

What it does: creates the `argocd` namespace → installs Argo CD → waits for the server → applies the
`AppProject` and the **root app**. It prints the initial admin password and the UI command at the end.

Manual equivalent is in `argocd-install.md`.

## Log in to the Argo UI

```bash
kubectl -n argocd port-forward svc/argocd-server 8081:443
# open https://localhost:8081  (accept the self-signed cert), user: admin
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

## Verify (acceptance check for component 1)

```bash
# 1) Argo CD pods are running
kubectl -n argocd get pods

# 2) the project and root app exist
kubectl -n argocd get appproject finbot
kubectl -n argocd get applications

# 3) the root app pulled in the namespace child app, and it synced
kubectl -n argocd get application namespace -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
#    want: Synced Healthy

# 4) the proof: the finbot namespace was created BY ARGO, not by you
kubectl get namespace finbot
```

If `applications` lists `finbot-root` and `namespace`, the namespace app shows **Synced / Healthy**, and
the `finbot` namespace exists — GitOps is working. You never created that namespace by hand; Argo did,
from Git.

## What "done" proves

You now have a control plane that turns Git into cluster state. The rest of the platform is just adding
`Application` files under `argocd/apps/` — each new component becomes a `git push`. The sync waves
already encode the model-first order (namespace → redis/monitoring → model → gateway → **agent last**).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `namespace` app stuck `OutOfSync`/`Unknown` | Argo can't reach your Git repo, or the path/URL is wrong. Check the repo URL in the manifests and that the files are pushed. |
| `ComparisonError: repository not found` | The `repoURL` doesn't match a repo Argo can read (private repo needs credentials). |
| Root app synced but no child apps appear | `root-app.yaml` `path` must be `argocd/apps` and those files must be pushed to Git. |
| Admin password command returns nothing | The initial-admin secret is deleted after first login/change; reset via the Argo CD docs. |
| Everything Healthy but nothing in `finbot` ns yet | Correct — only the namespace exists so far. Workloads arrive with later components. |

## Note on later components (sync waves aren't magic)

Sync waves order *creation*, not *readiness*. When we add the monitoring stack and Sealed Secrets
(later components), their **CRDs/controllers must be healthy before** resources that depend on them
sync. Argo's health checks handle most of this, but it's why those components get their own early waves
— we'll wire that carefully when we get there.

## Next

**Component 2 — Sealed Secrets**: the controller that lets encrypted secrets live safely in Git, plus
your HF token / gateway key / Slack webhook as sealed manifests. It's an early wave because everything
downstream needs those secrets.
