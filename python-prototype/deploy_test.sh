#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$ROOT_DIR/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT
STATE_DIR="$TEST_ROOT/state"
mkdir -p "$STATE_DIR"

FAKE_PODMAN="$TEST_ROOT/podman"
FAKE_CURL="$TEST_ROOT/curl"
LOG_FILE="$STATE_DIR/commands.log"

cat > "$FAKE_PODMAN" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
state=${FAKE_PODMAN_STATE:?}
log=${FAKE_PODMAN_LOG:?}
printf '%q ' "$@" >> "$log"
printf '\n' >> "$log"
cmd=${1:-}
shift || true
case "$cmd" in
  info) printf 'true\n' ;;
  network|volume)
    action=${1:-}; name=${2:-};
    case "$action" in
      exists) [[ -f "$state/$cmd-$name" ]] ;;
      create) name=${@: -1}; touch "$state/$cmd-$name"; printf '%s\n' "$name" ;;
      inspect) [[ -f "$state/$cmd-$name" ]] ;;
      *) exit 1 ;;
    esac ;;
  secret)
    action=${1:-}; name=${2:-};
    case "$action" in
      inspect) [[ -f "$state/secret-$name" ]] ;;
      rm) rm -f "$state/secret-$name" ;;
      create) touch "$state/secret-$name"; printf '%s\n' "$name" ;;
      *) exit 1 ;;
    esac ;;
  container)
    action=${1:-}; name=${2:-};
    [[ "$action" == exists ]] && [[ -f "$state/container-$name" ]] ;;
  run)
    name=''
    while (($#)); do
      if [[ "$1" == '--name' ]]; then name=$2; shift 2; else shift; fi
    done
    [[ -n "$name" ]] || { printf 'missing name\n' >&2; exit 1; }
    touch "$state/container-$name"
    printf '%s\n' "id-$name"
    ;;
  rm)
    while (($#)) && [[ "$1" == -* ]]; do shift; done
    rm -f "$state/container-${1:-}"
    ;;
  start) : ;;
  build) touch "$state/image"; printf 'built\n' ;;
  exec)
    if [[ "${1:-}" == '--interactive' ]]; then shift; fi
    container=${1:-}; shift || true
    case "${1:-}" in
      pg_isready|psql) cat >/dev/null || true ;;
      python)
        if printf '%s\n' "$*" | grep -q ' probe '; then printf '{"tag":"#2PP"}\n'; fi
        ;;
    esac
    ;;
  ps) printf 'clashlens-python-prototype-api\tUp\nclashlens-python-prototype-worker\tUp\n' ;;
  logs) : ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$FAKE_PODMAN"

cat > "$FAKE_CURL" <<'EOF'
#!/usr/bin/env bash
printf '{"ready":true}\n'
EOF
chmod +x "$FAKE_CURL"

cp "$ROOT_DIR/prototype.env.example" "$TEST_ROOT/prototype.env"
printf 'CLASHLENS_SECRET_DIR=%s/secrets\n' "$TEST_ROOT" >> "$TEST_ROOT/prototype.env"
chmod 600 "$TEST_ROOT/prototype.env"

export FAKE_PODMAN_STATE="$STATE_DIR"
export FAKE_PODMAN_LOG="$LOG_FILE"
export DEPLOY_ENV_FILE="$TEST_ROOT/prototype.env"
export PODMAN_BIN="$FAKE_PODMAN"
export CURL_BIN="$FAKE_CURL"

"$ROOT_DIR/deploy.sh" init >/dev/null
database_url=$(<"$TEST_ROOT/secrets/database-url")
database_password=$(<"$TEST_ROOT/secrets/postgres-password")
expected_database_url="postgresql://clashlens_prototype:${database_password}@postgres:5432/clashlens_prototype?sslmode=disable"
if [[ "$database_url" != "$expected_database_url" ]]; then
    printf 'database URL did not contain the generated PostgreSQL credential\n' >&2
    exit 1
fi
"$ROOT_DIR/deploy.sh" init >/dev/null
"$ROOT_DIR/deploy.sh" up >/dev/null
"$ROOT_DIR/deploy.sh" verify >/dev/null

if [[ -f "$STATE_DIR/container-clashlens-python-prototype-archive-fixture" ]]; then
    printf 'temporary archive container was not removed\n' >&2
    exit 1
fi
if ! grep -q -- '--read-only' "$LOG_FILE"; then
    printf 'runtime containers were not read-only\n' >&2
    exit 1
fi
if ! grep -q -- '127.0.0.1:18080:8000' "$LOG_FILE"; then
    printf 'API was not bound to loopback\n' >&2
    exit 1
fi
if ! grep -q -- 'ON_ERROR_STOP=on' "$LOG_FILE"; then
    printf 'schema apply did not stop on SQL errors\n' >&2
    exit 1
fi
if ! grep -q -- '--archive-insecure-test-only' "$LOG_FILE"; then
    printf 'synthetic verification did not use its explicit test-only override\n' >&2
    exit 1
fi
if ! grep -q -- '--entrypoint python' "$LOG_FILE"; then
    printf 'temporary archive did not override the application entrypoint\n' >&2
    exit 1
fi
if grep -q -- 'localhost/clashlens-python-prototype:prototype python /opt/clashlens/scripts/fake_archive.py' "$LOG_FILE"; then
    printf 'temporary archive passed an extra python token after the explicit entrypoint\n' >&2
    exit 1
fi
UNIT_FILE="$ROOT_DIR/deploy/systemd/clashlens-python-prototype.service"
if ! grep -q '^ExecStart=.*deploy.sh up$' "$UNIT_FILE" || ! grep -q '^ExecStop=.*deploy.sh down$' "$UNIT_FILE"; then
    printf 'user systemd unit does not use the isolated lifecycle commands\n' >&2
    exit 1
fi
if grep -q 'volume rm' "$UNIT_FILE"; then
    printf 'user systemd unit removes the durable volume\n' >&2
    exit 1
fi
"$ROOT_DIR/deploy.sh" down >/dev/null
if [[ ! -f "$STATE_DIR/volume-clashlens-python-prototype-postgres-data" ]]; then
    printf 'database volume was not preserved by down\n' >&2
    exit 1
fi
if grep -q -- 'volume rm' "$LOG_FILE"; then
    printf 'down removed durable volume\n' >&2
    exit 1
fi
printf 'deployment seam checks passed\n'
