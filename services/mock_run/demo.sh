#!/usr/bin/env bash
set -euo pipefail

NETWORK_NAME="demo-net"
SUBNET="172.30.0.0/24"

GEODNS_IMAGE="geodns-demo"
BACKEND_IMAGE="mock-backend"

GEODNS_CONTAINER="geodns"
NGINX_CONTAINER="eu-nginx"
BACKEND1_CONTAINER="booking-service-1"
BACKEND2_CONTAINER="booking-service-2"

GEODNS_HOST_PORT="1053"
NGINX_HOST_PORT="8081"
NGINX_STATIC_IP="172.30.0.10"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEODNS_DIR="${ROOT_DIR}/../geoDNS"
BACKEND_DIR="${ROOT_DIR}/server"
NGINX_CONF="${ROOT_DIR}/mock_nginx/nginx.conf"

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

remove_container_if_exists() {
  local name="$1"
  if container_exists "$name"; then
    docker rm -f "$name" >/dev/null 2>&1 || true
  fi
}

network_exists() {
  docker network ls --format '{{.Name}}' | grep -Fxq "$1"
}

ensure_prereqs() {
  command -v docker >/dev/null 2>&1 || {
    echo "Docker is required but not installed."
    exit 1
  }

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required for the test step."
    exit 1
  fi
}

build_images() {
  log "Building GeoDNS image"
  docker build -t "$GEODNS_IMAGE" "$GEODNS_DIR"

  log "Building mock backend image"
  docker build -t "$BACKEND_IMAGE" "$BACKEND_DIR"
}

create_network() {
  if network_exists "$NETWORK_NAME"; then
    log "Docker network '$NETWORK_NAME' already exists"
  else
    log "Creating Docker network '$NETWORK_NAME'"
    docker network create \
      --driver bridge \
      --subnet "$SUBNET" \
      "$NETWORK_NAME"
  fi
}

start_backends() {
  log "Starting backend containers"

  remove_container_if_exists "$BACKEND1_CONTAINER"
  remove_container_if_exists "$BACKEND2_CONTAINER"

  docker run -d \
    --name "$BACKEND1_CONTAINER" \
    --network "$NETWORK_NAME" \
    -e REGION=eu \
    -e INSTANCE=booking-service-1 \
    "$BACKEND_IMAGE" >/dev/null

  docker run -d \
    --name "$BACKEND2_CONTAINER" \
    --network "$NETWORK_NAME" \
    -e REGION=eu \
    -e INSTANCE=booking-service-2 \
    "$BACKEND_IMAGE" >/dev/null
}

start_nginx() {
  if [[ ! -f "$NGINX_CONF" ]]; then
    echo "NGINX config not found at: $NGINX_CONF"
    exit 1
  fi

  log "Starting EU NGINX gateway"

  remove_container_if_exists "$NGINX_CONTAINER"

  docker run -d \
    --name "$NGINX_CONTAINER" \
    --network "$NETWORK_NAME" \
    --ip "$NGINX_STATIC_IP" \
    -p "${NGINX_HOST_PORT}:80" \
    -v "${NGINX_CONF}:/etc/nginx/nginx.conf:ro" \
    nginx:latest >/dev/null
}

start_geodns() {
  log "Starting GeoDNS"

  remove_container_if_exists "$GEODNS_CONTAINER"

  docker run -d \
    --name "$GEODNS_CONTAINER" \
    --network "$NETWORK_NAME" \
    -p "${GEODNS_HOST_PORT}:53/udp" \
    "$GEODNS_IMAGE" >/dev/null
}

wait_for_services() {
  log "Waiting briefly for containers to start"
  sleep 3
}

run_tests() {
  log "Testing GeoDNS"

  if command -v dig >/dev/null 2>&1; then
    echo "eu.api.demo.local -> $(dig @127.0.0.1 -p ${GEODNS_HOST_PORT} eu.api.demo.local A +short)"
    echo "us.api.demo.local -> $(dig @127.0.0.1 -p ${GEODNS_HOST_PORT} us.api.demo.local A +short)"
    echo "asia.api.demo.local -> $(dig @127.0.0.1 -p ${GEODNS_HOST_PORT} asia.api.demo.local A +short)"
    echo "api.demo.local -> $(dig @127.0.0.1 -p ${GEODNS_HOST_PORT} api.demo.local A +short)"
  else
    echo "dig is not installed, skipping DNS query test."
    echo "On macOS you can install it with: brew install bind"
  fi

  log "Testing NGINX health endpoint"
  curl -s "http://localhost:${NGINX_HOST_PORT}/health"
  echo

  log "Testing POST /bookings through NGINX"
  curl -s -X POST "http://localhost:${NGINX_HOST_PORT}/bookings" \
    -H "Content-Type: application/json" \
    -d '{"customer_name":"Dylan","route":"Dublin-Galway","seats":2}'
  echo

  log "Repeating POST /bookings to observe load balancing"
  curl -s -X POST "http://localhost:${NGINX_HOST_PORT}/bookings" \
    -H "Content-Type: application/json" \
    -d '{"customer_name":"Dylan","route":"Dublin-Cork","seats":1}'
  echo
}

show_status() {
  log "Running containers"
  docker ps --filter "name=${GEODNS_CONTAINER}|${NGINX_CONTAINER}|${BACKEND1_CONTAINER}|${BACKEND2_CONTAINER}"

  log "Useful endpoints"
  echo "GeoDNS: dig @127.0.0.1 -p ${GEODNS_HOST_PORT} eu.api.demo.local A +short"
  echo "NGINX health: curl http://localhost:${NGINX_HOST_PORT}/health"
  echo "NGINX bookings: curl -X POST http://localhost:${NGINX_HOST_PORT}/bookings -H 'Content-Type: application/json' -d '{\"customer_name\":\"Dylan\",\"route\":\"Dublin-Galway\",\"seats\":2}'"
}

up() {
  ensure_prereqs
  build_images
  create_network
  start_backends
  start_nginx
  start_geodns
  wait_for_services
  show_status
}

test_only() {
  ensure_prereqs
  run_tests
}

all() {
  up
  run_tests
}

logs() {
  log "GeoDNS logs"
  docker logs "$GEODNS_CONTAINER" || true

  log "NGINX logs"
  docker logs "$NGINX_CONTAINER" || true

  log "booking-service-1 logs"
  docker logs "$BACKEND1_CONTAINER" || true

  log "booking-service-2 logs"
  docker logs "$BACKEND2_CONTAINER" || true
}

usage() {
  cat <<EOF
Usage: ./demo.sh <command>

Commands:
  up      Build images, create network, and start all containers
  test    Run DNS and HTTP tests against running containers
  all     Start everything and run tests
  logs    Show logs for all demo containers

Examples:
  ./demo.sh up
  ./demo.sh test
  ./demo.sh all
  ./demo.sh logs
EOF
}

case "${1:-}" in
  up)
    up
    ;;
  test)
    test_only
    ;;
  all)
    all
    ;;
  logs)
    logs
    ;;
  *)
    usage
    exit 1
    ;;
esac