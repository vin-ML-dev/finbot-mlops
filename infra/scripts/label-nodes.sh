#!/usr/bin/env bash
# Apply taints to Node B (model) and Node C (agent) so ONLY tolerating workloads
# land there — the same fencing we'll use on Hetzner. Node labels (node-role=*)
# are already set by the kind config; this adds the taints kind can't set itself.
#
# Idempotent: safe to run more than once.
set -euo pipefail

CLUSTER="${CLUSTER_NAME:-finbot}"
CTX="kind-${CLUSTER}"

echo ">> Using context: ${CTX}"
kubectl --context "${CTX}" get nodes -o wide

# kind names workers <cluster>-worker, <cluster>-worker2 (control-plane = <cluster>-control-plane)
NODE_B="${CLUSTER}-worker"    # Node B : model
NODE_C="${CLUSTER}-worker2"   # Node C : agent

echo ">> Labeling + tainting Node B (${NODE_B}) for the model"
kubectl --context "${CTX}" label  node "${NODE_B}" node-role=model --overwrite
kubectl --context "${CTX}" taint  node "${NODE_B}" dedicated=model:NoSchedule --overwrite

echo ">> Labeling + tainting Node C (${NODE_C}) for the agent"
kubectl --context "${CTX}" label  node "${NODE_C}" node-role=agent --overwrite
kubectl --context "${CTX}" taint  node "${NODE_C}" dedicated=agent:NoSchedule --overwrite

echo ">> Node A (control-plane) stays schedulable for infra/monitoring."
# kind's control-plane is schedulable by default (no NoSchedule taint), which is
# what we want locally — Node A hosts gateway/redis/prometheus/alertmanager.

echo
echo ">> Final node roles:"
kubectl --context "${CTX}" get nodes \
  -L node-role -o custom-columns='NODE:.metadata.name,ROLE:.metadata.labels.node-role,TAINTS:.spec.taints[*].key'
