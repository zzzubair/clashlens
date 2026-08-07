#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MIGRATION_FILES=(
  "$ROOT_DIR/deploy/migrations/0001_collector.sql"
  "$ROOT_DIR/deploy/migrations/0002_python_layer.sql"
)
ENV_FILE=${DEPLOY_ENV_FILE:-"$ROOT_DIR/app.env"}
PODMAN_BIN=${PODMAN_BIN:-podman}
CURL_BIN=${CURL_BIN:-curl}

# These values are deployment metadata, not application credentials.
NETWORK_NAME=clashlens-private
POSTGRES_VOLUME=clashlens-postgres-data
POSTGRES_CONTAINER=clashlens-postgres
COLLECTOR_CONTAINER=clashlens-collector
PYTHON_WORKER_CONTAINER=clashlens-python-worker
POSTGRES_IMAGE=docker.io/library/postgres:17-alpine
COLLECTOR_IMAGE=localhost/clashlens-collector:deployment
PYTHON_WORKER_IMAGE=localhost/clashlens-python-worker:deployment
HEALTH_HOST=127.0.0.1
HEALTH_PORT=8081

# app.env is parsed without evaluating shell syntax. This prevents a typo in the
# file from running a command and keeps credentials out of diagnostic output.
declare -Ag ENV_PRESENT=()

usage() {
  cat >&2 <<'EOF'
Usage: ./deploy.sh <command> [arguments]

Commands:
  init                         Create private Podman resources and apply SQL.
  up                           Initialize, build, start, and verify the collector.
  restart                      Restart the collector without rebuilding its image.
  status                       Show safe container, network, and volume status.
  logs [collector|postgres]    Show logs for one private container.
  down                         Stop and remove containers; keep the data volume.
  enqueue [collector args]     Enqueue work, or pass a tag as the only argument.
  maintenance <collector args>
                               Run a collector maintenance command.
  python-up                    Build, start, and verify the production Python worker.
  python-down                  Remove only the production Python worker container.
  python-status                Show the production Python worker container status.
EOF
}

die() {
  printf 'deploy: error: %s\n' "$*" >&2
  exit 1
}

