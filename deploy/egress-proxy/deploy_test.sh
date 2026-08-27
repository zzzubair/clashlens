#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORK_DIR=$(mktemp -d)
trap 'rm -rf -- "$WORK_DIR"' EXIT

FAKE_DOCKER="$WORK_DIR/docker"
DOCKER_LOG="$WORK_DIR/docker.log"
cat >"$FAKE_DOCKER" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >>"$FAKE_DOCKER_LOG"
printf '\n' >>"$FAKE_DOCKER_LOG"
case "${1:-} ${2:-}" in
  "container inspect") exit 1 ;;
esac
EOF
chmod 0700 "$FAKE_DOCKER"

if [[ ! -x "$ROOT_DIR/deploy.sh" ]]; then
  printf 'expected RED: egress proxy deploy script is missing\n' >&2
  exit 1
fi

FAKE_DOCKER_LOG="$DOCKER_LOG" \
  DOCKER_BIN="$FAKE_DOCKER" \
  PROXY_STATE_DIR="$WORK_DIR" \
  PROXY_LISTEN_IP=100.64.0.1 \
  PROXY_CLIENT_IP=100.64.0.2 \
  PROXY_PORT=3129 \
  "$ROOT_DIR/deploy.sh" up >/dev/null

run_line=$(grep '^run ' "$DOCKER_LOG")
[[ "$run_line" == *'--network host'* ]] || {
  printf 'proxy does not use Docker host networking\n' >&2
  exit 1
}
[[ "$run_line" != *'--publish'* ]] || {
  printf 'proxy still publishes a bridge port; bridge publication hides the client source address\n' >&2
  exit 1
}
[[ "$run_line" == *'--read-only'* && "$run_line" == *'--cap-drop all'* ]] || {
  printf 'proxy container hardening is incomplete\n' >&2
  exit 1
}
[[ "$run_line" == *'--restart unless-stopped'* ]] || {
  printf 'proxy restart policy is missing\n' >&2
  exit 1
}

grep -q '^Allow 100.64.0.2$' "$WORK_DIR/tinyproxy.conf" || {
  printf 'proxy client restriction is missing\n' >&2
  exit 1
}
grep -q '^Listen 100.64.0.1$' "$WORK_DIR/tinyproxy.conf" || {
  printf 'proxy does not listen on the configured Tailscale address\n' >&2
  exit 1
}
grep -q '^Port 3129$' "$WORK_DIR/tinyproxy.conf" || {
  printf 'proxy does not listen on the configured port\n' >&2
  exit 1
}
if grep -Eq '^(Listen 0\.0\.0\.0|Port 8888)$' "$WORK_DIR/tinyproxy.conf"; then
  printf 'proxy configuration still binds the bridge defaults\n' >&2
  exit 1
fi
grep -q '^ConnectPort 443$' "$WORK_DIR/tinyproxy.conf" || {
  printf 'proxy CONNECT port restriction is missing\n' >&2
  exit 1
}
[[ "$(stat -c '%a' "$WORK_DIR/tinyproxy.conf")" == "644" ]] || {
  printf 'proxy configuration is not readable by the unprivileged container user\n' >&2
  exit 1
}
grep -Fxq '^api\.clashofclans\.com$' "$ROOT_DIR/filter" || {
  printf 'proxy host allowlist is missing\n' >&2
  exit 1
}

printf 'ok: egress proxy is restricted and hardened\n'
