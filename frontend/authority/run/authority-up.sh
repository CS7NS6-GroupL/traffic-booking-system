#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="traffic-authority-ui"
CONTAINER_NAME="traffic-authority-ui"
NETWORK_NAME="demo-net"
HOST_PORT="8000"

DNS_SERVER="geodns"
DNS_PORT="53"
DNS_NAME="eu.api.demo.local"
NGINX_PORT="80"
REQUEST_TIMEOUT="5"

log() {
  echo
  echo "==> $1"
}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fxq "$1"
}

running_container_exists() {
  docker ps --format '{{.Names}}' | grep -Fxq "$1"
}

network_exists() {
  docker network ls --format '{{.Name}}' | grep -Fxq "$1"
}

remove_container_if_exists() {
  local name="$1"
  if container_exists "$name"; then
    docker rm -f "$name" >/dev/null 2>&1 || true
  fi
}

ensure_prereqs() {
  command -v docker >/dev/null 2>&1 || {
    echo "Docker is required but not installed."
    exit 1
  }
}

ensure_network() {
  if ! network_exists "$NETWORK_NAME"; then
    echo "Docker network '$NETWORK_NAME' does not exist."
    echo "Start your GeoDNS / Nginx demo first so the frontend can join the same network."
    exit 1
  fi
}

build_image() {
  log "Building authority frontend image"
  docker build -t "$IMAGE_NAME" .
}

start_container() {
  log "Starting authority frontend container"

  remove_container_if_exists "$CONTAINER_NAME"

  docker run -d \
    --name "$CONTAINER_NAME" \
    --network "$NETWORK_NAME" \
    -p "${HOST_PORT}:8000" \
    -e DNS_SERVER="$DNS_SERVER" \
    -e DNS_PORT="$DNS_PORT" \
    -e DNS_NAME="$DNS_NAME" \
    -e NGINX_PORT="$NGINX_PORT" \
    -e REQUEST_TIMEOUT="$REQUEST_TIMEOUT" \
    "$IMAGE_NAME" >/dev/null
}

show_status() {
  log "Frontend is running"
  echo "Container: $CONTAINER_NAME"
  echo "URL: http://localhost:${HOST_PORT}"
  echo
  echo "Default DNS name inside app: ${DNS_NAME}"
  echo "GeoDNS server: ${DNS_SERVER}:${DNS_PORT}"
}

main() {
  ensure_prereqs
  ensure_network
  build_image
  start_container
  show_status
}

main "$@"