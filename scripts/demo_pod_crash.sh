#!/usr/bin/env bash
# demo_pod_crash.sh
# Demonstrates pod crash recovery with Kafka message retention.
# Run against a Minikube cluster with the eu namespace deployed.

set -euo pipefail

NAMESPACE=${1:-eu}

echo "======================================================"
echo " Demo: Pod Crash Recovery (booking-service)"
echo " Namespace: $NAMESPACE"
echo "======================================================"

echo ""
echo "[1] Current pods in namespace $NAMESPACE:"
kubectl get pods -n "$NAMESPACE" -l app=booking-service

echo ""
echo "[2] Deleting a booking-service pod to simulate crash..."
kubectl delete pod -n "$NAMESPACE" -l app=booking-service --wait=false

echo ""
echo "[3] Watching pod recovery (Ctrl+C to stop)..."
kubectl get pods -n "$NAMESPACE" -w &
WATCH_PID=$!
sleep 30
kill $WATCH_PID 2>/dev/null || true

echo ""
echo "[4] Final pod state:"
kubectl get pods -n "$NAMESPACE" -l app=booking-service

echo ""
echo "======================================================"
echo " Expected outcome:"
echo "  - Kubernetes detects the pod termination."
echo "  - A replacement pod is scheduled and starts (~15s)."
echo "  - The Kafka consumer in the new pod rejoins group"
echo "    'validation-group' and resumes from the last"
echo "    committed offset — no booking request is lost."
echo "  - Any in-flight booking that was unacked is"
echo "    reprocessed exactly once by the new pod."
echo "======================================================"
