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
      create) name=${@: -1}; touch "$state/$cmd-$name" "$state/$cmd-$name-owned"; printf '%s\n' "$name" ;;
      inspect)
        name=${@: -1}
        if [[ "${2:-}" == '--format' ]]; then
          [[ -f "$state/$cmd-$name-owned" ]] && printf 'true\n'
        else
          [[ -f "$state/$cmd-$name" ]]
        fi
        ;;
      *) exit 1 ;;
    esac ;;
  secret)
    action=${1:-}; name=${2:-};
    case "$action" in
      inspect)
        name=${@: -1}
        if [[ "${2:-}" == '--format' ]]; then
          [[ -f "$state/secret-$name-owned" ]] && printf 'true\n'
        else
          [[ -f "$state/secret-$name" ]]
        fi
        ;;
      rm) rm -f "$state/secret-$name" "$state/secret-$name-owned" "$state/secret-$name-content" ;;
      create)
        name=${@: -2:1}
        source=${@: -1}
        if [[ "$*" == *'--replace'* ]]; then
          case "$name" in
            clashlens-python-prototype-database-url)
              if [[ -f "$state/container-clashlens-python-prototype-api" || -f "$state/container-clashlens-python-prototype-worker" ]]; then
                touch "$state/secret-$name-replaced-while-consumer-active"
              fi
              ;;
            clashlens-python-prototype-archive-access-key|clashlens-python-prototype-archive-secret-key)
              if [[ -f "$state/container-clashlens-python-prototype-worker" ]]; then
                touch "$state/secret-$name-replaced-while-consumer-active"
              fi
              ;;
          esac
        fi
        touch "$state/secret-$name" "$state/secret-$name-owned"
        cp -- "$source" "$state/secret-$name-content"
        printf '%s\n' "$name"
        ;;
      *) exit 1 ;;
    esac ;;
  container)
    action=${1:-}; name=${2:-};
    [[ "$action" == exists ]] && [[ -f "$state/container-$name" ]] ;;
  run)
    name=''
    user=''
    volume=''
    secret_names=()
    while (($#)); do
      case "$1" in
        --name) name=$2; shift 2 ;;
        --user) user=$2; shift 2 ;;
        --volume) volume=$2; shift 2 ;;
        --secret) secret_names+=("${2%%,*}"); shift 2 ;;
        *) shift ;;
      esac
    done
    [[ -n "$name" ]] || { printf 'missing name\n' >&2; exit 1; }
    touch "$state/container-$name" "$state/container-$name-owned"
    generation=0
    if [[ -f "$state/container-$name-generation" ]]; then
      generation=$(<"$state/container-$name-generation")
    fi
    printf '%s\n' "$((generation + 1))" > "$state/container-$name-generation"
    printf '%s\n' "$user" > "$state/container-$name-user"
    printf '%s\n' "$volume" > "$state/container-$name-volume"
    for secret_name in "${secret_names[@]}"; do
      if [[ -f "$state/secret-$secret_name-content" ]]; then
        cp -- "$state/secret-$secret_name-content" "$state/container-$name-secret-$secret_name-content"
      else
        : > "$state/container-$name-secret-$secret_name-content"
      fi
    done
    printf '%s\n' "id-$name"
    ;;
  rm)
    name=''
    remove_volumes=false
    while (($#)); do
      case "$1" in
        --volumes) remove_volumes=true ;;
        --*) ;;
        *) name=$1 ;;
      esac
      shift
    done
    volume=''
    if [[ -f "$state/container-$name-volume" ]]; then
      volume=$(<"$state/container-$name-volume")
    fi
    rm -f "$state/container-$name" "$state/container-$name-owned" \
      "$state/container-$name-user" "$state/container-$name-volume"
    if [[ "$remove_volumes" == true && -n "$volume" ]]; then
      rm -f "$state/volume-${volume%%:*}"
    fi
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
        if [[ "${FAKE_PODMAN_FAIL_SEED:-false}" == true && "$*" == *seed* ]]; then exit 42; fi
        ;;
    esac
    ;;
  inspect)
    name=${@: -1}
    if [[ "$*" == *'.Config.User'* ]]; then
      [[ -f "$state/container-$name-user" ]] || exit 1
      printf '%s\n' "$(<"$state/container-$name-user")"
    else
      [[ -f "$state/container-$name-owned" ]] && printf 'true\n'
    fi
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

