#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE=${DEPLOY_ENV_FILE:-"$ROOT_DIR/prototype.env"}
PODMAN_BIN=${PODMAN_BIN:-podman}
CURL_BIN=${CURL_BIN:-curl}

PREFIX=clashlens-python-prototype
NETWORK_NAME=${PREFIX}-network
VOLUME_NAME=${PREFIX}-postgres-data
POSTGRES_CONTAINER=${PREFIX}-postgres
API_CONTAINER=${PREFIX}-api
WORKER_CONTAINER=${PREFIX}-worker
ARCHIVE_CONTAINER=${PREFIX}-archive-fixture
IMAGE_NAME=localhost/${PREFIX}:prototype
MAX_API_BODY_BYTES=16777216
MAX_ARCHIVE_BODY_BYTES=67108864
MAX_ARCHIVE_CONNECT_TIMEOUT_SECONDS=60
MAX_ARCHIVE_READ_TIMEOUT_SECONDS=300
MAX_ARCHIVE_RETRIES=5
MAX_ARCHIVE_RETRY_BACKOFF_SECONDS=30

CONFIG_LOADED=false
SECRET_DIR=""
POSTGRES_DB=""
POSTGRES_USER=""
API_PORT=""
API_MAX_BODY_BYTES=""
ARCHIVE_ENDPOINT=""
ARCHIVE_BUCKET=""
ARCHIVE_SECURE=""
ARCHIVE_MAX_BODY_BYTES=""
ARCHIVE_CONNECT_TIMEOUT_SECONDS=""
ARCHIVE_READ_TIMEOUT_SECONDS=""
ARCHIVE_MAX_RETRIES=""
ARCHIVE_RETRY_BACKOFF_SECONDS=""
HMAC_CALLER=""
HMAC_KEY_ID=""
HMAC_PREVIOUS_KEY_ID=""
FIXTURE_FILE="$ROOT_DIR/testdata/legend_i_profile_v1.json"
VERIFY_INSECURE_WORKER=false

say() {
    printf '%s\n' "$*"
}

die() {
    printf 'prototype deployment failed: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: ./deploy.sh <command>

Commands:
  init    Create file-backed prototype configuration, secrets, network, volume, and database schema.
  build   Build the isolated prototype image.
  up      Build and start the API and worker containers.
  down    Stop and remove prototype containers but keep the database volume and secret files.
  status  Show only the isolated prototype resources.
  logs    Show logs for api, worker, or postgres.
  verify  Seed a synthetic profile, process it through a temporary fake archive, and verify saved-data output.
EOF
}

ensure_config_file() {
    local config_dir
    config_dir=$(dirname -- "$ENV_FILE")
    if [[ ! -d "$config_dir" ]]; then
        install -d -m 700 "$config_dir"
    fi
    if [[ ! -f "$ENV_FILE" ]]; then
        install -m 600 "$ROOT_DIR/prototype.env.example" "$ENV_FILE"
        say "created file-backed configuration at $ENV_FILE" >&2
    fi
}

load_config() {
    if [[ "$CONFIG_LOADED" == true ]]; then
        return
    fi
    ensure_config_file
    [[ -r "$ENV_FILE" ]] || die "configuration file is not readable"
    if [[ "$(stat -c '%a' "$ENV_FILE")" != "600" ]]; then
        chmod 600 "$ENV_FILE"
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
            export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
        else
            die "invalid configuration line"
        fi
    done < "$ENV_FILE"
    SECRET_DIR=${CLASHLENS_SECRET_DIR:-"$ROOT_DIR/.prototype-runtime/secrets"}
    POSTGRES_DB=${POSTGRES_DB:-clashlens_prototype}
    POSTGRES_USER=${POSTGRES_USER:-clashlens_prototype}
    API_PORT=${CLASHLENS_API_PORT:-18080}
    API_MAX_BODY_BYTES=${CLASHLENS_API_MAX_BODY_BYTES:-1048576}
    ARCHIVE_ENDPOINT=${CLASHLENS_ARCHIVE_ENDPOINT:-archive-fixture:9000}
    ARCHIVE_BUCKET=${CLASHLENS_ARCHIVE_BUCKET:-evidence}
    ARCHIVE_SECURE=${CLASHLENS_ARCHIVE_SECURE:-true}
    ARCHIVE_MAX_BODY_BYTES=${CLASHLENS_ARCHIVE_MAX_BODY_BYTES:-2000000}
    ARCHIVE_CONNECT_TIMEOUT_SECONDS=${CLASHLENS_ARCHIVE_CONNECT_TIMEOUT_SECONDS:-5}
    ARCHIVE_READ_TIMEOUT_SECONDS=${CLASHLENS_ARCHIVE_READ_TIMEOUT_SECONDS:-15}
    ARCHIVE_MAX_RETRIES=${CLASHLENS_ARCHIVE_MAX_RETRIES:-1}
    ARCHIVE_RETRY_BACKOFF_SECONDS=${CLASHLENS_ARCHIVE_RETRY_BACKOFF_SECONDS:-0.1}
    HMAC_CALLER=${CLASHLENS_HMAC_CALLER:-typescript-website}
    HMAC_KEY_ID=${CLASHLENS_HMAC_KEY_ID:-current}
    HMAC_PREVIOUS_KEY_ID=${CLASHLENS_HMAC_PREVIOUS_KEY_ID:-previous}
    CONFIG_LOADED=true
}

