#!/usr/bin/env bash
# demo_kafka_failure.sh
# Demonstrates Kafka broker failure and consumer reconnection.
# Run against a Minikube cluster with Kafka deployed as a StatefulSet.

set -euo pipefail

NAMESPACE=${1:-eu}

echo "======================================================"
echo " Demo: Kafka Broker Failure"
echo " Namespace: $NAMESPACE"
echo "======================================================"

echo ""
echo "[1] Current Kafka pods:"
kubectl get pods -n "$NAMESPACE" -l app=kafka

echo ""
echo "[2] Deleting kafka-0 to simulate broker failure..."
kubectl delete pod -n "$NAMESPACE" kafka-0 --wait=false

echo ""
echo "[3] Watching broker recovery (Ctrl+C to stop)..."
kubectl get pods -n "$NAMESPACE" -w &
WATCH_PID=$!
sleep 45
kill $WATCH_PID 2>/dev/null || true

echo ""
echo "[4] Final Kafka broker state:"
kubectl get pods -n "$NAMESPACE" -l app=kafka

echo ""
echo "======================================================"
echo " Expected outcome:"
echo "  - kafka-0 pod is terminated by Kubernetes."
echo "  - StatefulSet controller schedules a replacement pod"
echo "    with the same identity (kafka-0)."
echo "  - Kafka consumers (validation-service,"
echo "    notification-service) detect broker unavailability"
echo "    and automatically reconnect once the broker is back."
echo "  - Topic partitions are reassigned; messages already"
echo "    acknowledged before the crash are not re-delivered."
echo "  - In a multi-broker setup, a replica broker would"
echo "    take over partition leadership immediately."
echo "======================================================"