DEPLOY_ENV_PATH="$TEST_ROOT/config/prototype.env"

export FAKE_PODMAN_STATE="$STATE_DIR"
export FAKE_PODMAN_LOG="$LOG_FILE"
export DEPLOY_ENV_FILE="$DEPLOY_ENV_PATH"
export CLASHLENS_SECRET_DIR="$TEST_ROOT/secrets"
export PODMAN_BIN="$FAKE_PODMAN"
export CURL_BIN="$FAKE_CURL"

touch "$STATE_DIR/network-clashlens-python-prototype-network"
if "$ROOT_DIR/deploy.sh" init >/dev/null 2>&1; then
    printf 'deployment accepted an unowned network with the prototype name\n' >&2
    exit 1
fi
rm -f "$STATE_DIR/network-clashlens-python-prototype-network"

"$ROOT_DIR/deploy.sh" init >/dev/null

"$ROOT_DIR/deploy.sh" up >/dev/null

for name in postgres-password archive-access-key archive-secret-key; do
    path="$TEST_ROOT/secrets/$name"
    newline_count=$(tr -cd '\n' < "$path" | wc -c)
    value=$(<"$path")
    [[ "$newline_count" == 1 && "$value" =~ ^[A-Za-z0-9]+$ ]] || {
        printf '%s did not contain one alphanumeric line with one final LF\n' "$name" >&2
        exit 1
    }
    final_byte=$(tail -c 1 "$path" | od -An -t x1 | tr -d ' \n')
    [[ "$final_byte" == 0a ]] || {
        printf '%s did not end with LF\n' "$name" >&2
        exit 1
    }
done

declare -A legacy_values=()
declare -A legacy_metadata=()
declare -A legacy_inodes=()
for name in postgres-password archive-access-key archive-secret-key; do
    path="$TEST_ROOT/secrets/$name"
    legacy_values["$name"]=$(<"$path")
    printf '%s\n\n' "${legacy_values[$name]}" > "$path"
    chmod 600 "$path"
    legacy_metadata["$name"]=$(stat -c '%a:%u:%g' "$path")
    legacy_inodes["$name"]=$(stat -c '%i' "$path")
done

declare -A application_values=()
for name in database-url archive-access-key archive-secret-key; do
    path="$TEST_ROOT/secrets/$name"
    application_values["$name"]=$(<"$path")
    printf '%s\n\n' "${application_values[$name]}" > "$path"
    printf '%s\n\n' "${application_values[$name]}" > "$STATE_DIR/secret-clashlens-python-prototype-$name-content"
    for consumer in api worker; do
        if [[ "$consumer" == api && "$name" != database-url ]]; then
            continue
        fi
        printf '%s\n\n' "${application_values[$name]}" > \
            "$STATE_DIR/container-clashlens-python-prototype-$consumer-secret-clashlens-python-prototype-$name-content"
    done
done
worker_generation_before=$(<"$STATE_DIR/container-clashlens-python-prototype-worker-generation")
api_generation_before=$(<"$STATE_DIR/container-clashlens-python-prototype-api-generation")

if "$ROOT_DIR/deploy.sh" init >/dev/null; then
    :
else
    printf 'deployment failed while normalizing legacy secret files\n' >&2
    exit 1
fi
for name in postgres-password archive-access-key archive-secret-key; do
    path="$TEST_ROOT/secrets/$name"
    newline_count=$(tr -cd '\n' < "$path" | wc -c)
    value=$(<"$path")
    [[ "$newline_count" == 1 && "$value" == "${legacy_values[$name]}" ]] || {
        printf '%s did not normalize to its original semantic value with one LF\n' "$name" >&2
        exit 1
    }
    [[ "$(stat -c '%a:%u:%g' "$path")" == "${legacy_metadata[$name]}" ]] || {
        printf '%s mode or owner changed during normalization\n' "$name" >&2
        exit 1
    }
    [[ "$(stat -c '%i' "$path")" != "${legacy_inodes[$name]}" ]] || {
        printf '%s was not replaced atomically\n' "$name" >&2
        exit 1
    }
