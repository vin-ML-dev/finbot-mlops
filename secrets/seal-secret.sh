#!/usr/bin/env bash
# Seal a plaintext Secret into an encrypted SealedSecret that is safe to commit.
#
#   ./seal-secret.sh <plaintext-secret.yaml> <output-sealed.yaml>
#
# Needs: kubeseal (https://github.com/bitnami-labs/sealed-secrets/releases) and
# the controller running in-cluster (Argo installs it, wave 0).
set -euo pipefail

SRC="${1:?usage: seal-secret.sh <plaintext.yaml> <output-sealed.yaml>}"
OUT="${2:?usage: seal-secret.sh <plaintext.yaml> <output-sealed.yaml>}"

echo ">> Sealing ${SRC} -> ${OUT}"
kubeseal \
  --controller-namespace kube-system \
  --controller-name sealed-secrets \
  --format yaml \
  < "${SRC}" > "${OUT}"

echo ">> Done. ${OUT} is encrypted and safe to commit."
echo ">> Remember to add it to secrets/sealed/kustomization.yaml, then git push."