trim() {
  local value=$1
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

load_env_file() {
  [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE; copy app.env.example to app.env"
  [[ -r "$ENV_FILE" ]] || die "cannot read app.env"

  local mode
  mode=$(stat -c '%a' "$ENV_FILE")
  [[ "$mode" == "600" ]] || die "app.env must have mode 600 (got $mode)"

  local raw line name value first last
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    line=$(trim "$raw")
    [[ -z "$line" ]] && continue
    [[ "${line:0:1}" == "#" ]] && continue
    if [[ ! "$line" =~ ^(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      die "app.env contains an invalid setting"
    fi
    name=${BASH_REMATCH[2]}
    value=${BASH_REMATCH[3]}
    if [[ ${#value} -ge 2 ]]; then
      first=${value:0:1}
      last=${value: -1}
      if [[ ( "$first" == "\"" && "$last" == "\"" ) || ( "$first" == "'" && "$last" == "'" ) ]]; then
        value=${value:1:${#value}-2}
      fi
    fi
    export "$name=$value"
    ENV_PRESENT["$name"]=1
  done < "$ENV_FILE"
}

required_setting() {
  local name=$1
  [[ -n "${!name:-}" ]] || die "$name is required in app.env"
}

not_placeholder() {
  local name=$1
  local value=${!name:-}
  case "$value" in
    ""|CHANGE_ME|REPLACE_ME|replace-me|change-me)
      die "$name must contain a deployment value"
      ;;
  esac
}

validate_key_specs() {
  local name=$1
  local specs=$2
  local expected=$3
  local spec label path count=0
  local -a entries=()

  IFS=',' read -r -a entries <<< "$specs"
  for spec in "${entries[@]}"; do
    spec=$(trim "$spec")
    [[ -n "$spec" ]] || continue
    [[ "$spec" == *=* ]] || die "$name must use label=/run/secrets/name entries"
    label=$(trim "${spec%%=*}")
    path=$(trim "${spec#*=}")
    [[ -n "$label" && -n "$path" ]] || die "$name has an empty label or path"
    [[ "$label" != *[[:space:]]* ]] || die "$name has a label with spaces"
    [[ "$path" =~ ^/run/secrets/[^/]+$ ]] || die "$name paths must be direct files under /run/secrets"
    [[ "$path" != *..* ]] || die "$name contains an unsafe path"
    count=$((count + 1))
  done
  [[ "$count" == "$expected" ]] || die "$name must contain exactly $expected API key file entries"
}

validate_common_settings() {
  local required=(
    POSTGRES_DB
    POSTGRES_USER
    POSTGRES_PASSWORD
    CLASHLENS_DATABASE_URL
    CLASHLENS_ARCHIVE_ENDPOINT
    CLASHLENS_ARCHIVE_BUCKET
    CLASHLENS_ARCHIVE_ACCESS_KEY
    CLASHLENS_ARCHIVE_SECRET_KEY
    CLASHLENS_OFFICIAL_API_ORIGIN
    CLASHLENS_NORMAL_API_KEY_FILES
    CLASHLENS_INTERACTIVE_API_KEY_FILES
    CLASHLENS_API_KEY_HOST_DIR
  )
  local name
  for name in "${required[@]}"; do
    required_setting "$name"
  done

  for name in POSTGRES_PASSWORD CLASHLENS_DATABASE_URL CLASHLENS_ARCHIVE_ACCESS_KEY CLASHLENS_ARCHIVE_SECRET_KEY; do
    not_placeholder "$name"
  done

  [[ "$CLASHLENS_ARCHIVE_SECURE" == "true" ]] || die "CLASHLENS_ARCHIVE_SECURE must be true"
  [[ -z "${CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN:-}" ]] || die "CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN must not be set"
  [[ -z "${CLASHLENS_ALLOW_REDUCED_KEY_POOLS:-}" ]] || die "CLASHLENS_ALLOW_REDUCED_KEY_POOLS must not be set"
  [[ -z "${ENV_PRESENT[CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN]+present}" ]] || die "CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN must not be set"
  [[ -z "${ENV_PRESENT[CLASHLENS_ALLOW_REDUCED_KEY_POOLS]+present}" ]] || die "CLASHLENS_ALLOW_REDUCED_KEY_POOLS must not be set"

  local inline_key_setting
  for inline_key_setting in CLASHLENS_NORMAL_API_KEYS CLASHLENS_INTERACTIVE_API_KEYS; do
    [[ -z "${!inline_key_setting:-}" ]] || die "inline API keys must not be set; use API key files"
    [[ -z "${ENV_PRESENT[$inline_key_setting]+present}" ]] || die "inline API keys must not be set; use API key files"
  done

  [[ "$CLASHLENS_OFFICIAL_API_ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || \
    die "CLASHLENS_OFFICIAL_API_ORIGIN must be an HTTPS origin without a path"
  [[ "$CLASHLENS_ARCHIVE_ENDPOINT" != *"://"* ]] || die "CLASHLENS_ARCHIVE_ENDPOINT must be a host:port endpoint"
  [[ "$CLASHLENS_ARCHIVE_ENDPOINT" != */* ]] || die "CLASHLENS_ARCHIVE_ENDPOINT must not contain a path"
  [[ "$CLASHLENS_DATABASE_URL" == postgresql://* || "$CLASHLENS_DATABASE_URL" == postgres://* ]] || \
    die "CLASHLENS_DATABASE_URL must be a PostgreSQL URL"
  [[ "$CLASHLENS_DATABASE_URL" == *"@postgres:"* || "$CLASHLENS_DATABASE_URL" == *"@postgres/"* ]] || \
    die "CLASHLENS_DATABASE_URL must use the private postgres service"

  [[ "$CLASHLENS_API_KEY_HOST_DIR" == /* ]] || die "CLASHLENS_API_KEY_HOST_DIR must be an absolute directory"
  [[ "$CLASHLENS_HEALTH_HOST" == "127.0.0.1" ]] || die "CLASHLENS_HEALTH_HOST must be 127.0.0.1"
  [[ "$CLASHLENS_HEALTH_PORT" =~ ^[0-9]+$ ]] || die "CLASHLENS_HEALTH_PORT must be a number"
  (( CLASHLENS_HEALTH_PORT >= 1 && CLASHLENS_HEALTH_PORT <= 65535 )) || die "CLASHLENS_HEALTH_PORT is outside the valid range"

  validate_key_specs CLASHLENS_NORMAL_API_KEY_FILES "$CLASHLENS_NORMAL_API_KEY_FILES" 4
  validate_key_specs CLASHLENS_INTERACTIVE_API_KEY_FILES "$CLASHLENS_INTERACTIVE_API_KEY_FILES" 1
}

validate_key_files() {
  [[ -d "$CLASHLENS_API_KEY_HOST_DIR" ]] || die "CLASHLENS_API_KEY_HOST_DIR does not exist"

  local specs=$1
  local spec label path source
  local -a entries=()
  local -A targets=()
  IFS=',' read -r -a entries <<< "$specs"
  for spec in "${entries[@]}"; do
    spec=$(trim "$spec")
    [[ -n "$spec" ]] || continue
    label=$(trim "${spec%%=*}")
    path=$(trim "${spec#*=}")
    [[ -z "${targets[$path]+present}" ]] || die "API key file paths must be unique"
    targets["$path"]=1
    source="$CLASHLENS_API_KEY_HOST_DIR/${path##*/}"
    [[ -f "$source" ]] || die "API key file for label $label is missing"
    [[ -r "$source" ]] || die "API key file for label $label is not readable"
    [[ "$(stat -c '%a' "$source")" == "600" ]] || die "API key file for label $label must have mode 600"
  done
}

require_podman() {
  command -v "$PODMAN_BIN" >/dev/null 2>&1 || die "Podman is required"
}

require_rootless_podman() {
  local rootless
  rootless=$("$PODMAN_BIN" info --format '{{.Host.Security.Rootless}}' 2>/dev/null || true)
  [[ "$rootless" == "true" ]] || die "Podman must run in rootless mode"
}

container_exists() {
  "$PODMAN_BIN" container exists "$1" >/dev/null 2>&1
}

container_running() {
  local state
  state=$("$PODMAN_BIN" container inspect --format '{{.State.Running}}' "$1" 2>/dev/null || true)
  [[ "$state" == "true" ]]
}

replace_secret_value() {
  local name=$1
  local value=$2
  printf '%s' "$value" | "$PODMAN_BIN" secret create --replace "$name" - >/dev/null
}

ensure_network() {
  if ! "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1; then
    # The named network is private to this rootless Podman project. It must
    # retain outbound access for the official API and external archive.
    "$PODMAN_BIN" network create --label org.clashlens.scope=private "$NETWORK_NAME" >/dev/null
  fi
}

ensure_volume() {
  if ! "$PODMAN_BIN" volume exists "$POSTGRES_VOLUME" >/dev/null 2>&1; then
    "$PODMAN_BIN" volume create --label org.clashlens.scope=private "$POSTGRES_VOLUME" >/dev/null
  fi
}

ensure_postgres() {
  replace_secret_value clashlens-postgres-password "$POSTGRES_PASSWORD"
  if container_exists "$POSTGRES_CONTAINER"; then
    if ! container_running "$POSTGRES_CONTAINER"; then
      "$PODMAN_BIN" start "$POSTGRES_CONTAINER" >/dev/null
    fi
    return
  fi

  "$PODMAN_BIN" run \
    --detach \
    --name "$POSTGRES_CONTAINER" \
    --network "$NETWORK_NAME" \
    --network-alias postgres \
    --volume "$POSTGRES_VOLUME:/var/lib/postgresql/data" \
    --env POSTGRES_DB \
    --env POSTGRES_USER \
    --env POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password \
    --secret clashlens-postgres-password,type=mount,target=/run/secrets/postgres-password,uid=70,gid=70,mode=0400 \
    --health-cmd "pg_isready -U $POSTGRES_USER -d $POSTGRES_DB" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 6 \
    --restart unless-stopped \
    --label org.clashlens.component=postgres \
    "$POSTGRES_IMAGE" >/dev/null
}

wait_for_postgres() {
  local attempt
  for ((attempt = 1; attempt <= 60; attempt++)); do
    if "$PODMAN_BIN" exec "$POSTGRES_CONTAINER" pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  die "PostgreSQL did not become ready"
}

apply_migration_file() {
  local migration_file=$1
  [[ -f "$migration_file" ]] || die "missing deployment migration"
  "$PODMAN_BIN" exec --interactive "$POSTGRES_CONTAINER" \
    psql --quiet --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    <"$migration_file"
}

apply_initial_contract() {
  apply_migration_file "${MIGRATION_FILES[0]}"
}

advance_contract() {
  local migration_file
  for migration_file in "${MIGRATION_FILES[@]:1}"; do
    apply_migration_file "$migration_file"
  done
}

build_collector_image() {
  "$PODMAN_BIN" build \
    --pull=missing \
    --file "$ROOT_DIR/Containerfile" \
    --tag "$COLLECTOR_IMAGE" \
    "$ROOT_DIR"
}

build_python_worker_image() {
  "$PODMAN_BIN" build \
    --pull=missing \
    --file "$ROOT_DIR/python/Containerfile" \
    --tag "$PYTHON_WORKER_IMAGE" \
    "$ROOT_DIR/python"
}

require_python_worker_runtime() {
  "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1 || \
    die "private network is missing; run up first"
  container_running "$POSTGRES_CONTAINER" || die "PostgreSQL is not running; run up first"

  local version
  version=$("$PODMAN_BIN" exec "$POSTGRES_CONTAINER" \
    psql --tuples-only --no-align --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --command 'SELECT version FROM clash_lens_contract' 2>/dev/null || true)
  version=$(trim "$version")
  [[ "$version" == "2" ]] || die "production contract version 2 is required"
}

start_python_worker() {
  if container_exists "$PYTHON_WORKER_CONTAINER"; then
    "$PODMAN_BIN" rm --force "$PYTHON_WORKER_CONTAINER" >/dev/null
  fi

  replace_secret_value clashlens-python-worker-database-url "$CLASHLENS_DATABASE_URL"
  replace_secret_value clashlens-python-worker-archive-access-key "$CLASHLENS_ARCHIVE_ACCESS_KEY"
  replace_secret_value clashlens-python-worker-archive-secret-key "$CLASHLENS_ARCHIVE_SECRET_KEY"

  "$PODMAN_BIN" run \
    --detach \
    --name "$PYTHON_WORKER_CONTAINER" \
    --network "$NETWORK_NAME" \
    --env "CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url" \
    --env CLASHLENS_ARCHIVE_ENDPOINT \
    --env CLASHLENS_ARCHIVE_BUCKET \
    --env "CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key" \
    --env "CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key" \
    --secret clashlens-python-worker-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400 \
    --secret clashlens-python-worker-archive-access-key,type=mount,target=/run/secrets/archive-access-key,uid=10001,gid=10001,mode=0400 \
    --secret clashlens-python-worker-archive-secret-key,type=mount,target=/run/secrets/archive-secret-key,uid=10001,gid=10001,mode=0400 \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --cap-drop all \
    --security-opt no-new-privileges \
    --memory 384m \
    --pids-limit 256 \
    --cpus 1.0 \
    --health-cmd "python -m clashlens.cli ready --expected-contract-version 2" \
    --health-interval 30s \
    --health-timeout 20s \
    --health-retries 3 \
    --restart unless-stopped \
    --label org.clashlens.component=python-worker \
    "$PYTHON_WORKER_IMAGE" \
    worker --owner production-python-1 --max-jobs 100 --lease-seconds 60 --run-forever >/dev/null
}

wait_for_python_worker() {
  local attempt
  for ((attempt = 1; attempt <= 60; attempt++)); do
    if container_running "$PYTHON_WORKER_CONTAINER" && \
      "$PODMAN_BIN" healthcheck run "$PYTHON_WORKER_CONTAINER" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  die "production Python worker did not become healthy"
}

collector_secret_args() {
  local -n result=$1
  local specs=$2
  local spec path source secret_name
  local -a entries=()
  IFS=',' read -r -a entries <<< "$specs"
  for spec in "${entries[@]}"; do
    spec=$(trim "$spec")
    [[ -n "$spec" ]] || continue
    path=$(trim "${spec#*=}")
    source="$CLASHLENS_API_KEY_HOST_DIR/${path##*/}"
    secret_name="clashlens-${path##*/}"
    "$PODMAN_BIN" secret create --replace "$secret_name" "$source" >/dev/null
    result+=(--secret "$secret_name,type=mount,target=$path,uid=10001,gid=10001,mode=0400")
  done
}

collector_credential_secret_args() {
  local -n secret_result=$1
  local -n env_result=$2
  local index secret_name setting value
  local -a secret_names=(database-url archive-access-key archive-secret-key)
  local -a settings=(
    CLASHLENS_DATABASE_URL
    CLASHLENS_ARCHIVE_ACCESS_KEY
    CLASHLENS_ARCHIVE_SECRET_KEY
  )

  for index in "${!secret_names[@]}"; do
    secret_name=${secret_names[$index]}
    setting=${settings[$index]}
    value=${!setting}
    replace_secret_value "clashlens-$secret_name" "$value"
    secret_result+=(--secret "clashlens-$secret_name,type=mount,target=/run/secrets/$secret_name,uid=10001,gid=10001,mode=0400")
    env_result+=(--env "${setting}_FILE=/run/secrets/$secret_name")
  done
}

collector_env_args() {
  local -n result=$1
  local name

  for name in "${!ENV_PRESENT[@]}"; do
    case "$name" in
      CLASHLENS_DATABASE_URL|CLASHLENS_ARCHIVE_ACCESS_KEY|CLASHLENS_ARCHIVE_SECRET_KEY|CLASHLENS_NORMAL_API_KEYS|CLASHLENS_INTERACTIVE_API_KEYS|CLASHLENS_API_KEY_HOST_DIR|CLASHLENS_HEALTH_HOST|CLASHLENS_HEALTH_PORT|CLASHLENS_PODMAN_*|CLASHLENS_POSTGRES_CONTAINER|CLASHLENS_POSTGRES_IMAGE|CLASHLENS_COLLECTOR_CONTAINER|CLASHLENS_COLLECTOR_IMAGE)
        ;;
      CLASHLENS_*)
        result+=(--env "$name")
        ;;
    esac
  done

  result+=(--env "CLASHLENS_HEALTH_LISTEN=0.0.0.0:8081")
}

start_collector() {
  local -a secrets=()
  local -a env_args=()
  collector_secret_args secrets "$CLASHLENS_NORMAL_API_KEY_FILES"
  collector_secret_args secrets "$CLASHLENS_INTERACTIVE_API_KEY_FILES"
  collector_env_args env_args
  collector_credential_secret_args secrets env_args

  if container_exists "$COLLECTOR_CONTAINER"; then
    "$PODMAN_BIN" rm --force "$COLLECTOR_CONTAINER" >/dev/null
  fi

  "$PODMAN_BIN" run \
    --detach \
    --name "$COLLECTOR_CONTAINER" \
    --network "$NETWORK_NAME" \
    "${env_args[@]}" \
    --publish "$CLASHLENS_HEALTH_HOST:$CLASHLENS_HEALTH_PORT:8081/tcp" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --cap-drop all \
    --security-opt no-new-privileges \
    --restart unless-stopped \
    --label org.clashlens.component=collector \
    "${secrets[@]}" \
    "$COLLECTOR_IMAGE" run --role both >/dev/null
}

wait_for_collector() {
  local response attempt
  for ((attempt = 1; attempt <= 60; attempt++)); do
    response=$("$CURL_BIN" --fail --silent --max-time 3 "http://$CLASHLENS_HEALTH_HOST:$CLASHLENS_HEALTH_PORT/readyz" 2>/dev/null || true)
    if [[ "$response" =~ \"ready\"[[:space:]]*:[[:space:]]*true ]]; then
      return
    fi
    sleep 1
  done
  die "collector did not report ready on the localhost health endpoint"
}

initialize_runtime() {
  require_podman
  require_rootless_podman
  ensure_network
  ensure_volume
  ensure_postgres
  wait_for_postgres
  apply_initial_contract
}

image_exists() {
  "$PODMAN_BIN" image exists "$COLLECTOR_IMAGE" >/dev/null 2>&1
}

status_of_container() {
  local label=$1
  local name=$2
  if ! container_exists "$name"; then
    printf '%s: absent\n' "$label"
  elif container_running "$name"; then
    printf '%s: running\n' "$label"
  else
    printf '%s: stopped\n' "$label"
  fi
}

show_status() {
  require_podman
  if "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1; then
    printf 'network: present (private)\n'
  else
    printf 'network: absent\n'
  fi
  if "$PODMAN_BIN" volume exists "$POSTGRES_VOLUME" >/dev/null 2>&1; then
    printf 'volume: present\n'
  else
    printf 'volume: absent\n'
  fi
  status_of_container postgres "$POSTGRES_CONTAINER"
  status_of_container collector "$COLLECTOR_CONTAINER"
  status_of_container python-worker "$PYTHON_WORKER_CONTAINER"
}

stop_and_remove() {
  local name=$1
  if container_exists "$name"; then
    "$PODMAN_BIN" rm --force "$name" >/dev/null
  fi
}

run_collector_command() {
  container_exists "$COLLECTOR_CONTAINER" || die "collector container does not exist; run up first"
  container_running "$COLLECTOR_CONTAINER" || die "collector container is not running"
  "$PODMAN_BIN" exec "$COLLECTOR_CONTAINER" /usr/local/bin/collector "$@"
}

command=${1:-}
if [[ -z "$command" ]]; then
  usage
  exit 2
fi
shift

full_configuration=false
case "$command" in
  init|up|restart|python-up)
    load_env_file
    full_configuration=true
    ;;
  status|logs|down|enqueue|maintenance|python-down|python-status)
    if [[ -f "$ENV_FILE" ]]; then
      load_env_file
    fi
    ;;
  help|-h|--help)
    ;;
esac

NETWORK_NAME=${CLASHLENS_PODMAN_NETWORK:-$NETWORK_NAME}
POSTGRES_VOLUME=${CLASHLENS_PODMAN_VOLUME:-$POSTGRES_VOLUME}
POSTGRES_CONTAINER=${CLASHLENS_POSTGRES_CONTAINER:-$POSTGRES_CONTAINER}
COLLECTOR_CONTAINER=${CLASHLENS_COLLECTOR_CONTAINER:-$COLLECTOR_CONTAINER}
PYTHON_WORKER_CONTAINER=${CLASHLENS_PYTHON_WORKER_CONTAINER:-$PYTHON_WORKER_CONTAINER}
POSTGRES_IMAGE=${CLASHLENS_POSTGRES_IMAGE:-$POSTGRES_IMAGE}
COLLECTOR_IMAGE=${CLASHLENS_COLLECTOR_IMAGE:-$COLLECTOR_IMAGE}
PYTHON_WORKER_IMAGE=${CLASHLENS_PYTHON_WORKER_IMAGE:-$PYTHON_WORKER_IMAGE}
CLASHLENS_ARCHIVE_SECURE=${CLASHLENS_ARCHIVE_SECURE:-true}
CLASHLENS_HEALTH_HOST=${CLASHLENS_HEALTH_HOST:-$HEALTH_HOST}
CLASHLENS_HEALTH_PORT=${CLASHLENS_HEALTH_PORT:-$HEALTH_PORT}
HEALTH_HOST=$CLASHLENS_HEALTH_HOST
HEALTH_PORT=$CLASHLENS_HEALTH_PORT

# Keep the names above in sync with the values passed to the collector.
if [[ "$full_configuration" == "true" ]]; then
  validate_common_settings
fi

case "$command" in
  init)
    [[ $# == 0 ]] || die "init accepts no arguments"
    initialize_runtime
    printf 'database initialized; data volume is %s\n' "$POSTGRES_VOLUME"
    ;;
  up)
    [[ $# == 0 ]] || die "up accepts no arguments"
    validate_key_files "$CLASHLENS_NORMAL_API_KEY_FILES"
    validate_key_files "$CLASHLENS_INTERACTIVE_API_KEY_FILES"
    build_collector_image
    initialize_runtime
    start_collector
    wait_for_collector
    advance_contract
    wait_for_collector
    printf 'collector is ready at http://%s:%s/readyz\n' "$HEALTH_HOST" "$HEALTH_PORT"
    ;;
  restart)
    [[ $# == 0 ]] || die "restart accepts no arguments"
    validate_key_files "$CLASHLENS_NORMAL_API_KEY_FILES"
    validate_key_files "$CLASHLENS_INTERACTIVE_API_KEY_FILES"
    initialize_runtime
    image_exists || die "collector image is missing; run up first"
    start_collector
    wait_for_collector
    advance_contract
    wait_for_collector
    printf 'collector restarted and is ready at http://%s:%s/readyz\n' "$HEALTH_HOST" "$HEALTH_PORT"
    ;;
  status)
    [[ $# == 0 ]] || die "status accepts no arguments"
    show_status
    ;;
  logs)
    component=${1:-collector}
    if [[ "$component" == "collector" ]]; then
      target=$COLLECTOR_CONTAINER
    elif [[ "$component" == "postgres" ]]; then
      target=$POSTGRES_CONTAINER
    elif [[ "$component" == "python-worker" ]]; then
      target=$PYTHON_WORKER_CONTAINER
    else
      die "logs target must be collector, postgres, or python-worker"
    fi
    shift || true
    container_exists "$target" || die "$component container does not exist"
    "$PODMAN_BIN" logs "$@" "$target"
    ;;
  down)
    [[ $# == 0 ]] || die "down accepts no arguments"
    require_podman
    stop_and_remove "$PYTHON_WORKER_CONTAINER"
    stop_and_remove "$COLLECTOR_CONTAINER"
    stop_and_remove "$POSTGRES_CONTAINER"
    printf 'containers removed; network and data volume were kept\n'
    ;;
  enqueue)
    if [[ $# == 1 && "$1" != -* ]]; then
      run_collector_command enqueue --type live_refresh --tag "$1"
    else
      run_collector_command enqueue "$@"
    fi
    ;;
  maintenance)
    [[ $# -gt 0 ]] || die "maintenance requires a collector maintenance command"
    run_collector_command maintenance "$@"
    ;;
  python-up)
    [[ $# == 0 ]] || die "python-up accepts no arguments"
    require_podman
    require_rootless_podman
    require_python_worker_runtime
    build_python_worker_image
    start_python_worker
    wait_for_python_worker
    printf 'production Python worker is healthy\n'
    ;;
  python-down)
    [[ $# == 0 ]] || die "python-down accepts no arguments"
    require_podman
    stop_and_remove "$PYTHON_WORKER_CONTAINER"
    printf 'production Python worker container removed\n'
    ;;
  python-status)
    [[ $# == 0 ]] || die "python-status accepts no arguments"
    require_podman
    status_of_container python-worker "$PYTHON_WORKER_CONTAINER"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
