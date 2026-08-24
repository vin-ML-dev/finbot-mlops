#!/usr/bin/env bash
# Bring up the local 3-node cluster: terraform apply -> label/taint nodes.
# One command; safe to re-run. Hetzner will use the same shape (terraform apply
# a different provider, then the same node labeling).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${HERE}/../terraform"
CLUSTER="${CLUSTER_NAME:-finbot}"

echo "==> Preflight: docker + kubectl + terraform present"
command -v docker    >/dev/null || { echo "docker not found";    exit 1; }
command -v kubectl   >/dev/null || { echo "kubectl not found";   exit 1; }
command -v terraform >/dev/null || { echo "terraform not found"; exit 1; }

echo "==> Docker memory advice: give Docker >= 10-12 GB for a 3-node kind cluster."
docker info --format 'Docker total memory: {{.MemTotal}}' 2>/dev/null || true

echo "==> terraform init + apply (creates the kind cluster)"
terraform -chdir="${TF_DIR}" init -input=false
terraform -chdir="${TF_DIR}" apply -auto-approve -input=false

echo "==> Label + taint Node B/Node C"
CLUSTER_NAME="${CLUSTER}" "${HERE}/label-nodes.sh"

echo
echo "==> Cluster is up. Nodes:"
kubectl --context "kind-${CLUSTER}" get nodes -o wide
echo
echo "Next: component 1 (Argo CD bootstrap)."