done

secret_content="$STATE_DIR/secret-clashlens-python-prototype-postgres-password-content"
secret_newline_count=$(tr -cd '\n' < "$secret_content" | wc -c)
secret_value=$(<"$secret_content")
[[ "$secret_newline_count" == 1 && "$secret_value" == "${legacy_values[postgres-password]}" ]] || {
    printf 'PostgreSQL Podman secret did not refresh from its normalized host file\n' >&2
    exit 1
}
for name in database-url archive-access-key archive-secret-key; do
    secret_content="$STATE_DIR/secret-clashlens-python-prototype-$name-content"
    secret_newline_count=$(tr -cd '\n' < "$secret_content" | wc -c)
    secret_value=$(<"$secret_content")
    [[ "$secret_newline_count" == 2 && "$secret_value" == "${application_values[$name]}" ]] || {
        printf 'init replaced application Podman secret %s behind its consumers\n' "$name" >&2
        exit 1
    }
done
[[ "$(<"$STATE_DIR/container-clashlens-python-prototype-worker-generation")" == "$worker_generation_before" ]] || {
    printf 'init recreated the worker instead of retaining its running consumer\n' >&2
    exit 1
}
[[ "$(<"$STATE_DIR/container-clashlens-python-prototype-api-generation")" == "$api_generation_before" ]] || {
    printf 'init recreated the API instead of retaining its running consumer\n' >&2
    exit 1
}
worker_archive_snapshot="$STATE_DIR/container-clashlens-python-prototype-worker-secret-clashlens-python-prototype-archive-access-key-content"
[[ "$(tr -cd '\n' < "$worker_archive_snapshot" | wc -c)" == 2 ]] || {
    printf 'init changed the running worker archive secret mount\n' >&2
    exit 1
}

unowned_secret="$STATE_DIR/secret-clashlens-python-prototype-archive-access-key-owned"
rm -f "$unowned_secret"
if "$ROOT_DIR/deploy.sh" init >/dev/null 2>&1; then
    printf 'deployment accepted an unowned application secret\n' >&2
    exit 1
fi
touch "$unowned_secret"

"$ROOT_DIR/deploy.sh" up >/dev/null
for name in database-url archive-access-key archive-secret-key; do
    secret_content="$STATE_DIR/secret-clashlens-python-prototype-$name-content"
    secret_newline_count=$(tr -cd '\n' < "$secret_content" | wc -c)
    secret_value=$(<"$secret_content")
    [[ "$secret_newline_count" == 1 && "$secret_value" == "${application_values[$name]}" ]] || {
        printf 'up did not refresh application Podman secret %s from its normalized host file\n' "$name" >&2
        exit 1
    }
    [[ ! -f "$STATE_DIR/secret-clashlens-python-prototype-$name-replaced-while-consumer-active" ]] || {
        printf 'up replaced application secret %s before removing every consumer\n' "$name" >&2
        exit 1
    }
    for consumer in api worker; do
        if [[ "$consumer" == api && "$name" != database-url ]]; then
            continue
        fi
        consumer_content="$STATE_DIR/container-clashlens-python-prototype-$consumer-secret-clashlens-python-prototype-$name-content"
        [[ "$(tr -cd '\n' < "$consumer_content" | wc -c)" == 1 && "$(<"$consumer_content")" == "${application_values[$name]}" ]] || {
            printf 'up recreated %s with stale application secret %s\n' "$consumer" "$name" >&2
            exit 1
        }
    done