decimal_positive_at_most() {
    awk -v value="$1" -v maximum="$2" \
        'BEGIN { exit !(value > 0 && value <= maximum) }'
}

decimal_nonnegative_at_most() {
    awk -v value="$1" -v maximum="$2" \
        'BEGIN { exit !(value >= 0 && value <= maximum) }'
}

validate_config() {
    [[ "$SECRET_DIR" = /* ]] || die "CLASHLENS_SECRET_DIR must be an absolute path"
    [[ "$ARCHIVE_ENDPOINT" =~ ^[^/:]+:[1-9][0-9]{1,5}$ ]] || die "archive endpoint must be a host:port value"
    local archive_port=${ARCHIVE_ENDPOINT##*:}
    [[ "$archive_port" -le 65535 ]] || die "archive endpoint port is out of range"
    [[ "$ARCHIVE_SECURE" == true ]] || die "production-shaped startup requires TLS archive access"
    [[ "$HMAC_CALLER" == typescript-website ]] || die "prototype deployment supports only the fixed website caller"
    [[ "$HMAC_KEY_ID" == current && "$HMAC_PREVIOUS_KEY_ID" == previous ]] || die "prototype HMAC key IDs must be current and previous"
    [[ "$POSTGRES_DB" =~ ^[a-z_][a-z0-9_]*$ ]] || die "invalid PostgreSQL database name"
    [[ "$POSTGRES_USER" =~ ^[a-z_][a-z0-9_]*$ ]] || die "invalid PostgreSQL user name"
    [[ "$API_PORT" =~ ^[1-9][0-9]{2,4}$ && "$API_PORT" -le 65535 ]] || die "invalid API port"
    [[ "$API_MAX_BODY_BYTES" =~ ^[1-9][0-9]{0,7}$ && "$API_MAX_BODY_BYTES" -le "$MAX_API_BODY_BYTES" ]] || die "invalid API body limit"
    [[ "$ARCHIVE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || die "invalid archive bucket"
    [[ "$ARCHIVE_MAX_BODY_BYTES" =~ ^[1-9][0-9]{0,7}$ && "$ARCHIVE_MAX_BODY_BYTES" -le "$MAX_ARCHIVE_BODY_BYTES" ]] || die "invalid archive body limit"
    [[ "$ARCHIVE_MAX_RETRIES" =~ ^[0-5]$ ]] || die "invalid archive retry limit"
    [[ "$ARCHIVE_CONNECT_TIMEOUT_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]] && \
        decimal_positive_at_most "$ARCHIVE_CONNECT_TIMEOUT_SECONDS" "$MAX_ARCHIVE_CONNECT_TIMEOUT_SECONDS" || \
        die "invalid archive connect timeout"
    [[ "$ARCHIVE_READ_TIMEOUT_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]] && \
        decimal_positive_at_most "$ARCHIVE_READ_TIMEOUT_SECONDS" "$MAX_ARCHIVE_READ_TIMEOUT_SECONDS" || \
        die "invalid archive read timeout"
    [[ "$ARCHIVE_RETRY_BACKOFF_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]] && \
        decimal_nonnegative_at_most "$ARCHIVE_RETRY_BACKOFF_SECONDS" "$MAX_ARCHIVE_RETRY_BACKOFF_SECONDS" || \
        die "invalid archive retry backoff"
    if [[ -n "${CLASHLENS_DATABASE_URL:-}" || -n "${CLASHLENS_ARCHIVE_ACCESS_KEY:-}" || -n "${CLASHLENS_ARCHIVE_SECRET_KEY:-}" ]]; then
        die "credentials must be stored in files, not configuration values"
    fi
    [[ -f "$FIXTURE_FILE" ]] || die "synthetic archive fixture is missing"
}

require_podman() {
    command -v "$PODMAN_BIN" >/dev/null 2>&1 || die "podman is required"
    "$PODMAN_BIN" info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -qx true \
        || die "podman must run in rootless mode"
}

ensure_host_file() {
    local path=$1
    local kind=$2
    mkdir -p "$(dirname -- "$path")"
    chmod 700 "$(dirname -- "$path")"
    if [[ ! -f "$path" ]]; then
        local temporary="${path}.tmp.$$"
        if [[ "$kind" == hmac ]]; then
            (umask 077; openssl rand 32 | base64 -w0 | tr '+/' '-_' | tr -d '='; printf '\n') > "$temporary"
        else
            (umask 077; openssl rand -hex 24; printf '\n') > "$temporary"
        fi
        chmod 600 "$temporary"
        mv -f "$temporary" "$path"
    fi
    chmod 600 "$path"
}

ensure_host_secrets() {
    command -v openssl >/dev/null 2>&1 || die "openssl is required to generate local prototype keys"
    mkdir -p "$SECRET_DIR"
    chmod 700 "$SECRET_DIR"
    ensure_host_file "$SECRET_DIR/postgres-password" password
    ensure_host_file "$SECRET_DIR/archive-access-key" password
    ensure_host_file "$SECRET_DIR/archive-secret-key" password
    ensure_host_file "$SECRET_DIR/typescript-current" hmac
    ensure_host_file "$SECRET_DIR/typescript-previous" hmac
    local db_password
    db_password=$(<"$SECRET_DIR/postgres-password")
    [[ "$db_password" =~ ^[A-Za-z0-9]+$ ]] || die "postgres password file must contain generated alphanumeric text"
    local database_url="postgresql://${POSTGRES_USER}:${db_password}@postgres:5432/${POSTGRES_DB}?sslmode=disable"
    (umask 077; printf '%s\n' "$database_url" > "$SECRET_DIR/database-url")
    chmod 600 "$SECRET_DIR/database-url"
}

secret_name() {
    case "$1" in
        postgres-password) printf '%s-postgres-password' "$PREFIX" ;;
        database-url) printf '%s-database-url' "$PREFIX" ;;
        archive-access-key) printf '%s-archive-access-key' "$PREFIX" ;;
        archive-secret-key) printf '%s-archive-secret-key' "$PREFIX" ;;
        typescript-current) printf '%s-typescript-current' "$PREFIX" ;;
        typescript-previous) printf '%s-typescript-previous' "$PREFIX" ;;
        *) die "unknown secret name" ;;
    esac
}

ensure_podman_secret() {
    local file_name=$1
    local path="$SECRET_DIR/$file_name"
    local name
    name=$(secret_name "$file_name")
    if "$PODMAN_BIN" secret inspect "$name" >/dev/null 2>&1; then
        secret_is_owned "$name" || die "existing Podman secret is not owned by this prototype"
        return
    fi
    "$PODMAN_BIN" secret create --label io.clashlens.prototype=true "$name" "$path" >/dev/null
}

refresh_hmac_secrets() {
    local file_name name
    remove_container "$API_CONTAINER"
    for file_name in typescript-current typescript-previous; do
        name=$(secret_name "$file_name")
        if "$PODMAN_BIN" secret inspect "$name" >/dev/null 2>&1; then
            secret_is_owned "$name" || die "existing Podman secret is not owned by this prototype"
            "$PODMAN_BIN" secret rm "$name" >/dev/null
        fi
        "$PODMAN_BIN" secret create --label io.clashlens.prototype=true "$name" "$SECRET_DIR/$file_name" >/dev/null
    done
}

secret_is_owned() {
    local name=$1
    "$PODMAN_BIN" secret inspect \
        --format '{{ index .Spec.Labels "io.clashlens.prototype" }}' "$name" 2>/dev/null \
        | grep -qx true
}

resource_is_owned() {
    local kind=$1
    local name=$2
    "$PODMAN_BIN" "$kind" inspect \
        --format '{{ index .Labels "io.clashlens.prototype" }}' "$name" 2>/dev/null \
        | grep -qx true
}

container_exists() {
    "$PODMAN_BIN" container exists "$1" >/dev/null 2>&1
}

remove_container() {
    local name=$1
    if container_exists "$name"; then
        container_is_owned "$name" || die "existing Podman container is not owned by this prototype"
        "$PODMAN_BIN" rm --force "$name" >/dev/null
    fi
}

container_is_owned() {
    local name=$1
    "$PODMAN_BIN" inspect \
        --format '{{ index .Config.Labels "io.clashlens.prototype" }}' "$name" 2>/dev/null \
        | grep -qx true
}

ensure_network_and_volume() {
    if ! "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1; then
        "$PODMAN_BIN" network create --label "io.clashlens.prototype=true" "$NETWORK_NAME" >/dev/null
    elif ! resource_is_owned network "$NETWORK_NAME"; then
        die "existing Podman network is not owned by this prototype"
    fi
    if ! "$PODMAN_BIN" volume exists "$VOLUME_NAME" >/dev/null 2>&1; then
        "$PODMAN_BIN" volume create --label "io.clashlens.prototype=true" "$VOLUME_NAME" >/dev/null
    elif ! resource_is_owned volume "$VOLUME_NAME"; then
        die "existing Podman volume is not owned by this prototype"
    fi
}

start_postgres() {
    remove_container "$POSTGRES_CONTAINER"
    "$PODMAN_BIN" run --detach \
        --name "$POSTGRES_CONTAINER" \
        --label io.clashlens.prototype=true \
        --network "$NETWORK_NAME" \
        --network-alias postgres \
        --volume "$VOLUME_NAME:/var/lib/postgresql/data" \
        --secret "$(secret_name postgres-password),type=mount,target=/run/secrets/postgres-password,uid=70,gid=70,mode=0400" \
        --env POSTGRES_DB="$POSTGRES_DB" \
        --env POSTGRES_USER="$POSTGRES_USER" \
        --env POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password \
        --memory=512m \
        --pids-limit=256 \
        --cap-drop=ALL \
        --security-opt=no-new-privileges \
        --stop-timeout 30 \
        docker.io/library/postgres:17-alpine >/dev/null
}

wait_for_postgres() {
    local attempt
    for attempt in $(seq 1 60); do
        if "$PODMAN_BIN" exec "$POSTGRES_CONTAINER" pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" >/dev/null 2>&1; then
            return
        fi
        sleep 1
    done
    die "prototype PostgreSQL did not become ready"
}

apply_schema() {
    "$PODMAN_BIN" exec --interactive "$POSTGRES_CONTAINER" psql \
        --set ON_ERROR_STOP=on \
        --username "$POSTGRES_USER" \
        --dbname "$POSTGRES_DB" \
        < "$ROOT_DIR/src/clashlens_prototype/schema.sql" >/dev/null
}

build_image() {
    require_podman
    "$PODMAN_BIN" build --pull=missing --file "$ROOT_DIR/Containerfile" --tag "$IMAGE_NAME" "$ROOT_DIR" >/dev/null
}

runtime_init() {
    load_config
    validate_config
    require_podman
    ensure_host_secrets
    ensure_podman_secret postgres-password
    ensure_podman_secret database-url
    ensure_podman_secret archive-access-key
    ensure_podman_secret archive-secret-key
    ensure_podman_secret typescript-current
    ensure_podman_secret typescript-previous
    ensure_network_and_volume
    if ! container_exists "$POSTGRES_CONTAINER"; then
        start_postgres
    else
        container_is_owned "$POSTGRES_CONTAINER" || die "existing PostgreSQL container is not owned by this prototype"
        "$PODMAN_BIN" start "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
    fi
    wait_for_postgres
    apply_schema
}

start_api() {
    remove_container "$API_CONTAINER"
    "$PODMAN_BIN" run --detach \
        --name "$API_CONTAINER" \
        --label io.clashlens.prototype=true \
        --network "$NETWORK_NAME" \
        --publish "127.0.0.1:${API_PORT}:8000" \
        --secret "$(secret_name database-url),type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400" \
        --secret "$(secret_name typescript-current),type=mount,target=/run/secrets/typescript-current,uid=10001,gid=10001,mode=0400" \
        --secret "$(secret_name typescript-previous),type=mount,target=/run/secrets/typescript-previous,uid=10001,gid=10001,mode=0400" \
        --env CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url \
        --env CLASHLENS_HMAC_CALLER="$HMAC_CALLER" \
        --env CLASHLENS_HMAC_KEY_ID="$HMAC_KEY_ID" \
        --env CLASHLENS_HMAC_SECRET_FILE=/run/secrets/typescript-current \
        --env CLASHLENS_HMAC_PREVIOUS_KEY_ID="$HMAC_PREVIOUS_KEY_ID" \
        --env CLASHLENS_HMAC_PREVIOUS_SECRET_FILE=/run/secrets/typescript-previous \
        --env CLASHLENS_API_MAX_BODY_BYTES="$API_MAX_BODY_BYTES" \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --memory=512m \
        --pids-limit=256 \
        --cap-drop=ALL \
        --security-opt=no-new-privileges \
        --stop-timeout 20 \
        "$IMAGE_NAME" serve --host 0.0.0.0 --port 8000 >/dev/null
}

start_worker() {
    local insecure=${1:-false}
    local endpoint=${2:-$ARCHIVE_ENDPOINT}
    remove_container "$WORKER_CONTAINER"
    local archive_security_args=()
    if [[ "$insecure" == true ]]; then
        archive_security_args+=(--archive-insecure-test-only)
    fi
    "$PODMAN_BIN" run --detach \
        --name "$WORKER_CONTAINER" \
        --label io.clashlens.prototype=true \
        --network "$NETWORK_NAME" \
        --secret "$(secret_name database-url),type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400" \
        --secret "$(secret_name archive-access-key),type=mount,target=/run/secrets/archive-access-key,uid=10001,gid=10001,mode=0400" \
        --secret "$(secret_name archive-secret-key),type=mount,target=/run/secrets/archive-secret-key,uid=10001,gid=10001,mode=0400" \
        --env CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url \
        --env CLASHLENS_ARCHIVE_ENDPOINT="$endpoint" \
        --env CLASHLENS_ARCHIVE_BUCKET="$ARCHIVE_BUCKET" \
        --env CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key \
        --env CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key \
        --env CLASHLENS_ARCHIVE_MAX_BODY_BYTES="$ARCHIVE_MAX_BODY_BYTES" \
        --env CLASHLENS_ARCHIVE_CONNECT_TIMEOUT_SECONDS="$ARCHIVE_CONNECT_TIMEOUT_SECONDS" \
        --env CLASHLENS_ARCHIVE_READ_TIMEOUT_SECONDS="$ARCHIVE_READ_TIMEOUT_SECONDS" \
        --env CLASHLENS_ARCHIVE_MAX_RETRIES="$ARCHIVE_MAX_RETRIES" \
        --env CLASHLENS_ARCHIVE_RETRY_BACKOFF_SECONDS="$ARCHIVE_RETRY_BACKOFF_SECONDS" \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        --memory=768m \
        --pids-limit=256 \
        --cap-drop=ALL \
        --security-opt=no-new-privileges \
        --stop-timeout 20 \
        "$IMAGE_NAME" worker --owner "$WORKER_CONTAINER" --run-forever --max-jobs 1 --poll-interval-seconds 1 "${archive_security_args[@]}" >/dev/null
}

wait_for_api() {
    command -v "$CURL_BIN" >/dev/null 2>&1 || die "curl is required to check API readiness"
    local attempt
    for attempt in $(seq 1 60); do
        if "$CURL_BIN" --fail --silent --show-error --max-time 3 "http://127.0.0.1:${API_PORT}/readyz" | grep -q '"ready":true'; then
            return
        fi
        sleep 1
    done
    die "prototype API did not become ready"
}

cmd_init() {
    runtime_init
    say "prototype database, volume, network, and file-backed secrets are ready"
}

cmd_up() {
    runtime_init
    build_image
    refresh_hmac_secrets
    start_api
    start_worker false
    wait_for_api
    say "prototype API is ready at http://127.0.0.1:${API_PORT}"
}

cmd_down() {
    require_podman
    remove_container "$ARCHIVE_CONTAINER"
    remove_container "$WORKER_CONTAINER"
    remove_container "$API_CONTAINER"
    remove_container "$POSTGRES_CONTAINER"
    say "prototype containers removed; database volume and secret files were kept"
}

cmd_status() {
    require_podman
    "$PODMAN_BIN" ps --all --filter "name=^${PREFIX}-" --format '{{.Names}}\t{{.Status}}'
    "$PODMAN_BIN" network inspect "$NETWORK_NAME" >/dev/null 2>&1 && say "network: $NETWORK_NAME" || say "network: absent"
    "$PODMAN_BIN" volume inspect "$VOLUME_NAME" >/dev/null 2>&1 && say "volume: $VOLUME_NAME" || say "volume: absent"
}

cmd_logs() {
    require_podman
    local component=${1:-api}
    case "$component" in
        api) "$PODMAN_BIN" logs "$API_CONTAINER" ;;
        worker) "$PODMAN_BIN" logs "$WORKER_CONTAINER" ;;
        postgres) "$PODMAN_BIN" logs "$POSTGRES_CONTAINER" ;;
        *) die "logs component must be api, worker, or postgres" ;;
    esac
}

probe_saved_player() {
    local output
    output=$("$PODMAN_BIN" exec "$API_CONTAINER" python -m clashlens_prototype.cli probe \
        --url "http://127.0.0.1:8000/v1/players/%232PP" \
        --caller "$HMAC_CALLER" \
        --key-id "$HMAC_KEY_ID" \
        --secret-file /run/secrets/typescript-current 2>/dev/null) || return 1
    [[ "$output" == *'"tag": "#2PP"'* || "$output" == *'"tag":"#2PP"'* ]]
}

wait_for_saved_player() {
    local attempt
    for attempt in $(seq 1 60); do
        if probe_saved_player; then
            return
        fi
        sleep 1
    done
    die "synthetic profile was not available through the saved-data API"
}

cleanup_verify() {
    set +e
    remove_container "$ARCHIVE_CONTAINER"
    if [[ "$VERIFY_INSECURE_WORKER" == true ]]; then
        start_worker false >/dev/null 2>&1 || true
        VERIFY_INSECURE_WORKER=false
    fi
}

cmd_verify() {
    load_config
    validate_config
    runtime_init
    build_image
    refresh_hmac_secrets
    start_api
    wait_for_api
    trap cleanup_verify EXIT
    remove_container "$ARCHIVE_CONTAINER"
    "$PODMAN_BIN" run --detach \
        --name "$ARCHIVE_CONTAINER" \
        --label io.clashlens.prototype=true \
        --network "$NETWORK_NAME" \
        --network-alias archive-fixture \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=16m \
        --memory=128m \
        --pids-limit=64 \
        --cap-drop=ALL \
        --security-opt=no-new-privileges \
        --entrypoint python \
        "$IMAGE_NAME" /opt/clashlens/scripts/fake_archive.py \
        --file /opt/clashlens/testdata/legend_i_profile_v1.json \
        --bucket "$ARCHIVE_BUCKET" --host 0.0.0.0 --port 9000 >/dev/null
    start_worker true archive-fixture:9000
    VERIFY_INSECURE_WORKER=true
    local digest object_key now
    digest=$(sha256sum "$FIXTURE_FILE" | cut -d ' ' -f 1)
    object_key="synthetic/${digest}.json"
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    "$PODMAN_BIN" exec "$WORKER_CONTAINER" python -m clashlens_prototype.cli seed \
        --occurrence-key "synthetic-verify-${digest}" \
        --tag '#2PP' \
        --endpoint profile \
        --observed-at "$now" \
        --response-hash "$digest" \
        --archive-reference "s3://${ARCHIVE_BUCKET}/${object_key}" \
        --max-attempts 2 >/dev/null
    wait_for_saved_player
    remove_container "$ARCHIVE_CONTAINER"
    start_worker false
    VERIFY_INSECURE_WORKER=false
    wait_for_saved_player
    trap - EXIT
    say "synthetic profile verified through the temporary archive and saved-data API"
}

main() {
    local command=${1:-}
    case "$command" in
        init) cmd_init ;;
        build) load_config; validate_config; build_image ;;
        up) cmd_up ;;
        down) cmd_down ;;
        status) cmd_status ;;
        logs) shift; cmd_logs "${1:-api}" ;;
        verify) cmd_verify ;;
        -h|--help|help) usage ;;
        *) usage >&2; exit 2 ;;
    esac
}

main "$@"
