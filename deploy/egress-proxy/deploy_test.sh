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
  PROXY_LISTEN_IP=100.108.3.103 \
  PROXY_CLIENT_IP=100.115.149.49 \
  "$ROOT_DIR/deploy.sh" up >/dev/null

run_line=$(grep '^run ' "$DOCKER_LOG")
[[ "$run_line" == *'100.108.3.103:3128:8888/tcp'* ]] || {
  printf 'proxy is not bound only to the Tailscale address\n' >&2
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

grep -q '^Allow 100.115.149.49$' "$WORK_DIR/tinyproxy.conf" || {
  printf 'proxy client restriction is missing\n' >&2
  exit 1
}
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
