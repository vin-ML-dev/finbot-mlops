#!/usr/bin/env bash
# Component 1 — install Argo CD, then hand control to Git via the root app.
# One-time per cluster. After this, deploys are `git push`, not kubectl.
set -euo pipefail

CTX="${KUBE_CONTEXT:-kind-finbot}"
ARGOCD_VERSION="${ARGOCD_VERSION:-stable}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Using context: ${CTX}"
kubectl --context "${CTX}" cluster-info >/dev/null

echo "==> 1/4 Create the argocd namespace"
kubectl --context "${CTX}" create namespace argocd --dry-run=client -o yaml \
  | kubectl --context "${CTX}" apply -f -

echo "==> 2/4 Install Argo CD (${ARGOCD_VERSION})"
kubectl --context "${CTX}" apply -n argocd \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

echo "==> 3/4 Wait for the Argo CD server to be ready"
kubectl --context "${CTX}" -n argocd rollout status deploy/argocd-server --timeout=300s

echo "==> 4/4 Apply the AppProject + the root App-of-Apps"
kubectl --context "${CTX}" apply -f "${HERE}/../projects/finbot-project.yaml"
kubectl --context "${CTX}" apply -f "${HERE}/root-app.yaml"

echo
echo "==> Argo CD is installed and the root app is applied."
echo "    Initial admin password:"
kubectl --context "${CTX}" -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d; echo
echo "    UI:  kubectl -n argocd port-forward svc/argocd-server 8081:443"
echo "         then open https://localhost:8081  (user: admin)"
