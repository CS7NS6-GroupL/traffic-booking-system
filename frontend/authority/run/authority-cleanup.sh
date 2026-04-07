#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="traffic-authority-ui"
IMAGE_NAME="traffic-authority-ui"

echo "Stopping and removing authority frontend container..."
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "Removing authority frontend image..."
docker rmi "$IMAGE_NAME" >/dev/null 2>&1 || true

echo "Authority frontend cleanup complete."