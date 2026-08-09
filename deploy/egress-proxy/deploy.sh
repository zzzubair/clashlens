#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DOCKER_BIN=${DOCKER_BIN:-docker}
PROXY_STATE_DIR=${PROXY_STATE_DIR:-"$HOME/.local/share/clashlens-egress-proxy"}
PROXY_LISTEN_IP=${PROXY_LISTEN_IP:-}
PROXY_CLIENT_IP=${PROXY_CLIENT_IP:-}
PROXY_PORT=${PROXY_PORT:-3128}
IMAGE=${PROXY_IMAGE:-clashlens-egress-proxy:deployment}
CONTAINER=${PROXY_CONTAINER:-clashlens-egress-proxy}

usage() {
  printf 'Usage: %s <up|down|status|logs>\n' "$0" >&2
}

die() {
  printf 'egress-proxy: error: %s\n' "$*" >&2
  exit 1
}

require_settings() {
  [[ "$PROXY_LISTEN_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "PROXY_LISTEN_IP must be an IPv4 address"
  [[ "$PROXY_CLIENT_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "PROXY_CLIENT_IP must be an IPv4 address"
  [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] || die "PROXY_PORT must be a port number"
  (( PROXY_PORT >= 1 && PROXY_PORT <= 65535 )) || die "PROXY_PORT is outside the valid range"
  command -v "$DOCKER_BIN" >/dev/null 2>&1 || die "Docker is required"
}

container_exists() {
  "$DOCKER_BIN" container inspect "$CONTAINER" >/dev/null 2>&1
}

write_config() {
  umask 077
  mkdir -p "$PROXY_STATE_DIR"
  chmod 0755 "$PROXY_STATE_DIR"
  cat >"$PROXY_STATE_DIR/tinyproxy.conf" <<EOF
User tinyproxy
Group tinyproxy
Port $PROXY_PORT
Listen $PROXY_LISTEN_IP
Timeout 60
LogFile "/dev/stderr"
LogLevel Info
PidFile "/tmp/tinyproxy.pid"
MaxClients 50
Allow $PROXY_CLIENT_IP
ConnectPort 443
Filter "/etc/tinyproxy/filter"
FilterDefaultDeny Yes
FilterURLs Off
DisableViaHeader Yes
EOF
  # This file contains only network policy. The unprivileged container user
  # must be able to read it through the bind mount.
  chmod 0644 "$PROXY_STATE_DIR/tinyproxy.conf"
}

up() {
  require_settings
  write_config
  "$DOCKER_BIN" build --pull --file "$ROOT_DIR/Containerfile" --tag "$IMAGE" "$ROOT_DIR"
  if container_exists; then
    "$DOCKER_BIN" rm --force "$CONTAINER" >/dev/null
  fi
  "$DOCKER_BIN" run \
    --detach \
    --name "$CONTAINER" \
    --network host \
    --mount "type=bind,src=$PROXY_STATE_DIR/tinyproxy.conf,dst=/etc/tinyproxy/tinyproxy.conf,readonly" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=4m \
    --cap-drop all \
    --security-opt no-new-privileges \
    --pids-limit 100 \
    --memory 64m \
    --restart unless-stopped \
    "$IMAGE" >/dev/null
  printf 'egress proxy is listening on %s:%s for client %s\n' "$PROXY_LISTEN_IP" "$PROXY_PORT" "$PROXY_CLIENT_IP"
}

case "${1:-}" in
  up)
    [[ $# == 1 ]] || die "up accepts no arguments"
    up
    ;;
  down)
    [[ $# == 1 ]] || die "down accepts no arguments"
    if container_exists; then
      "$DOCKER_BIN" rm --force "$CONTAINER" >/dev/null
    fi
    ;;
  status)
    [[ $# == 1 ]] || die "status accepts no arguments"
    if container_exists; then
      "$DOCKER_BIN" container inspect --format '{{.State.Status}}' "$CONTAINER"
    else
      printf 'absent\n'
    fi
    ;;
  logs)
    shift
    container_exists || die "proxy container does not exist"
    "$DOCKER_BIN" logs "$@" "$CONTAINER"
    ;;
  *)
    usage
    exit 2
    ;;
esac
