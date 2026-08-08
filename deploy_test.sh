#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORK_DIR=$(mktemp -d)
trap 'rm -rf -- "$WORK_DIR"' EXIT



fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Shared fake Podman. State lives in $FAKE_STATE; every command line is
# appended escaped to $FAKE_PODMAN_LOG. The fake models enough state for the
# deploy script: networks, volumes, images, containers, secrets, psql stdin
# capture, and the deployment contract version (absent|1|2|3).
# ---------------------------------------------------------------------------
FAKE_BIN="$WORK_DIR/bin"
mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/podman" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >>"$FAKE_PODMAN_LOG"
printf '\n' >>"$FAKE_PODMAN_LOG"
verb=${1:-}
shift || true

case "$verb" in
  info)
    printf 'true\n'
    ;;
  build)
    while (( $# > 0 )); do
      if [[ "$1" == "--tag" && $# -ge 2 ]]; then
        mkdir -p "$FAKE_STATE/images/$(dirname "$2")"
        : >"$FAKE_STATE/images/$2"
        shift 2
      else
        shift
      fi
    done
    ;;
  network)
    sub=${1:-}
    name=${!#}
    case "$sub" in
      exists) [[ -d "$FAKE_STATE/networks/$name" ]] ;;
      create) mkdir -p "$FAKE_STATE/networks/$name" ;;
    esac
    ;;
  volume)
    sub=${1:-}
    name=${!#}
    case "$sub" in
      exists) [[ -d "$FAKE_STATE/volumes/$name" ]] ;;
      create) mkdir -p "$FAKE_STATE/volumes/$name" ;;
    esac
    ;;
  image)
    sub=${1:-}
    name=${2:-}
    case "$sub" in
      exists) [[ -f "$FAKE_STATE/images/$name" ]] ;;
      build)
        while (( $# > 0 )); do
          if [[ "$1" == "--tag" && $# -ge 2 ]]; then
            mkdir -p "$FAKE_STATE/images/$(dirname "$2")"
            : >"$FAKE_STATE/images/$2"
            shift 2
          else
            shift
          fi
        done
        ;;
    esac
    ;;
  container)
    sub=${1:-}
    shift || true
    case "$sub" in
      exists)
        name=${!#}
        [[ -d "$FAKE_STATE/containers/$name" ]]
        ;;
      inspect)
        name=${!#}
        if [[ "$*" == *"State.Health.Status"* ]]; then
          if [[ -f "$FAKE_STATE/containers/$name.health" ]]; then
            cat "$FAKE_STATE/containers/$name.health"
          else
            printf 'unknown\n'
          fi
        else
          if [[ -f "$FAKE_STATE/containers/$name.running" ]]; then
            printf 'true\n'
          else
            printf 'false\n'
          fi
        fi
        ;;
      start)
        name=${!#}
        : >"$FAKE_STATE/containers/$name.running"
        ;;
      stop)
        name=${!#}
        rm -f "$FAKE_STATE/containers/$name.running"
        ;;
      rm)
        name=${!#}
        rm -rf "$FAKE_STATE/containers/$name" \
          "$FAKE_STATE/containers/$name.running" \
          "$FAKE_STATE/containers/$name.health"
        ;;
    esac
    ;;
  run)
    name=""
    while (( $# > 0 )); do
      if [[ "$1" == "--name" && $# -ge 2 ]]; then
        name=$2
        shift 2
      else
        shift
      fi
    done
    mkdir -p "$FAKE_STATE/containers/$name"
    : >"$FAKE_STATE/containers/$name.running"
    ;;
  exec)
    interactive=false
    args=()
    for arg in "$@"; do
      if [[ "$arg" == "--interactive" ]]; then
        interactive=true
      else
        args+=("$arg")
      fi
    done
    if [[ "$interactive" == true ]]; then
      n=$(find "$FAKE_STATE/stdin" -maxdepth 1 -type f 2>/dev/null | wc -l)
      cat >"$FAKE_STATE/stdin/exec-$n"
      if grep -Fq 'CREATE TABLE IF NOT EXISTS collector_jobs (' "$FAKE_STATE/stdin/exec-$n"; then
        printf '1' >"$FAKE_STATE/contract_version"
      fi
      if grep -q 'collector_reset_baseline_sweeps' "$FAKE_STATE/stdin/exec-$n"; then
        printf '2' >"$FAKE_STATE/contract_version"
      fi
    fi
    if [[ "$*" == *"SELECT version FROM clash_lens_contract"* ]]; then
      if [[ -f "$FAKE_STATE/contract_version" ]]; then
        cat "$FAKE_STATE/contract_version"
      else
        exit 1
      fi
    fi
    if [[ "$*" == *"queue-status"* ]]; then
      printf '{"pending":0,"failed":0}\n'
    fi
    ;;
  stop)
    name=${!#}
    rm -f "$FAKE_STATE/containers/$name.running"
    ;;
  start)
    name=${!#}
    : >"$FAKE_STATE/containers/$name.running"
    ;;
  rm)
    name=${!#}
    rm -rf "$FAKE_STATE/containers/$name" \
      "$FAKE_STATE/containers/$name.running" \
      "$FAKE_STATE/containers/$name.health"
    ;;
  secret)
    sub=${1:-}
    shift || true
    case "$sub" in
      create)
        name=""
        source="-"
        if [[ "${1:-}" == "--replace" ]]; then
          name=${2:-}
          source=${3:--}
        else
          name=${1:-}
          source=${2:--}
        fi
        mkdir -p "$FAKE_STATE/secrets"
        if [[ "$source" == "-" ]]; then
          cat >"$FAKE_STATE/secrets/$name"
        else
          cp "$source" "$FAKE_STATE/secrets/$name"
        fi
        ;;
      rm)
        rm -f "$FAKE_STATE/secrets/${!#}"
        ;;
    esac
    ;;
  healthcheck)
    ;;
esac
EOF
chmod 0700 "$FAKE_BIN/podman"

cat >"$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
printf '{"ready":true}\n'
EOF
chmod 0700 "$FAKE_BIN/curl"

# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------

new_scenario() {
  local dir
  dir=$(mktemp -d "$WORK_DIR/scenario-XXXXXX")
  mkdir -p "$dir/keys" "$dir/state/containers" "$dir/state/networks" \
    "$dir/state/volumes" "$dir/state/images" "$dir/state/secrets" "$dir/state/stdin"
  local name
  for name in normal-1 normal-2 normal-3 normal-4 interactive-1 \
    clashlens-hmac-current clashlens-hmac-previous; do
    printf 'test-key-%s\n' "$name" >"$dir/keys/$name"
    chmod 0600 "$dir/keys/$name"
  done
  printf '%s\n' "$dir"
}

write_scenario_env() {
  local envfile=$1 keydir=$2
  cat >"$envfile" <<EOF
POSTGRES_DB=clashlens
POSTGRES_USER=clashlens
POSTGRES_PASSWORD=test-admin-password-0123456789abcdef
CLASHLENS_COLLECTOR_DB_PASSWORD=collector-role-password-0123456789
CLASHLENS_WORKER_DB_PASSWORD=worker-role-password-0123456789abcd
CLASHLENS_API_DB_PASSWORD=api-role-password-0123456789abcdef
CLASHLENS_ARCHIVE_ENDPOINT=storage.googleapis.com
CLASHLENS_ARCHIVE_SECURE=true
CLASHLENS_ARCHIVE_BUCKET=clash-lens-test
CLASHLENS_ARCHIVE_ACCESS_KEY=collector-archive-access
CLASHLENS_ARCHIVE_SECRET_KEY=collector-archive-secret-do-not-print
CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY=worker-archive-access
CLASHLENS_WORKER_ARCHIVE_SECRET_KEY=worker-archive-secret-do-not-print
CLASHLENS_OFFICIAL_API_ORIGIN=https://api.clashofclans.com
CLASHLENS_OFFICIAL_API_PROXY_URL=http://100.108.3.103:3128
CLASHLENS_API_KEY_HOST_DIR=$keydir
CLASHLENS_NORMAL_API_KEY_FILES=normal-1=/run/secrets/normal-1,normal-2=/run/secrets/normal-2,normal-3=/run/secrets/normal-3,normal-4=/run/secrets/normal-4
CLASHLENS_INTERACTIVE_API_KEY_FILES=interactive-1=/run/secrets/interactive-1
CLASHLENS_INTERACTIVE_API_KEY_FILE=/run/secrets/interactive-1
CLASHLENS_HMAC_CALLER=typescript-website
CLASHLENS_HMAC_KEY_ID=current
CLASHLENS_HMAC_SECRET_FILE=/run/secrets/clashlens-hmac-current
CLASHLENS_HEALTH_HOST=127.0.0.1
CLASHLENS_HEALTH_PORT=18081
CLASHLENS_COLLECTOR_MEMORY=256m
CLASHLENS_COLLECTOR_CPUS=1.0
CLASHLENS_COLLECTOR_PIDS=128
CLASHLENS_POSTGRES_MEMORY=512m
CLASHLENS_POSTGRES_CPUS=1.5
CLASHLENS_POSTGRES_PIDS=256
CLASHLENS_API_MEMORY=192m
CLASHLENS_API_CPUS=0.5
CLASHLENS_API_PIDS=128
CLASHLENS_WORKER_MEMORY=384m
CLASHLENS_WORKER_CPUS=1.0
CLASHLENS_WORKER_PIDS=256
CLASHLENS_WEBSITE_HOST=127.0.0.1
CLASHLENS_WEBSITE_PORT=13000
CLASHLENS_WEBSITE_MEMORY=128m
CLASHLENS_WEBSITE_CPUS=0.5
CLASHLENS_WEBSITE_PIDS=128
CLASHLENS_WORKER_LEASE_SECONDS=60
EOF
  chmod 0600 "$envfile"
}

deploy() {
  # deploy <scenario-dir> <env-file> [VAR=value ...] -- <deploy args...>
  local dir=$1 envfile=$2
  shift 2
  local -a extra=()
  while (( $# > 0 )) && [[ "$1" != "--" ]]; do
    extra+=("$1")
    shift
  done
  [[ "${1:-}" == "--" ]] && shift
  env FAKE_STATE="$dir/state" FAKE_PODMAN_LOG="$dir/podman.log" \
    DEPLOY_ENV_FILE="$envfile" PODMAN_BIN="$FAKE_BIN/podman" CURL_BIN="$FAKE_BIN/curl" \
    "${extra[@]}" "$ROOT_DIR/deploy.sh" "$@"
}

deploy_fails() {
  # deploy_fails <scenario-dir> <env-file> <expected-message> -- args...
  local dir=$1 envfile=$2 message=$3
  shift 3
  local output
  if output=$(deploy "$dir" "$envfile" "$@" 2>&1); then
    fail "expected failure: $message"
  fi
  [[ "$output" == *"$message"* ]] || {
    printf 'expected error containing %q, got:\n%s\n' "$message" "$output" >&2
    exit 1
  }
}

log_has() {
  local log=$1 pattern=$2 description=$3
  grep -q "$pattern" "$log" || fail "$description"
}

log_lacks() {
  local log=$1 pattern=$2 description=$3
  grep -q "$pattern" "$log" && fail "$description" || true
}

norm_log() {
  if (( $# > 0 )); then
    sed 's/\\//g' "$1"
  else
    sed 's/\\//g'
  fi
}

first_line() {
  local log=$1 pattern=$2
  grep -n "$pattern" "$log" | head -n 1 | cut -d: -f1
}

secret_file() {
  local dir=$1 name=$2
  printf '%s' "$dir/state/secrets/$name"
}

sentinel_values=(
  'test-admin-password-0123456789abcdef'
  'collector-role-password-0123456789'
  'worker-role-password-0123456789abcd'
  'api-role-password-0123456789abcdef'
  'collector-archive-secret-do-not-print'
  'worker-archive-secret-do-not-print'
)

assert_no_sentinel_in_log() {
  local log=$1 sentinel
  for sentinel in "${sentinel_values[@]}"; do
    grep -q "$sentinel" "$log" && fail "secret value $sentinel appeared in the command log"
  done
  return 0
}

# ---------------------------------------------------------------------------
# Guard 1: status rejects an unsafe app.env mode without printing values.
# ---------------------------------------------------------------------------
GUARD_DIR=$(new_scenario)
GUARD_ENV="$WORK_DIR/guard.env"
printf '%s\n' \
  'CLASHLENS_ARCHIVE_BUCKET=evidence' \
  >"$GUARD_ENV"
chmod 0644 "$GUARD_ENV"
if output=$(deploy "$GUARD_DIR" "$GUARD_ENV" -- status 2>&1); then
  fail 'status accepted an app.env file with mode 0644'
fi
[[ "$output" == *"mode 600"* ]] || fail 'status did not report the required app.env mode'
printf 'ok: status rejects an unsafe app.env mode without printing values\n'

# ---------------------------------------------------------------------------
# Guard 2: status and maintenance work without unrelated production settings.
# ---------------------------------------------------------------------------
RECOVERY_ENV="$WORK_DIR/recovery.env"
printf '%s\n' 'CLASHLENS_ARCHIVE_BUCKET=incomplete-recovery-config' >"$RECOVERY_ENV"
chmod 0600 "$RECOVERY_ENV"
if ! output=$(deploy "$GUARD_DIR" "$RECOVERY_ENV" -- status 2>&1); then
  fail 'status rejected an incomplete recovery configuration'
fi
[[ "$output" == *'collector: absent'* ]] || fail 'status did not inspect containers with incomplete configuration'
printf 'ok: status works without unrelated production settings\n'

MAINTENANCE_DIR=$(new_scenario)
mkdir -p "$MAINTENANCE_DIR/state/containers/clashlens-collector"
: >"$MAINTENANCE_DIR/state/containers/clashlens-collector.running"
if ! output=$(deploy "$MAINTENANCE_DIR" "$RECOVERY_ENV" -- maintenance list-failed --limit 20 2>&1); then
  fail 'maintenance failed with incomplete configuration'
fi
grep -q 'maintenance list-failed --limit 20' "$MAINTENANCE_DIR/podman.log" || \
  fail 'maintenance did not reach the running collector with incomplete configuration'
printf 'ok: maintenance works without unrelated production settings\n'

# ---------------------------------------------------------------------------
# Guard 3: deployment rejects inline API-key secrets.
# ---------------------------------------------------------------------------
INLINE_DIR=$(new_scenario)
INLINE_ENV="$INLINE_DIR/app.env"
write_scenario_env "$INLINE_ENV" "$INLINE_DIR/keys"
printf '%s\n' \
  'CLASHLENS_NORMAL_API_KEYS=normal-inline=inline-secret-do-not-print' \
  'CLASHLENS_INTERACTIVE_API_KEYS=interactive-inline=inline-secret-do-not-print' \
  >>"$INLINE_ENV"
if output=$(deploy "$INLINE_DIR" "$INLINE_ENV" -- up 2>&1); then
  fail 'deployment accepted inline API-key secrets'
fi
[[ "$output" == *'inline API keys must not be set'* ]] || fail 'deployment did not report the inline API-key guard'
[[ "$output" != *'inline-secret-do-not-print'* ]] || fail 'deployment printed an inline API-key secret'
printf 'ok: deployment rejects inline API-key secrets\n'

# ---------------------------------------------------------------------------
# Guard 4: legacy shared settings are rejected before side effects.
# ---------------------------------------------------------------------------
LEGACY_DIR=$(new_scenario)
LEGACY_ENV="$LEGACY_DIR/app.env"
write_scenario_env "$LEGACY_ENV" "$LEGACY_DIR/keys"
printf '%s\n' \
  'CLASHLENS_DATABASE_URL=postgresql://clashlens:legacy-secret@postgres:5432/clashlens?sslmode=disable' \
  >>"$LEGACY_ENV"
deploy_fails "$LEGACY_DIR" "$LEGACY_ENV" 'CLASHLENS_DATABASE_URL must not be set' -- up
[[ ! -s "$LEGACY_DIR/podman.log" ]] || fail 'legacy setting rejection had podman side effects'

LEGACY_IMAGE_DIR=$(new_scenario)
LEGACY_IMAGE_ENV="$LEGACY_IMAGE_DIR/app.env"
write_scenario_env "$LEGACY_IMAGE_ENV" "$LEGACY_IMAGE_DIR/keys"
printf '%s\n' 'CLASHLENS_PYTHON_WORKER_IMAGE=localhost/clashlens-python-worker:old' >>"$LEGACY_IMAGE_ENV"
deploy_fails "$LEGACY_IMAGE_DIR" "$LEGACY_IMAGE_ENV" 'renamed to CLASHLENS_PYTHON_IMAGE' -- up

GLOBAL_DIR=$(new_scenario)
GLOBAL_ENV="$GLOBAL_DIR/app.env"
write_scenario_env "$GLOBAL_ENV" "$GLOBAL_DIR/keys"
printf '%s\n' 'CLASHLENS_ENABLE_GLOBAL_RANKINGS=true' >>"$GLOBAL_ENV"
deploy_fails "$GLOBAL_DIR" "$GLOBAL_ENV" 'global Top-200 collection must stay default-off' -- up
printf 'ok: legacy shared database URL, worker image name, and global rankings are rejected\n'

# ---------------------------------------------------------------------------
# Guard 5: resource budgets must be explicit and valid before side effects.
# ---------------------------------------------------------------------------
BUDGET_DIR=$(new_scenario)
BUDGET_ENV="$BUDGET_DIR/app.env"
write_scenario_env "$BUDGET_ENV" "$BUDGET_DIR/keys"
printf '%s\n' 'CLASHLENS_COLLECTOR_MEMORY=CHANGE_ME' >>"$BUDGET_ENV"
deploy_fails "$BUDGET_DIR" "$BUDGET_ENV" 'CLASHLENS_COLLECTOR_MEMORY' -- up
[[ ! -s "$BUDGET_DIR/podman.log" ]] || fail 'placeholder resource budget had podman side effects'

BADBUDGET_DIR=$(new_scenario)
BADBUDGET_ENV="$BADBUDGET_DIR/app.env"
write_scenario_env "$BADBUDGET_ENV" "$BADBUDGET_DIR/keys"
printf '%s\n' 'CLASHLENS_API_CPUS=abc' >>"$BADBUDGET_ENV"
deploy_fails "$BADBUDGET_DIR" "$BADBUDGET_ENV" 'CLASHLENS_API_CPUS' -- up
[[ ! -s "$BADBUDGET_DIR/podman.log" ]] || fail 'invalid resource budget had podman side effects'
printf 'ok: resource budgets are required, explicit, and validated before side effects\n'

# ---------------------------------------------------------------------------
# Scenario A: fresh up runs bridge -> migration 0002 -> roles -> required.
# ---------------------------------------------------------------------------
FRESH_DIR=$(new_scenario)
FRESH_ENV="$FRESH_DIR/app.env"
write_scenario_env "$FRESH_ENV" "$FRESH_DIR/keys"
deploy "$FRESH_DIR" "$FRESH_ENV" -- up >/dev/null

FRESH_LOG="$FRESH_DIR/podman.log"
FRESH_NORM="$FRESH_DIR/podman.norm.log"
norm_log "$FRESH_LOG" >"$FRESH_NORM"
log_has "$FRESH_LOG" '^build ' 'collector image was not built'
log_has "$FRESH_LOG" '^exec --interactive clashlens-postgres psql ' 'migration psql execution is missing'

[[ -n "$(find "$FRESH_DIR/state/stdin" -maxdepth 1 -type f)" ]] || fail 'no psql stdin was captured'
grep -Flq 'CREATE TABLE IF NOT EXISTS collector_jobs (' "$FRESH_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'migration 0001 was not applied on an absent database'
grep -lq 'collector_reset_baseline_sweeps' "$FRESH_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'migration 0002 was not applied on a fresh database'
grep -lq 'ALTER ROLE clashlens_collector' "$FRESH_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'collector role password was not configured through psql stdin'
grep -lq 'ALTER ROLE clashlens_python_worker' "$FRESH_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'worker role password was not configured through psql stdin'
grep -lq 'ALTER ROLE clashlens_python_api' "$FRESH_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'api role password was not configured through psql stdin'
role_stdin=$(grep -l 'ALTER ROLE clashlens_collector WITH LOGIN PASSWORD' "$FRESH_DIR/state/stdin"/exec-* 2>/dev/null | head -n 1)
[[ -n "$role_stdin" ]] || fail 'could not locate the role configuration psql stdin'
grep -q 'collector-role-password-0123456789' "$role_stdin" || \
  fail 'collector role password did not reach psql stdin'
grep -q 'worker-role-password-0123456789abcd' "$role_stdin" || \
  fail 'worker role password did not reach psql stdin'
grep -q 'api-role-password-0123456789abcdef' "$role_stdin" || \
  fail 'api role password did not reach psql stdin'

bridge_run=$(grep '^run ' "$FRESH_NORM" | grep 'clashlens-collector-bridge')
required_run=$(grep '^run ' "$FRESH_NORM" | grep 'clashlens-collector:deployment' | grep -v 'clashlens-collector-bridge')
[[ -n "$bridge_run" ]] || fail 'bridge collector was not started on a fresh database'
[[ -n "$required_run" ]] || fail 'required collector was not started after migration'
[[ "$bridge_run" == *'CLASHLENS_SHARED_TRAFFIC_GATE_MODE=bridge'* ]] || \
  fail 'bridge collector did not receive the explicit bridge mode'
[[ "$bridge_run" == *'CLASHLENS_SCHEMA_VERSION=1'* ]] || \
  fail 'bridge collector did not receive contract version 1'
[[ "$required_run" == *'CLASHLENS_SHARED_TRAFFIC_GATE_MODE=required'* ]] || \
  fail 'required collector did not receive the explicit required mode'
[[ "$required_run" == *'CLASHLENS_SCHEMA_VERSION=2'* ]] || \
  fail 'required collector did not receive contract version 2'

bridge_line=$(first_line "$FRESH_LOG" 'clashlens-collector-bridge')
migration2_line=$(grep -n '^exec --interactive clashlens-postgres psql ' "$FRESH_LOG" | sed -n '2p' | cut -d: -f1)
required_line=$(first_line "$FRESH_LOG" '^run .*--name clashlens-collector ')
bridge_stop_line=$(first_line "$FRESH_LOG" '^stop --time 30 clashlens-collector-bridge')
bridge_secret_rm_line=$(first_line "$FRESH_LOG" '^secret rm clashlens-bridge-database-url')
[[ -n "$bridge_line" && -n "$migration2_line" && -n "$required_line" \
  && -n "$bridge_stop_line" && -n "$bridge_secret_rm_line" ]] || \
  fail 'could not locate bridge/migration/required order lines'
(( bridge_line < migration2_line )) || fail 'bridge was not started before migration 0002'
(( migration2_line < required_line )) || fail 'migration 0002 did not run before the required collector'
(( bridge_stop_line < required_line )) || fail 'bridge was not stopped before the required collector'
(( bridge_secret_rm_line < required_line )) || fail 'bridge admin secret was not removed before the required collector'
[[ ! -f "$(secret_file "$FRESH_DIR" clashlens-bridge-database-url)" ]] || \
  fail 'bridge admin secret file still exists after migration'

log_has "$FRESH_LOG" '^secret rm clashlens-bridge-database-url' 'bridge admin secret was not removed'
log_has "$FRESH_LOG" '^stop --time 30 clashlens-collector-bridge' 'bridge was not stopped gracefully'

assert_no_sentinel_in_log "$FRESH_LOG"
[[ "$bridge_run" != *'test-admin-password-0123456789abcdef'* ]] || fail 'admin password appeared in bridge run metadata'
grep -q 'collector-role-password-0123456789' "$(secret_file "$FRESH_DIR" clashlens-collector-database-url)" || \
  fail 'collector database URL did not flow through secret stdin'
grep -q 'collector-archive-secret-do-not-print' "$(secret_file "$FRESH_DIR" clashlens-collector-archive-secret-key)" || \
  fail 'collector archive secret did not flow through secret stdin'
[[ "$required_run" != *'clashlens-bridge-database-url'* ]] || \
  fail 'required collector received the bridge admin secret'

[[ "$bridge_run" == *'clashlens-bridge-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400'* ]] || \
  fail 'bridge admin URL secret was not mounted'
bridge_normalized=$(norm_log <<<"$bridge_run")
[[ "$bridge_normalized" == *'--env CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url'* ]] || \
  fail 'bridge did not receive the database URL file setting'
[[ "$bridge_normalized" == *'--env CLASHLENS_NORMAL_API_KEY_FILES=normal-1=/run/secrets/normal-1,normal-2=/run/secrets/normal-2,normal-3=/run/secrets/normal-3,normal-4=/run/secrets/normal-4'* ]] || \
  fail 'bridge did not receive the normal API key file list'
[[ "$bridge_normalized" == *'--env CLASHLENS_INTERACTIVE_API_KEY_FILES=interactive-1=/run/secrets/interactive-1'* ]] || \
  fail 'bridge did not receive the interactive API key file list'

required_normalized=$required_run
[[ "$required_normalized" == *'clashlens-collector-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400'* ]] || \
  fail 'collector role database secret was not mounted'
[[ "$required_normalized" == *'--env CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url'* ]] || \
  fail 'collector did not receive its role database URL file setting'
[[ "$required_normalized" == *'clashlens-collector-archive-access-key,type=mount,target=/run/secrets/archive-access-key'* ]] || \
  fail 'collector archive access secret was not mounted'
[[ "$required_normalized" == *'clashlens-collector-archive-secret-key,type=mount,target=/run/secrets/archive-secret-key'* ]] || \
  fail 'collector archive secret secret was not mounted'
[[ "$required_normalized" == *'--env CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key'* ]] || \
  fail 'collector archive access key file setting is missing'
[[ "$required_normalized" == *'--env CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key'* ]] || \
  fail 'collector archive secret key file setting is missing'
[[ "$required_normalized" == *'--env CLASHLENS_SCHEMA_VERSION=2'* ]] || fail 'required collector schema version is missing'
[[ "$required_normalized" == *'--env CLASHLENS_SHARED_TRAFFIC_GATE_MODE=required'* ]] || fail 'required collector mode is missing'
[[ "$required_normalized" == *'--env CLASHLENS_API_BASE_URL=https://api.clashofclans.com'* ]] || \
  fail 'collector did not receive the official API origin through its runtime setting'
[[ "$required_normalized" == *'--env CLASHLENS_API_PROXY_URL=http://100.108.3.103:3128'* ]] || \
  fail 'collector did not receive the fixed-egress proxy through its runtime setting'
[[ "$required_normalized" == *'--env CLASHLENS_NORMAL_API_KEY_FILES=normal-1=/run/secrets/normal-1,normal-2=/run/secrets/normal-2,normal-3=/run/secrets/normal-3,normal-4=/run/secrets/normal-4'* ]] || \
  fail 'collector did not receive the normal API key file list'
[[ "$required_normalized" == *'--env CLASHLENS_INTERACTIVE_API_KEY_FILES=interactive-1=/run/secrets/interactive-1'* ]] || \
  fail 'collector did not receive the interactive API key file list'
[[ "$required_normalized" != *'--env CLASHLENS_OFFICIAL_API_ORIGIN='* \
  && "$required_normalized" != *'--env CLASHLENS_OFFICIAL_API_PROXY_URL='* ]] || \
  fail 'collector received deployment-only official API setting names'

postgres_run=$(grep '^run ' "$FRESH_NORM" | grep 'postgres:17-alpine')
postgres_normalized=$(norm_log <<<"$postgres_run")
[[ "$postgres_normalized" != *'--env-file'* ]] || fail 'PostgreSQL received the full app.env file'
[[ "$postgres_normalized" != *'--env POSTGRES_PASSWORD '* ]] || fail 'PostgreSQL password was passed through environment metadata'
[[ "$postgres_normalized" == *'--env POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password'* ]] || \
  fail 'PostgreSQL password file setting is missing'
[[ "$postgres_normalized" == *'clashlens-postgres-password,type=mount,target=/run/secrets/postgres-password,uid=70,gid=70,mode=0400'* ]] || \
  fail 'PostgreSQL password secret mount is missing'
[[ "$postgres_normalized" == *'--memory 512m'* && "$postgres_normalized" == *'--pids-limit 256'* && "$postgres_normalized" == *'--cpus 1.5'* ]] || \
  fail 'PostgreSQL did not receive its explicit resource budget'

[[ "$required_normalized" == *'--memory 256m'* && "$required_normalized" == *'--pids-limit 128'* && "$required_normalized" == *'--cpus 1.0'* ]] || \
  fail 'collector did not receive its explicit resource budget'
[[ "$required_normalized" == *'--health-cmd'* ]] || fail 'collector has no health check'

for setting in CLASHLENS_COLLECTOR_DB_PASSWORD CLASHLENS_WORKER_DB_PASSWORD CLASHLENS_API_DB_PASSWORD \
  CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY CLASHLENS_WORKER_ARCHIVE_SECRET_KEY \
  CLASHLENS_HMAC_SECRET_FILE CLASHLENS_HMAC_CALLER CLASHLENS_HMAC_KEY_ID \
  CLASHLENS_INTERACTIVE_API_KEY_FILE; do
  [[ "$required_normalized" != *"--env $setting "* ]] || \
    fail "$setting leaked into collector environment metadata"
done
printf 'ok: fresh up runs bridge before migration 0002 and replaces it with the required collector\n'

# ---------------------------------------------------------------------------
# Scenario B: up on a populated v1 database never reapplies migration 0001.
# ---------------------------------------------------------------------------
V1_DIR=$(new_scenario)
V1_ENV="$V1_DIR/app.env"
write_scenario_env "$V1_ENV" "$V1_DIR/keys"
printf '1' >"$V1_DIR/state/contract_version"
FAKE_STATE="$V1_DIR/state" FAKE_PODMAN_LOG="$V1_DIR/podman.log" \
  "$FAKE_BIN/podman" run --detach --name clashlens-collector \
  localhost/clashlens-collector:previous >/dev/null
FAKE_STATE="$V1_DIR/state" FAKE_PODMAN_LOG="$V1_DIR/podman.log" \
  "$FAKE_BIN/podman" container exists clashlens-collector || \
  fail 'populated-v1 scenario did not create the existing collector'
v1_existing_state=$(
  FAKE_STATE="$V1_DIR/state" FAKE_PODMAN_LOG="$V1_DIR/podman.log" \
    "$FAKE_BIN/podman" container inspect --format '{{.State.Running}}' clashlens-collector
)
[[ "$v1_existing_state" == "true" ]] || \
  fail 'populated-v1 scenario collector is not running'
deploy "$V1_DIR" "$V1_ENV" -- up >/dev/null
if grep -Flq 'CREATE TABLE IF NOT EXISTS collector_jobs (' "$V1_DIR/state/stdin"/exec-* 2>/dev/null; then
  fail 'migration 0001 was reapplied on a populated v1 database'
fi
grep -lq 'collector_reset_baseline_sweeps' "$V1_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'migration 0002 was not applied on a v1 database'
grep -lq 'ALTER ROLE clashlens_collector' "$V1_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'roles were not configured on a v1 database'
log_has "$V1_DIR/podman.log" 'clashlens-collector-bridge' 'bridge was not used on a v1 database'
log_has "$V1_DIR/podman.log" 'clashlens-collector:deployment' 'required collector was not started on a v1 database'
v1_stop_line=$(first_line "$V1_DIR/podman.log" '^stop --time 30 clashlens-collector ' || true)
v1_remove_line=$(first_line "$V1_DIR/podman.log" '^rm clashlens-collector ' || true)
v1_bridge_line=$(first_line "$V1_DIR/podman.log" '^run .*--name clashlens-collector-bridge ' || true)
[[ -n "$v1_stop_line" && -n "$v1_remove_line" && -n "$v1_bridge_line" ]] || \
  fail 'could not locate the populated-v1 collector stop, removal, and bridge start'
(( v1_stop_line < v1_remove_line )) || \
  fail 'populated-v1 collector was not stopped before removal'
(( v1_remove_line < v1_bridge_line )) || \
  fail 'populated-v1 collector was not removed before the bridge started'
assert_no_sentinel_in_log "$V1_DIR/podman.log"
printf 'ok: up on v1 stops the existing collector before applying 0002 through the bridge\n'

# ---------------------------------------------------------------------------
# Scenario C: up on v2 reapplies only 0002 and never runs a bridge.
# ---------------------------------------------------------------------------
V2_DIR=$(new_scenario)
V2_ENV="$V2_DIR/app.env"
write_scenario_env "$V2_ENV" "$V2_DIR/keys"
printf '2' >"$V2_DIR/state/contract_version"
deploy "$V2_DIR" "$V2_ENV" -- up >/dev/null
[[ -n "$(find "$V2_DIR/state/stdin" -maxdepth 1 -type f)" ]] || fail 'no psql stdin was captured on a v2 database'
if grep -Flq 'CREATE TABLE IF NOT EXISTS collector_jobs (' "$V2_DIR/state/stdin"/exec-* 2>/dev/null; then
  fail 'migration 0001 was applied on a v2 database'
fi
[[ "$(grep -l 'collector_reset_baseline_sweeps' "$V2_DIR/state/stdin"/exec-* 2>/dev/null | wc -l)" == "1" ]] || \
  fail 'migration 0002 was not reapplied exactly once on a v2 database'
grep -lq 'ALTER ROLE clashlens_collector' "$V2_DIR/state/stdin"/exec-* 2>/dev/null || \
  fail 'roles were not configured on a v2 database'
log_lacks "$V2_DIR/podman.log" 'clashlens-collector-bridge' 'bridge collector was started on a v2 database'
log_has "$V2_DIR/podman.log" 'clashlens-collector:deployment' 'required collector was not started on a v2 database'
assert_no_sentinel_in_log "$V2_DIR/podman.log"
printf 'ok: up on a v2 database reapplies only 0002 and starts the required collector\n'

# ---------------------------------------------------------------------------
# Scenario D: init applies 0001 only on an absent database.
# ---------------------------------------------------------------------------
INIT_DIR=$(new_scenario)
INIT_ENV="$INIT_DIR/app.env"
write_scenario_env "$INIT_ENV" "$INIT_DIR/keys"
deploy "$INIT_DIR" "$INIT_ENV" -- init >/dev/null
grep -Flq 'CREATE TABLE IF NOT EXISTS collector_jobs (' "$INIT_DIR/state/stdin"/exec-* || \
  fail 'init did not apply migration 0001 on an absent database'
if grep -lq 'collector_reset_baseline_sweeps' "$INIT_DIR/state/stdin"/exec-*; then
  fail 'init applied migration 0002'
fi
log_lacks "$INIT_DIR/podman.log" '^run .*clashlens-collector' 'init started a collector container'

INIT_V1_DIR=$(new_scenario)
INIT_V1_ENV="$INIT_V1_DIR/app.env"
write_scenario_env "$INIT_V1_ENV" "$INIT_V1_DIR/keys"
printf '1' >"$INIT_V1_DIR/state/contract_version"
deploy_fails "$INIT_V1_DIR" "$INIT_V1_ENV" 'already initialized' -- init

INIT_V2_DIR=$(new_scenario)
INIT_V2_ENV="$INIT_V2_DIR/app.env"
write_scenario_env "$INIT_V2_ENV" "$INIT_V2_DIR/keys"
printf '2' >"$INIT_V2_DIR/state/contract_version"
deploy_fails "$INIT_V2_DIR" "$INIT_V2_ENV" 'already initialized' -- init
printf 'ok: init applies 0001 only on an absent database\n'

# ---------------------------------------------------------------------------
# Scenario E: restart is a start-only v2 recovery path.
# ---------------------------------------------------------------------------
RESTART_DIR=$(new_scenario)
RESTART_ENV="$RESTART_DIR/app.env"
write_scenario_env "$RESTART_ENV" "$RESTART_DIR/keys"
printf '2' >"$RESTART_DIR/state/contract_version"
mkdir -p "$RESTART_DIR/state/images/localhost"
: >"$RESTART_DIR/state/images/localhost/clashlens-collector:deployment"
deploy "$RESTART_DIR" "$RESTART_ENV" -- restart >/dev/null
log_lacks "$RESTART_DIR/podman.log" '^build ' 'restart rebuilt an image'
log_lacks "$RESTART_DIR/podman.log" '^exec --interactive ' 'restart ran SQL'
log_lacks "$RESTART_DIR/podman.log" 'clashlens-collector-bridge' 'restart started a bridge collector'
log_has "$RESTART_DIR/podman.log" 'clashlens-collector:deployment' 'restart did not start the required collector'
assert_no_sentinel_in_log "$RESTART_DIR/podman.log"

RESTART_ABSENT_DIR=$(new_scenario)
RESTART_ABSENT_ENV="$RESTART_ABSENT_DIR/app.env"
write_scenario_env "$RESTART_ABSENT_ENV" "$RESTART_ABSENT_DIR/keys"
deploy_fails "$RESTART_ABSENT_DIR" "$RESTART_ABSENT_ENV" 'restart requires contract version 2' -- restart

UNKNOWN_DIR=$(new_scenario)
UNKNOWN_ENV="$UNKNOWN_DIR/app.env"
write_scenario_env "$UNKNOWN_ENV" "$UNKNOWN_DIR/keys"
printf '3' >"$UNKNOWN_DIR/state/contract_version"
deploy_fails "$UNKNOWN_DIR" "$UNKNOWN_ENV" 'unsupported contract version' -- up
printf 'ok: restart is start-only for v2 and unknown versions are rejected\n'

# ---------------------------------------------------------------------------
# Scenario F: python-up builds then starts API and worker; start paths never
# build; the API is private with a stable alias and no published port.
# ---------------------------------------------------------------------------
PY_DIR=$(new_scenario)
PY_ENV="$PY_DIR/app.env"
write_scenario_env "$PY_ENV" "$PY_DIR/keys"
deploy "$PY_DIR" "$PY_ENV" -- up >/dev/null
deploy "$PY_DIR" "$PY_ENV" -- python-up >/dev/null

PY_LOG="$PY_DIR/podman.log"
PY_NORM="$PY_DIR/podman.norm.log"
norm_log "$PY_LOG" >"$PY_NORM"
api_run=$(grep '^run ' "$PY_NORM" | grep 'clashlens-python:deployment' | grep 'serve ')
worker_run=$(grep '^run ' "$PY_NORM" | grep 'clashlens-python:deployment' | grep 'worker ')
[[ -n "$api_run" ]] || fail 'private Python API was not started'
[[ -n "$worker_run" ]] || fail 'production Python worker was not started'
api_normalized=$(norm_log <<<"$api_run")
worker_normalized=$(norm_log <<<"$worker_run")

[[ "$api_normalized" == *'--network clashlens-private'* ]] || fail 'API did not use the private network'
[[ "$api_normalized" == *'--network-alias python-api'* ]] || fail 'API has no stable network alias'
[[ "$api_normalized" != *'--publish'* ]] || fail 'API published a host port'
[[ "$worker_normalized" == *'--network clashlens-private'* ]] || fail 'worker did not use the private network'
[[ "$worker_normalized" != *'--publish'* ]] || fail 'worker published a host port'

[[ "$api_normalized" == *'clashlens-python-api-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400'* ]] || \
  fail 'API role database secret was not mounted'
[[ "$api_normalized" == *'clashlens-python-api-hmac-current,type=mount,target=/run/secrets/clashlens-hmac-current,uid=10001,gid=10001,mode=0400'* ]] || \
  fail 'API HMAC current secret was not mounted'
[[ "$api_normalized" == *'clashlens-python-api-interactive-key,type=mount,target=/run/secrets/interactive-1,uid=10001,gid=10001,mode=0400'* ]] || \
  fail 'API interactive official key secret was not mounted'
[[ "$api_normalized" == *'--env CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url'* ]] || \
  fail 'API database URL file setting is missing'
[[ "$api_normalized" == *'--env CLASHLENS_HMAC_SECRET_FILE=/run/secrets/clashlens-hmac-current'* ]] || \
  fail 'API HMAC secret file setting is missing'
[[ "$api_normalized" == *'--env CLASHLENS_HMAC_CALLER=typescript-website'* ]] || \
  fail 'API HMAC caller setting is missing'
[[ "$api_normalized" == *'--env CLASHLENS_HMAC_KEY_ID=current'* ]] || \
  fail 'API HMAC key ID setting is missing'
[[ "$api_normalized" == *'--env CLASHLENS_OFFICIAL_KEY_FILE=/run/secrets/interactive-1'* ]] || \
  fail 'API official key file setting is missing'
[[ "$api_normalized" == *'--env CLASHLENS_OFFICIAL_PROXY_URL=http://100.108.3.103:3128'* ]] || \
  fail 'API did not receive the fixed-egress proxy URL'
[[ "$api_normalized" != *'--env CLASHLENS_INTERACTIVE_API_KEY_FILE='* && "$api_normalized" != *'--env CLASHLENS_OFFICIAL_API_PROXY_URL='* ]] || \
  fail 'API received a legacy CLI env name'
[[ "$api_normalized" == *'serve --host 0.0.0.0 --port 8000'* ]] || \
  fail 'API did not start through the fixed CLI serve seam'
[[ "$api_normalized" == *'probe --url http://127.0.0.1:8000/readyz'* ]] || \
  fail 'API health does not use the fixed CLI probe seam'
[[ "$api_normalized" == *'--memory 192m'* && "$api_normalized" == *'--pids-limit 128'* && "$api_normalized" == *'--cpus 0.5'* ]] || \
  fail 'API did not receive its explicit resource budget'
[[ "$api_normalized" != *'archive'* ]] || fail 'API received archive settings'

[[ "$worker_normalized" == *'clashlens-python-worker-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400'* ]] || \
  fail 'worker role database secret was not mounted'
[[ "$worker_normalized" == *'clashlens-python-worker-archive-access-key,type=mount,target=/run/secrets/archive-access-key'* ]] || \
  fail 'worker read-only archive access secret was not mounted'
[[ "$worker_normalized" == *'clashlens-python-worker-archive-secret-key,type=mount,target=/run/secrets/archive-secret-key'* ]] || \
  fail 'worker read-only archive secret secret was not mounted'
[[ "$worker_normalized" == *'--env CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key'* ]] || \
  fail 'worker archive access key file setting is missing'
[[ "$worker_normalized" == *'worker --owner production-python-1 --max-jobs 100 --lease-seconds 60 --run-forever'* ]] || \
  fail 'worker did not receive the configured lease and bounds'
[[ "$worker_normalized" == *'ready --expected-contract-version 2'* ]] || \
  fail 'worker health does not use the ready seam'
[[ "$worker_normalized" == *'--memory 384m'* && "$worker_normalized" == *'--pids-limit 256'* && "$worker_normalized" == *'--cpus 1.0'* ]] || \
  fail 'worker did not receive its explicit resource budget'
[[ "$worker_normalized" != *'collector-archive-secret-do-not-print'* ]] || \
  fail 'worker received the collector archive secret'
[[ "$worker_normalized" != *'clashlens-normal-'* && "$worker_normalized" != *'clashlens-interactive-1'* ]] || \
  fail 'worker received official API key secrets'
[[ "$worker_normalized" != *'HMAC'* && "$worker_normalized" != *'clashlens-python-api-'* ]] || \
  fail 'worker received API or HMAC settings'
[[ "$api_normalized" != *'collector-archive-secret-do-not-print'* && "$api_normalized" != *'worker-archive-secret-do-not-print'* ]] || \
  fail 'API received archive credentials'
assert_no_sentinel_in_log "$PY_LOG"

build_count_before=$(grep -c '^build ' "$PY_LOG")
deploy "$PY_DIR" "$PY_ENV" -- python-start >/dev/null
build_count_after=$(grep -c '^build ' "$PY_LOG")
[[ "$build_count_after" == "$build_count_before" ]] || fail 'python-start rebuilt the python image'
deploy "$PY_DIR" "$PY_ENV" -- api-start >/dev/null
[[ "$(grep -c '^build ' "$PY_LOG")" == "$build_count_after" ]] || fail 'api-start rebuilt an image'
deploy "$PY_DIR" "$PY_ENV" -- worker-start >/dev/null
[[ "$(grep -c '^build ' "$PY_LOG")" == "$build_count_after" ]] || fail 'worker-start rebuilt an image'
printf 'ok: python-up builds once; API and worker start paths are private, scoped, and build-free\n'

# ---------------------------------------------------------------------------
# Scenario G: rollback selects an existing image tag and never builds.
# ---------------------------------------------------------------------------
ROLLBACK_DIR=$(new_scenario)
ROLLBACK_ENV="$ROLLBACK_DIR/app.env"
write_scenario_env "$ROLLBACK_ENV" "$ROLLBACK_DIR/keys"
printf '2' >"$ROLLBACK_DIR/state/contract_version"
mkdir -p "$ROLLBACK_DIR/state/networks" "$ROLLBACK_DIR/state/containers" "$ROLLBACK_DIR/state/images/localhost"
mkdir -p "$ROLLBACK_DIR/state/networks/clashlens-private"
: >"$ROLLBACK_DIR/state/containers/clashlens-postgres"
: >"$ROLLBACK_DIR/state/containers/clashlens-postgres.running"
: >"$ROLLBACK_DIR/state/images/localhost/clashlens-python:previous-release"
deploy "$ROLLBACK_DIR" "$ROLLBACK_ENV" \
  CLASHLENS_PYTHON_IMAGE=localhost/clashlens-python:previous-release -- python-start >/dev/null
log_lacks "$ROLLBACK_DIR/podman.log" '^build ' 'rollback path rebuilt an image'
log_has "$ROLLBACK_DIR/podman.log" 'clashlens-python:previous-release' \
  'rollback did not start the previous immutable image tag'
printf 'ok: rollback selects an existing image tag through start-only commands\n'

# ---------------------------------------------------------------------------
# Scenario H: website deployment is private except for its configured ingress,
# receives only the HMAC file, and has a start-only recovery path.
# ---------------------------------------------------------------------------
WEBSITE_DIR=$(new_scenario)
WEBSITE_ENV="$WEBSITE_DIR/app.env"
write_scenario_env "$WEBSITE_ENV" "$WEBSITE_DIR/keys"
deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- up >/dev/null
deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- python-up >/dev/null
deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- website-up >/dev/null
WEBSITE_LOG="$WEBSITE_DIR/podman.log"
WEBSITE_NORM="$WEBSITE_DIR/podman.norm.log"
norm_log "$WEBSITE_LOG" >"$WEBSITE_NORM"
website_build=$(grep '^build ' "$WEBSITE_NORM" | grep 'clashlens-website:deployment')
[[ "$website_build" == *"--file $ROOT_DIR/website/Containerfile"* && "$website_build" == *"$ROOT_DIR/website"* ]] || \
  fail 'website image build did not use website/Containerfile and website context'
website_run=$(grep '^run ' "$WEBSITE_NORM" | grep 'clashlens-website:deployment')
[[ -n "$website_run" ]] || fail 'website container was not started'
[[ "$website_run" == *'--network clashlens-private'* ]] || fail 'website did not use the private network'
[[ "$website_run" == *'--publish 127.0.0.1:13000:3000/tcp'* ]] || fail 'website did not publish only the configured ingress'
for setting in 'NODE_ENV=production' 'CLASHLENS_PYTHON_API_URL=http://python-api:8000' \
  'CLASHLENS_PYTHON_HMAC_CALLER=typescript-website' 'CLASHLENS_PYTHON_HMAC_KEY_ID=current' \
  'CLASHLENS_PYTHON_HMAC_SECRET_FILE=/run/secrets/clashlens-python-hmac' \
  'CLASHLENS_TRUST_PROXY=false'; do
  [[ "$website_run" == *"--env $setting"* ]] || fail "website is missing $setting"
done
[[ "$website_run" == *'clashlens-python-api-hmac-current,type=mount,target=/run/secrets/clashlens-python-hmac,uid=1000,gid=1000,mode=0400'* ]] || \
  fail 'website HMAC secret mount is missing or has unsafe ownership'
[[ "$(grep -o -- '--secret [^ ]*' <<<"$website_run" | wc -l)" == '1' ]] || fail 'website received more than one secret'
for forbidden in database archive official interactive collector worker admin postgres; do
  [[ "$website_run" != *"$forbidden"* ]] || fail "website received forbidden $forbidden metadata"
done
[[ "$website_run" == *'--read-only'* && "$website_run" == *'--tmpfs /tmp:rw,noexec,nosuid,nodev'* \
  && "$website_run" == *'--cap-drop all'* && "$website_run" == *'--security-opt no-new-privileges'* ]] || \
  fail 'website hardening is incomplete'
[[ "$website_run" == *'--memory 128m'* && "$website_run" == *'--pids-limit 128'* && "$website_run" == *'--cpus 0.5'* ]] || \
  fail 'website did not receive its explicit resource budget'
[[ "$website_run" == *'--health-cmd'* && "$website_run" == *'node'* && "$website_run" == *'127.0.0.1:3000/healthz'* \
  && "$website_run" == *'--health-interval 10s'* && "$website_run" == *'--health-timeout 3s'* && "$website_run" == *'--health-retries 12'* ]] || \
  fail 'website health check is not bounded and Node-based'
assert_no_sentinel_in_log "$WEBSITE_LOG"
build_count_before=$(grep -c '^build ' "$WEBSITE_LOG")
sql_count_before=$(grep -c '^exec --interactive ' "$WEBSITE_LOG" || true)
deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- website-start >/dev/null
[[ "$(grep -c '^build ' "$WEBSITE_LOG")" == "$build_count_before" ]] || fail 'website-start rebuilt an image'
[[ "$(grep -c '^exec --interactive ' "$WEBSITE_LOG" || true)" == "$sql_count_before" ]] || fail 'website-start ran SQL'
deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- logs website >/dev/null
grep -q '^logs clashlens-website' "$WEBSITE_LOG" || fail 'website logs did not target the website container'
output=$(deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- status)
[[ "$output" == *'website: running'* ]] || fail 'status did not show the website'
website_down_before=$(wc -l <"$WEBSITE_LOG")
deploy "$WEBSITE_DIR" "$WEBSITE_ENV" -- website-down >/dev/null
website_down_log=$(tail -n +"$((website_down_before + 1))" "$WEBSITE_LOG")
grep -q '^stop --time 30 clashlens-website' <<<"$website_down_log" || fail 'website-down was not graceful'
grep -q '^rm clashlens-website *$' <<<"$website_down_log" || fail 'website-down did not remove the website'
printf 'ok: website build, start-only recovery, ingress, secrets, and hardening are scoped\n'

# ---------------------------------------------------------------------------
# Scenario I: lifecycle stops are graceful and ordered.
# ---------------------------------------------------------------------------
LIFE_DIR=$(new_scenario)
LIFE_ENV="$LIFE_DIR/app.env"
write_scenario_env "$LIFE_ENV" "$LIFE_DIR/keys"
deploy "$LIFE_DIR" "$LIFE_ENV" -- up >/dev/null
deploy "$LIFE_DIR" "$LIFE_ENV" -- python-up >/dev/null
deploy "$LIFE_DIR" "$LIFE_ENV" -- website-up >/dev/null
LIFE_LOG="$LIFE_DIR/podman.log"

segment_before=$(wc -l <"$LIFE_LOG")
deploy "$LIFE_DIR" "$LIFE_ENV" -- api-down >/dev/null
api_down_segment=$(tail -n +"$((segment_before + 1))" "$LIFE_LOG")
grep -q '^stop --time 30 clashlens-website' <<<"$api_down_segment" || fail 'api-down did not stop website first'
grep -q '^stop --time 30 clashlens-python-api' <<<"$api_down_segment" || fail 'api-down was not graceful'
website_stop_line=$(grep -n '^stop --time 30 clashlens-website' <<<"$api_down_segment" | head -n1 | cut -d: -f1)
api_stop_line=$(grep -n '^stop --time 30 clashlens-python-api' <<<"$api_down_segment" | head -n1 | cut -d: -f1)
(( website_stop_line < api_stop_line )) || fail 'api-down did not stop website before API'
grep -q '^rm clashlens-python-api *$' <<<"$api_down_segment" || fail 'api-down did not remove the API container'
grep -q 'clashlens-python-worker' <<<"$api_down_segment" && fail 'api-down touched the worker'
grep -q 'clashlens-postgres' <<<"$api_down_segment" && fail 'api-down touched PostgreSQL'

segment_before=$(wc -l <"$LIFE_LOG")
deploy "$LIFE_DIR" "$LIFE_ENV" -- worker-down >/dev/null
worker_down_segment=$(tail -n +"$((segment_before + 1))" "$LIFE_LOG")
grep -q '^stop --time 70 clashlens-python-worker' <<<"$worker_down_segment" || \
  fail 'worker stop grace is not at least the configured job lease plus margin'
grep -q '^rm clashlens-python-worker *$' <<<"$worker_down_segment" || fail 'worker-down did not remove the worker'

deploy "$LIFE_DIR" "$LIFE_ENV" -- python-up >/dev/null
deploy "$LIFE_DIR" "$LIFE_ENV" -- website-up >/dev/null
down_before=$(wc -l <"$LIFE_LOG")
deploy "$LIFE_DIR" "$LIFE_ENV" -- down >/dev/null
down_log=$(tail -n +"$((down_before + 1))" "$LIFE_LOG")
grep -q '^stop --time 30 clashlens-website' <<<"$down_log" || fail 'down did not stop the website first'
grep -q '^stop --time 70 clashlens-python-worker' <<<"$down_log" || fail 'down did not stop the worker first'
website_stop_line=$(grep -n '^stop --time 30 clashlens-website' <<<"$down_log" | head -n1 | cut -d: -f1)
worker_stop_line=$(grep -n '^stop --time 70 clashlens-python-worker' <<<"$down_log" | head -n1 | cut -d: -f1)
api_stop_line=$(grep -n '^stop --time 30 clashlens-python-api' <<<"$down_log" | head -n1 | cut -d: -f1)
collector_stop_line=$(grep -n '^stop --time 30 clashlens-collector' <<<"$down_log" | head -n1 | cut -d: -f1)
postgres_stop_line=$(grep -n '^stop --time 60 clashlens-postgres' <<<"$down_log" | head -n1 | cut -d: -f1)
[[ -n "$website_stop_line" && -n "$worker_stop_line" && -n "$api_stop_line" && -n "$collector_stop_line" && -n "$postgres_stop_line" ]] || \
  fail 'down did not stop every container'
(( website_stop_line < worker_stop_line && worker_stop_line < api_stop_line && api_stop_line < collector_stop_line && collector_stop_line < postgres_stop_line )) || \
  fail 'down did not stop dependents before PostgreSQL'
grep -q 'rm --force' "$LIFE_LOG" && fail 'a lifecycle stop used rm --force'
printf 'ok: lifecycle stops are graceful, ordered, and preserve the data volume\n'

# ---------------------------------------------------------------------------
# Scenario I: status shows health and queue status without secrets.
# ---------------------------------------------------------------------------
STATUS_DIR=$(new_scenario)
STATUS_ENV="$STATUS_DIR/app.env"
write_scenario_env "$STATUS_ENV" "$STATUS_DIR/keys"
deploy "$STATUS_DIR" "$STATUS_ENV" -- up >/dev/null
deploy "$STATUS_DIR" "$STATUS_ENV" -- python-up >/dev/null
for name in clashlens-postgres clashlens-collector clashlens-python-api clashlens-python-worker; do
  printf 'healthy\n' >"$STATUS_DIR/state/containers/$name.health"
done
output=$(deploy "$STATUS_DIR" "$STATUS_ENV" -- status)
[[ "$output" == *'postgres: running (healthy)'* ]] || fail 'status did not show postgres health'
[[ "$output" == *'collector: running (healthy)'* ]] || fail 'status did not show collector health'
[[ "$output" == *'python-api: running (healthy)'* ]] || fail 'status did not show API health'
[[ "$output" == *'python-worker: running (healthy)'* ]] || fail 'status did not show worker health'
[[ "$output" == *'python queue: {'* ]] || fail 'status did not invoke the worker queue-status seam'
grep -q 'queue-status' "$STATUS_DIR/podman.log" || fail 'status did not invoke queue-status through the CLI seam'
assert_no_sentinel_in_log <(printf '%s\n' "$output")
printf 'ok: status shows health and queue status without loading unrelated secrets\n'

# ---------------------------------------------------------------------------
# Scenario J: systemd units are start-only, role-specific, and ordered.
# ---------------------------------------------------------------------------
stack_unit=$(<"$ROOT_DIR/deploy/systemd/clashlens.service")
worker_unit=$(<"$ROOT_DIR/deploy/systemd/clashlens-python-worker.service")
api_unit=$(<"$ROOT_DIR/deploy/systemd/clashlens-python-api.service")
website_unit=$(<"$ROOT_DIR/deploy/systemd/clashlens-website.service")
[[ "$website_unit" == *'Requires=clashlens-python-api.service clashlens.service'* ]] || fail 'website unit does not require the API and stack'
[[ "$website_unit" == *'ExecStart=%h/clashlens/deploy.sh website-start'* && "$website_unit" == *'ExecStop=%h/clashlens/deploy.sh website-down'* ]] || \
  fail 'website unit is not start-only and scoped'
[[ "$stack_unit" == *'ExecStart=%h/clashlens/deploy.sh restart'* ]] || \
  fail 'collector stack unit does not use the start-only restart command'
[[ "$stack_unit" == *'ExecStop=%h/clashlens/deploy.sh stack-down'* ]] || \
  fail 'collector stack unit has no scoped stop command'
[[ "$stack_unit" != *'deploy.sh up'* && "$stack_unit" != *'deploy.sh python-up'* ]] || \
  fail 'collector stack unit runs a building command at boot'
[[ "$worker_unit" == *'ExecStart=%h/clashlens/deploy.sh worker-start'* ]] || \
  fail 'worker unit does not use the start-only worker-start command'
[[ "$worker_unit" == *'ExecStop=%h/clashlens/deploy.sh worker-down'* ]] || \
  fail 'worker unit has no role-specific stop command'
[[ "$worker_unit" == *'Requires=clashlens.service'* ]] || fail 'worker unit does not require the collector stack'
[[ "$api_unit" == *'ExecStart=%h/clashlens/deploy.sh api-start'* ]] || \
  fail 'API unit does not use the start-only api-start command'
[[ "$api_unit" == *'ExecStop=%h/clashlens/deploy.sh api-down'* ]] || \
  fail 'API unit has no role-specific stop command'
[[ "$api_unit" == *'Requires=clashlens.service'* ]] || fail 'API unit does not require the collector stack'
for unit in "$stack_unit" "$worker_unit" "$api_unit"; do
  [[ "$unit" == *'TimeoutStopSec=2min'* ]] || fail 'a unit lost its stop timeout'
done
[[ "$worker_unit" != *'python-up'* && "$api_unit" != *'python-up'* ]] || \
  fail 'a python unit builds at boot'
[[ "$api_unit" != *'worker-start'* && "$worker_unit" != *'api-start'* ]] || \
  fail 'python units are not role-specific'
printf 'ok: systemd units are start-only, role-specific, and ordered\n'

# ---------------------------------------------------------------------------
# Scenario K: tracked integration files stay aligned with the script.
# ---------------------------------------------------------------------------
ENV_EXAMPLE="$ROOT_DIR/app.env.example"
[[ -f "$ENV_EXAMPLE" ]] || fail 'app.env.example is missing'
env_example=$(<"$ENV_EXAMPLE")
for setting in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
  CLASHLENS_COLLECTOR_DB_PASSWORD CLASHLENS_WORKER_DB_PASSWORD \
  CLASHLENS_API_DB_PASSWORD CLASHLENS_ARCHIVE_ENDPOINT \
  CLASHLENS_ARCHIVE_SECURE CLASHLENS_ARCHIVE_BUCKET \
  CLASHLENS_ARCHIVE_ACCESS_KEY CLASHLENS_ARCHIVE_SECRET_KEY \
  CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY CLASHLENS_WORKER_ARCHIVE_SECRET_KEY \
  CLASHLENS_OFFICIAL_API_ORIGIN CLASHLENS_OFFICIAL_API_PROXY_URL \
  CLASHLENS_NORMAL_API_KEY_FILES CLASHLENS_INTERACTIVE_API_KEY_FILES \
  CLASHLENS_API_KEY_HOST_DIR CLASHLENS_INTERACTIVE_API_KEY_FILE \
  CLASHLENS_HMAC_CALLER CLASHLENS_HMAC_KEY_ID CLASHLENS_HMAC_SECRET_FILE \
  CLASHLENS_WORKER_LEASE_SECONDS CLASHLENS_HEALTH_HOST CLASHLENS_HEALTH_PORT \
  CLASHLENS_POSTGRES_MEMORY CLASHLENS_POSTGRES_CPUS CLASHLENS_POSTGRES_PIDS \
  CLASHLENS_COLLECTOR_MEMORY CLASHLENS_COLLECTOR_CPUS CLASHLENS_COLLECTOR_PIDS \
  CLASHLENS_API_MEMORY CLASHLENS_API_CPUS CLASHLENS_API_PIDS \
  CLASHLENS_WORKER_MEMORY CLASHLENS_WORKER_CPUS CLASHLENS_WORKER_PIDS \
  CLASHLENS_WEBSITE_HOST CLASHLENS_WEBSITE_PORT \
  CLASHLENS_WEBSITE_MEMORY CLASHLENS_WEBSITE_CPUS CLASHLENS_WEBSITE_PIDS; do
  [[ "$env_example" == *"$setting="* ]] || fail "app.env.example is missing $setting"
done
for rejected in CLASHLENS_DATABASE_URL= CLASHLENS_PYTHON_WORKER_IMAGE \
  CLASHLENS_NORMAL_API_KEYS= CLASHLENS_INTERACTIVE_API_KEYS= \
  CLASHLENS_ENABLE_GLOBAL_RANKINGS= CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN \
  CLASHLENS_ALLOW_REDUCED_KEY_POOLS CLASHLENS_OFFICIAL_KEY_FILE= \
  CLASHLENS_OFFICIAL_PROXY_URL=; do
  [[ "$env_example" != *"$rejected"* ]] || fail "app.env.example still documents the rejected setting $rejected"
done
for password_setting in POSTGRES_PASSWORD CLASHLENS_COLLECTOR_DB_PASSWORD \
  CLASHLENS_WORKER_DB_PASSWORD CLASHLENS_API_DB_PASSWORD; do
  grep -Eq "^$password_setting=CHANGE_ME$" "$ENV_EXAMPLE" || \
    fail "app.env.example does not keep $password_setting as an honest CHANGE_ME placeholder"
done
printf 'ok: app.env.example covers every required setting and rejects obsolete settings\n'

for unit in clashlens.service clashlens-python-worker.service clashlens-python-api.service; do
  [[ -f "$ROOT_DIR/deploy/systemd/$unit" ]] || fail "systemd unit $unit is missing"
  unit_content=$(<"$ROOT_DIR/deploy/systemd/$unit")
  [[ "$unit_content" != *'deploy.sh up'* && "$unit_content" != *'deploy.sh python-up'* \
    && "$unit_content" != *'build'* ]] || fail "unit $unit runs a build or migrate command"
  [[ "$unit_content" == *'TimeoutStopSec=2min'* ]] || fail "unit $unit lost its stop timeout"
done
[[ -f "$ROOT_DIR/docs/deployment.md" ]] || fail 'docs/deployment.md is missing'
deployment_doc=$(<"$ROOT_DIR/docs/deployment.md")
for marker in 'deploy.sh init' 'deploy.sh up' 'deploy.sh restart' 'deploy.sh python-up' \
  'deploy.sh api-start' 'deploy.sh worker-start' 'deploy.sh python-start' \
  'deploy.sh queue-status' 'deploy.sh stack-down' 'deploy.sh python-down' \
  'deploy.sh api-down' 'deploy.sh worker-down' 'clashlens-python-api.service' \
  'bridge' 'contract version' 'Issue 31' 'python-api' 'resource budget'; do
  [[ "$deployment_doc" == *"$marker"* ]] || fail "docs/deployment.md no longer documents $marker"
done
[[ "$deployment_doc" != *'python-status'* ]] || fail 'docs/deployment.md still references the removed python-status command'
printf 'ok: systemd units and deployment runbook stay aligned with the script\n'

printf 'all deploy regression tests passed\n'
