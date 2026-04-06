#!/usr/bin/env bash
set -euo pipefail

NETWORK_NAME="demo-net"

CONTAINERS=(
  geodns
  eu-nginx
  booking-service-1
  booking-service-2
)

echo "Stopping and removing demo containers..."
for c in "${CONTAINERS[@]}"; do
  docker rm -f "$c" >/dev/null 2>&1 || true
done

echo "Removing Docker network..."
docker network rm "$NETWORK_NAME" >/dev/null 2>&1 || true

echo "Cleanup complete."