done
[[ "$(<"$STATE_DIR/container-clashlens-python-prototype-worker-generation")" -gt "$worker_generation_before" ]] || {
    printf 'up did not recreate the worker after refreshing application secrets\n' >&2
    exit 1
}
[[ "$(<"$STATE_DIR/container-clashlens-python-prototype-api-generation")" -gt "$api_generation_before" ]] || {
    printf 'up did not recreate the API after refreshing application secrets\n' >&2
    exit 1
}
for name in database-url archive-access-key archive-secret-key; do
    replacement_line=$(grep -n "secret create --replace .*clashlens-python-prototype-$name " "$LOG_FILE" | tail -n 1 | cut -d: -f1)
    api_remove_line=$(grep -n 'rm --force clashlens-python-prototype-api' "$LOG_FILE" | tail -n 1 | cut -d: -f1)
    worker_remove_line=$(grep -n 'rm --force clashlens-python-prototype-worker' "$LOG_FILE" | tail -n 1 | cut -d: -f1)
    [[ -n "$replacement_line" && "$api_remove_line" -lt "$replacement_line" && "$worker_remove_line" -lt "$replacement_line" ]] || {
        printf 'up replaced application secret %s before removing every consumer\n' "$name" >&2
        exit 1
    }
done

postgres_run=$(grep 'docker.io/library/postgres:17-alpine' "$LOG_FILE")
if [[ "$postgres_run" != *'--user 70:70'* ]]; then
    printf 'PostgreSQL did not start with the pinned image runtime identity\n' >&2
    exit 1
fi
if ! grep -Fq -- 'clashlens-python-prototype-postgres-password\,type=mount\,target=/run/secrets/postgres-password\,uid=70\,gid=70\,mode=0400' "$LOG_FILE"; then
    printf 'PostgreSQL password secret was not mounted for the pinned image runtime identity\n' >&2
    exit 1
fi

# An empty Podman Config.User uses the image default, which is root for the stale container.
POSTGRES_STATE="$STATE_DIR/container-clashlens-python-prototype-postgres"
POSTGRES_VOLUME_STATE="$STATE_DIR/volume-clashlens-python-prototype-postgres-data"
[[ -f "$POSTGRES_VOLUME_STATE" ]] || {
    printf 'test setup did not create the named PostgreSQL volume\n' >&2
    exit 1
}
touch "$POSTGRES_STATE" "$POSTGRES_STATE-owned"
: > "$POSTGRES_STATE-user"
printf '%s\n' 'clashlens-python-prototype-postgres-data:/var/lib/postgresql/data' > "$POSTGRES_STATE-volume"
if ! "$ROOT_DIR/deploy.sh" init >/dev/null; then
    printf 'deployment failed while reconciling a stale PostgreSQL container\n' >&2
    exit 1
fi
if [[ ! -f "$POSTGRES_VOLUME_STATE" ]]; then
    printf 'stale PostgreSQL recreation removed the named volume\n' >&2
    exit 1
fi
postgres_user=$(<"$POSTGRES_STATE-user")
if [[ "$postgres_user" != '70:70' ]]; then
    printf 'stale PostgreSQL container was not recreated with Config.User 70:70\n' >&2
    exit 1
fi
if ! grep -q -- 'rm --force clashlens-python-prototype-postgres' "$LOG_FILE"; then
    printf 'stale PostgreSQL container was not removed before recreation\n' >&2
    exit 1
fi

database_url=$(<"$TEST_ROOT/secrets/database-url")
database_password=$(<"$TEST_ROOT/secrets/postgres-password")
expected_database_url="postgresql://clashlens_prototype:${database_password}@postgres:5432/clashlens_prototype?sslmode=disable"
if [[ "$database_url" != "$expected_database_url" ]]; then
    printf 'database URL did not use the generated PostgreSQL credential and database name\n' >&2
    exit 1
fi
if [[ ! -f "$DEPLOY_ENV_PATH" ]]; then
    printf 'deployment did not create the configuration parent directory\n' >&2
    exit 1
fi
INVALID_ENV_PATH="$TEST_ROOT/invalid.env"
cp "$DEPLOY_ENV_PATH" "$INVALID_ENV_PATH"
printf '%s\n' 'CLASHLENS_API_MAX_BODY_BYTES=999999999999999999999' >> "$INVALID_ENV_PATH"
if DEPLOY_ENV_FILE="$INVALID_ENV_PATH" "$ROOT_DIR/deploy.sh" init >/dev/null 2>&1; then
    printf 'deployment accepted an unbounded API body limit\n' >&2
    exit 1
