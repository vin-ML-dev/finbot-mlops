#!/usr/bin/env bash
# Tear the cluster down completely. For kind this frees all laptop RAM; on
# Hetzner the equivalent destroys the VPS so billing stops. Same command shape.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${HERE}/../terraform"

echo "==> terraform destroy (deletes the kind cluster)"
terraform -chdir="${TF_DIR}" destroy -auto-approve -input=false

echo "==> Done. Verify nothing lingers:"
kind get clusters 2>/dev/null || true
