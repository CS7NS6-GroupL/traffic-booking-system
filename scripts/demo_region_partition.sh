#!/usr/bin/env bash
# demo_region_partition.sh
# Simulates the EU regional cluster losing network connectivity.
#
# This is achieved by cordoning all EU Minikube nodes so that
# no new pods can be scheduled and existing pods appear "unreachable"
# to the other clusters. AWS Route 53 health checks detect the
# outage and stop routing traffic to the EU cluster.
# The Data Gateway falls back to MongoDB Atlas for EU-region data.

set -euo pipefail

EU_NODES=$(kubectl get nodes -l region=eu -o jsonpath='{.items[*].metadata.name}')

if [ -z "$EU_NODES" ]; then
  echo "No nodes labelled region=eu found."
  echo "For a local Minikube demo, label a node first:"
  echo "  kubectl label node <node-name> region=eu"
  exit 1
fi

echo "======================================================"
echo " Demo: EU Regional Cluster Network Partition"
echo "======================================================"

echo ""
echo "[1] EU nodes to be cordoned: $EU_NODES"

echo ""
echo "[2] Cordoning EU nodes (simulating connectivity loss)..."
for NODE in $EU_NODES; do
  kubectl cordon "$NODE"
  echo "  Cordoned: $NODE"
done

echo ""
echo "[3] EU cluster is now isolated."
echo "    In production, Route 53 health checks (interval 30s)"
echo "    would detect unhealthy endpoints and stop routing"
echo "    new requests to the EU cluster."
echo ""
echo "    The Data Gateway in US/Asia clusters falls back to"
echo "    MongoDB Atlas when EU data-gateway is unreachable."
echo ""
echo "    Sleeping 30s to observe the partition effect..."
sleep 30

echo ""
echo "[4] Restoring EU nodes (uncordoning)..."
for NODE in $EU_NODES; do
  kubectl uncordon "$NODE"
  echo "  Uncordoned: $NODE"
done

echo ""
echo "[5] EU cluster restored. Route 53 will resume routing"
echo "    after the next health check passes (~30s)."

echo ""
echo "======================================================"
echo " Expected outcome:"
echo "  - During partition: EU pods cannot be scheduled;"
echo "    Route 53 removes EU from DNS answers."
echo "  - US and Asia clusters continue serving all traffic."
echo "  - Data Gateway requests for EU data fall back to"
echo "    MongoDB Atlas (globally replicated) with a small"
echo "    consistency lag (eventual consistency window)."
echo "  - After uncordon: EU pods reschedule, health checks"
echo "    pass, Route 53 reintroduces EU into rotation."
echo "======================================================"