fi
"$ROOT_DIR/deploy.sh" init >/dev/null
"$ROOT_DIR/deploy.sh" up >/dev/null

for name in database-url archive-access-key archive-secret-key; do
    path="$TEST_ROOT/secrets/$name"
    printf '%s\n\n' "${application_values[$name]}" > "$path"
    printf '%s\n\n' "${application_values[$name]}" > "$STATE_DIR/secret-clashlens-python-prototype-$name-content"
    for consumer in api worker; do
        if [[ "$consumer" == api && "$name" != database-url ]]; then
            continue
        fi
        printf '%s\n\n' "${application_values[$name]}" > \
            "$STATE_DIR/container-clashlens-python-prototype-$consumer-secret-clashlens-python-prototype-$name-content"
    done
done
worker_generation_before_verify=$(<"$STATE_DIR/container-clashlens-python-prototype-worker-generation")
api_generation_before_verify=$(<"$STATE_DIR/container-clashlens-python-prototype-api-generation")
if FAKE_PODMAN_FAIL_SEED=true "$ROOT_DIR/deploy.sh" verify >/dev/null 2>&1; then
    printf 'verify unexpectedly succeeded in its failure cleanup control\n' >&2
    exit 1
fi
[[ -f "$STATE_DIR/container-clashlens-python-prototype-worker" ]] || {
    printf 'verify failure did not restore the normal worker\n' >&2
    exit 1
}
[[ "$(<"$STATE_DIR/container-clashlens-python-prototype-worker-generation")" -gt "$worker_generation_before_verify" ]] || {
    printf 'verify failure did not recreate the normal worker\n' >&2
    exit 1
}
[[ "$(<"$STATE_DIR/container-clashlens-python-prototype-api-generation")" -gt "$api_generation_before_verify" ]] || {
    printf 'verify did not recreate the API after refreshing application secrets\n' >&2
    exit 1
}
last_worker_run=$(grep 'run .*clashlens-python-prototype-worker' "$LOG_FILE" | tail -n 1)
if [[ "$last_worker_run" == *'--archive-insecure-test-only'* ]]; then
    printf 'verify failure left the temporary insecure worker command running\n' >&2
    exit 1
fi
for name in database-url archive-access-key archive-secret-key; do
    secret_content="$STATE_DIR/secret-clashlens-python-prototype-$name-content"
    [[ "$(tr -cd '\n' < "$secret_content" | wc -c)" == 1 && "$(<"$secret_content")" == "${application_values[$name]}" ]] || {
        printf 'verify did not refresh application secret %s from its normalized host file\n' "$name" >&2
        exit 1
    }
    [[ ! -f "$STATE_DIR/secret-clashlens-python-prototype-$name-replaced-while-consumer-active" ]] || {
        printf 'verify replaced application secret %s before removing every consumer\n' "$name" >&2
        exit 1
    }
    for consumer in api worker; do
        if [[ "$consumer" == api && "$name" != database-url ]]; then
            continue
        fi
        consumer_content="$STATE_DIR/container-clashlens-python-prototype-$consumer-secret-clashlens-python-prototype-$name-content"
        [[ "$(tr -cd '\n' < "$consumer_content" | wc -c)" == 1 && "$(<"$consumer_content")" == "${application_values[$name]}" ]] || {
            printf 'verify recreated %s with stale application secret %s\n' "$consumer" "$name" >&2
            exit 1
        }
    done
done
"$ROOT_DIR/deploy.sh" verify >/dev/null

if [[ -f "$STATE_DIR/container-clashlens-python-prototype-archive-fixture" ]]; then
    printf 'temporary archive container was not removed\n' >&2
    exit 1
fi
if ! grep -q -- '--read-only' "$LOG_FILE"; then
    printf 'runtime containers were not read-only\n' >&2
    exit 1
fi
if ! grep -q -- 'secret rm clashlens-python-prototype-typescript-current' "$LOG_FILE" || \
   ! grep -q -- 'secret rm clashlens-python-prototype-typescript-previous' "$LOG_FILE"; then
    printf 'application HMAC secrets were not refreshed before API startup\n' >&2
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
