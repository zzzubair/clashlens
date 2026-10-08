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
[[ "$run_line" == *'--pids-limit 128'* && "$run_line" == *'--memory 128m'* ]] || {
  printf 'proxy has too few process slots or too little memory for 96 connections\n' >&2
  exit 1
}

# Tinyproxy reads one directive per line, its name in any case; a repeated
# directive would leave the effective value unclear, so each must appear once.
declare -A directive_values=() directive_counts=()
while read -r directive value; do
  [[ -z "$directive" || "$directive" == \#* ]] && continue
  directive=${directive,,}
  value=${value#\"}
  value=${value%\"}
  directive_counts[$directive]=$(( ${directive_counts[$directive]:-0} + 1 ))
  directive_values[$directive]=$value
done <"$WORK_DIR/tinyproxy.conf"

expect_directive() {
  local directive=$1 expected=$2 message=$3
  [[ "${directive_counts[$directive]:-0}" == 1 && "${directive_values[$directive]:-}" == "$expected" ]] || {
    printf '%s\n' "$message" >&2
    exit 1
  }
}

expect_directive allow 100.64.0.2 'proxy client restriction is missing'
expect_directive listen 100.64.0.1 'proxy does not listen only on the configured Tailscale address'
expect_directive port 3129 'proxy does not listen only on the configured port'
expect_directive maxclients 96 'proxy does not allow the 96 connections its callers are budgeted'
expect_directive connectport 443 'proxy CONNECT port restriction is missing'
[[ "$(stat -c '%a' "$WORK_DIR/tinyproxy.conf")" == "644" ]] || {
  printf 'proxy configuration is not readable by the unprivileged container user\n' >&2
  exit 1
}
grep -Eqf "$ROOT_DIR/filter" <<< 'api.clashofclans.com:443' || {
  printf 'proxy filter rejects the official API CONNECT destination\n' >&2
  exit 1
}
for rejected in 'http://api.clashofclans.com/' 'api.clashofclans.com:80' 'example.com:443' 'api.clashofclans.com.evil:443'; do
  if grep -Eqf "$ROOT_DIR/filter" <<< "$rejected"; then
    printf 'proxy filter permits forbidden destination %s\n' "$rejected" >&2
    exit 1
  fi
done

printf 'ok: egress proxy is restricted and hardened\n'
