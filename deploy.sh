#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CANONICAL_REPOSITORY_URL=https://github.com/zzzubair/clashlens
GIT_BIN=${GIT_BIN:-git}
MIGRATION_FILES=(
  "$ROOT_DIR/deploy/migrations/0001_collector.sql"
  "$ROOT_DIR/deploy/migrations/0002_python_layer.sql"
  "$ROOT_DIR/deploy/migrations/0003_regular_poll_dedup.sql"
  "$ROOT_DIR/deploy/migrations/0004_source_parser_v2.sql"
  "$ROOT_DIR/deploy/migrations/0005_army_decoding.sql"
  "$ROOT_DIR/deploy/migrations/0006_provider_identities.sql"
  "$ROOT_DIR/deploy/migrations/0007_player_discovery.sql"
  "$ROOT_DIR/deploy/migrations/0008_public_army_analytics.sql"
  "$ROOT_DIR/deploy/migrations/0009_raw_evidence.sql"
  "$ROOT_DIR/deploy/migrations/0010_boundary_publication_coordinator.sql"
  "$ROOT_DIR/deploy/migrations/0011_boundary_publication_contract.sql"
  "$ROOT_DIR/deploy/migrations/0012_parsed_content_dedup.sql"
  "$ROOT_DIR/deploy/migrations/0013_army_backfill_priority.sql"
)
ENV_FILE=${DEPLOY_ENV_FILE:-"$ROOT_DIR/app.env"}
PODMAN_BIN=${PODMAN_BIN:-podman}
CURL_BIN=${CURL_BIN:-curl}
PYTHON_BIN=${PYTHON_BIN:-python3}

# These values are deployment metadata, not application credentials.
NETWORK_NAME=clashlens-private
POSTGRES_VOLUME=clashlens-postgres-data
POSTGRES_CONTAINER=clashlens-postgres
COLLECTOR_CONTAINER=clashlens-collector
COLLECTOR_BRIDGE_CONTAINER=clashlens-collector-bridge
PYTHON_WORKER_CONTAINER=clashlens-python-worker
PYTHON_API_CONTAINER=clashlens-python-api
WEBSITE_CONTAINER=clashlens-website
POSTGRES_IMAGE=docker.io/library/postgres:17-alpine
COLLECTOR_IMAGE=localhost/clashlens-collector:deployment
PYTHON_IMAGE=localhost/clashlens-python:deployment
WEBSITE_IMAGE=localhost/clashlens-website:deployment
HEALTH_HOST=127.0.0.1
HEALTH_PORT=8081

# Fixed PostgreSQL runtime roles. Their passwords are set and rotated through
# admin psql stdin; the admin role remains POSTGRES_USER.
COLLECTOR_ROLE=clashlens_collector
WORKER_ROLE=clashlens_python_worker
API_ROLE=clashlens_python_api
API_NETWORK_ALIAS=python-api
API_LISTEN_PORT=8000

# Worker replicas are identical containers over one shared queue. The
# configured count is bounded by the target host's 16 hardware threads.
# Replicas are named <CLASHLENS_PYTHON_WORKER_CONTAINER>-1..-N.
WORKER_REPLICA_MAX=16

# Bounded in-process Python worker concurrency. The sequential-compatible
# default is 1 lane per replica. The target-host profile is one replica with
# 12 lanes and 12 database/archive connections. The two pool sizes bound each
# replica's PostgreSQL and archive HTTP connections; the sequential defaults
# keep a single worker compatible with prior behavior.
WORKER_CONCURRENCY_MAX=32
WORKER_POOL_SIZE_MAX=64

# The collector PostgreSQL pool is explicitly bounded. The safe default of 16
# matches the pgxpool implicit default on the 16-thread production host. The
# target-host profile uses 32 connections for 4 normal keys at 8 workers per
# key; interactive work shares the same bounded pool.
COLLECTOR_POOL_SIZE_MAX=64

COLLECTOR_STOP_GRACE=30
API_STOP_GRACE=30
POSTGRES_STOP_GRACE=60

# app.env is parsed without evaluating shell syntax. This prevents a typo in
# the file from running a command and keeps credentials out of diagnostic
# output.
declare -Ag ENV_PRESENT=()

usage() {
  cat >&2 <<'EOF'
Usage: ./deploy.sh <command> [arguments]

Commands:
  init                         Create private Podman resources; apply migration
                               0001 only on an absent database.
  up                           Build the collector image, migrate the contract
                               to version 5 (stop -> migrate -> restart),
                               and start the required collector.
  restart                      Start-only recovery for an existing version-5
                               stack: never builds and never runs SQL.
  build-collector              Build the immutable collector image only.
  build-python                 Build the immutable Python image only.
  build-website                Build the immutable website image only.
  candidate-prepare            Prepare only the configured disposable
                               PostgreSQL database through migration 0013.
  deployment-receipt <scope> <environment> <results-dir>
                               Write a candidate-preparation or deployed-stack
                               evidence receipt outside the checkout.
  operating-check [--previous-snapshot <path>]
                               Print one bounded private operating snapshot and
                               exit 0 healthy, 1 objective failure, or 2 unknown.
  python-up                    Build the Python image, then start the private
                               API and the worker replicas.
  python-start                 Start-only path for the private API and worker
                               replicas.
  api-start                    Start-only path for the private Python API.
  worker-start                 Start-only path for the worker replicas.
  website-up                   Build, replace, start, and wait for the website.
  website-start                Start-only path for the website container.
  status                       Show safe container, network, and volume status.
  logs [collector|postgres|python-api|python-worker[-N]|website]
                               Show logs for one private container.
  down                         Gracefully stop and remove all containers; keep
                               the data volume.
  stack-down                   Gracefully stop and remove only collector and
                               PostgreSQL.
  python-down                  Gracefully stop and remove website, API, and
                               worker replicas.
  api-down                     Gracefully stop website, then remove only the API.
  worker-down                  Gracefully stop and remove only the worker
                               replicas.
  website-down                 Gracefully stop and remove only the website.
  enqueue [collector args]     Enqueue work, or pass a tag as the only argument.
  maintenance <collector args> Run a collector maintenance command.
  queue-status                 Show the Python worker queue status from
                               replica 1.
  support-recovery-exec <args> Run recovery inside the configured private API
                               container (for the restricted host wrapper).
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

# A database password must be high-entropy and restricted to URL-safe
# unreserved bytes so it can appear unescaped inside a PostgreSQL URL.
url_safe_password() {
  local name=$1
  local value=${!name:-}
  [[ -n "$value" ]] || die "$name is required in app.env"
  case "$value" in
    ""|CHANGE_ME|REPLACE_ME|replace-me|change-me)
      die "$name must contain a deployment value"
      ;;
  esac
  [[ "$value" =~ ^[A-Za-z0-9_-]{32,128}$ ]] || \
    die "$name must contain 32-128 URL-safe unreserved characters (A-Za-z0-9_-)"
}

validate_resource_setting() {
  local name=$1
  local kind=$2
  local value=${!name:-}
  [[ -n "$value" ]] || die "$name is required in app.env"
  case "$value" in
    ""|CHANGE_ME|REPLACE_ME|replace-me|change-me)
      die "$name must contain a deployment value"
      ;;
  esac
  case "$kind" in
    memory)
      [[ "$value" =~ ^[0-9]+[bkmgt]?$ ]] || \
        die "$name must be a byte count with an optional b/k/m/g/t suffix"
      ;;
    cpus)
      [[ "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "$name must be a CPU count"
      ;;
    pids)
      [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be a PID count"
      ;;
  esac
}

validate_resource_budgets() {
  local component kind
  for component in POSTGRES COLLECTOR API WORKER WEBSITE; do
    for kind in MEMORY CPUS PIDS; do
      case "$kind" in
        MEMORY) validate_resource_setting "CLASHLENS_${component}_${kind}" memory ;;
        CPUS) validate_resource_setting "CLASHLENS_${component}_${kind}" cpus ;;
        PIDS) validate_resource_setting "CLASHLENS_${component}_${kind}" pids ;;
      esac
    done
  done
  validate_resource_setting CLASHLENS_POSTGRES_SHM_SIZE memory
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
    CLASHLENS_COLLECTOR_DB_PASSWORD
    CLASHLENS_WORKER_DB_PASSWORD
    CLASHLENS_API_DB_PASSWORD
    CLASHLENS_ARCHIVE_ENDPOINT
    CLASHLENS_ARCHIVE_BUCKET
    CLASHLENS_ARCHIVE_ACCESS_KEY
    CLASHLENS_ARCHIVE_SECRET_KEY
    CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY
    CLASHLENS_WORKER_ARCHIVE_SECRET_KEY
    CLASHLENS_OFFICIAL_API_ORIGIN
    CLASHLENS_OFFICIAL_API_PROXY_URL
    CLASHLENS_NORMAL_API_KEY_FILES
    CLASHLENS_INTERACTIVE_API_KEY_FILES
    CLASHLENS_API_KEY_HOST_DIR
    CLASHLENS_INTERACTIVE_API_KEY_FILE
    CLASHLENS_HMAC_CALLER
    CLASHLENS_HMAC_KEY_ID
    CLASHLENS_HMAC_SECRET_FILE
    CLASHLENS_WEBSITE_HOST
    CLASHLENS_WEBSITE_PORT
    CLASHLENS_WORKER_LEASE_SECONDS
  )
  local name
  for name in "${required[@]}"; do
    required_setting "$name"
  done
  if [[ -n "${CLASHLENS_ARCHIVE_INSTANCE_ID:-}" ]]; then
    for name in CLASHLENS_ARCHIVE_REGION CLASHLENS_ARCHIVE_INSTANCE_ID CLASHLENS_ARCHIVE_MARKER_KEY CLASHLENS_ARCHIVE_MARKER_HASH CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION; do
      required_setting "$name"
    done
  fi

  url_safe_password POSTGRES_PASSWORD
  url_safe_password CLASHLENS_COLLECTOR_DB_PASSWORD
  url_safe_password CLASHLENS_WORKER_DB_PASSWORD
  url_safe_password CLASHLENS_API_DB_PASSWORD
  not_placeholder CLASHLENS_ARCHIVE_ACCESS_KEY
  not_placeholder CLASHLENS_ARCHIVE_SECRET_KEY
  not_placeholder CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY
  not_placeholder CLASHLENS_WORKER_ARCHIVE_SECRET_KEY
  not_placeholder CLASHLENS_OFFICIAL_API_PROXY_URL

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

  # The admin URL is derived from POSTGRES_USER and POSTGRES_PASSWORD and is
  # used only by the migration bridge and role setup. No runtime container
  # receives it, so it must not be configured as a shared setting.
  [[ -z "${CLASHLENS_DATABASE_URL:-}" ]] || die "CLASHLENS_DATABASE_URL must not be set; role URLs are derived from role passwords"
  [[ -z "${ENV_PRESENT[CLASHLENS_DATABASE_URL]+present}" ]] || die "CLASHLENS_DATABASE_URL must not be set; role URLs are derived from role passwords"
  # The API container receives the official key file and proxy URL under the
  # CLI serve env names. The deployment derives them from the shared
  # interactive key file and proxy settings, so app.env must not set them.
  [[ -z "${CLASHLENS_OFFICIAL_KEY_FILE:-}" ]] || die "CLASHLENS_OFFICIAL_KEY_FILE is derived from CLASHLENS_INTERACTIVE_API_KEY_FILE; do not set it in app.env"
  [[ -z "${ENV_PRESENT[CLASHLENS_OFFICIAL_KEY_FILE]+present}" ]] || die "CLASHLENS_OFFICIAL_KEY_FILE is derived from CLASHLENS_INTERACTIVE_API_KEY_FILE; do not set it in app.env"
  [[ -z "${CLASHLENS_OFFICIAL_PROXY_URL:-}" ]] || die "CLASHLENS_OFFICIAL_PROXY_URL is derived from CLASHLENS_OFFICIAL_API_PROXY_URL; do not set it in app.env"
  [[ -z "${ENV_PRESENT[CLASHLENS_OFFICIAL_PROXY_URL]+present}" ]] || die "CLASHLENS_OFFICIAL_PROXY_URL is derived from CLASHLENS_OFFICIAL_API_PROXY_URL; do not set it in app.env"
  [[ -z "${CLASHLENS_PYTHON_WORKER_IMAGE:-}" ]] || die "CLASHLENS_PYTHON_WORKER_IMAGE was renamed to CLASHLENS_PYTHON_IMAGE"
  [[ -z "${ENV_PRESENT[CLASHLENS_ENABLE_GLOBAL_RANKINGS]+present}" ]] || \
    die "CLASHLENS_ENABLE_GLOBAL_RANKINGS is deployment-owned; remove it from app.env"

  [[ "$POSTGRES_DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "POSTGRES_DB must be a valid PostgreSQL database name"
  [[ "$POSTGRES_USER" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "POSTGRES_USER must be a valid PostgreSQL role name"
  [[ "$CLASHLENS_OFFICIAL_API_ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || \
    die "CLASHLENS_OFFICIAL_API_ORIGIN must be an HTTPS origin without a path"
  [[ "$CLASHLENS_OFFICIAL_API_PROXY_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || \
    die "CLASHLENS_OFFICIAL_API_PROXY_URL must be an HTTP(S) origin with a plain host and optional port, without credentials or a path"
  [[ "$CLASHLENS_ARCHIVE_ENDPOINT" != *"://"* ]] || die "CLASHLENS_ARCHIVE_ENDPOINT must be a host:port endpoint"
  [[ "$CLASHLENS_ARCHIVE_ENDPOINT" != */* ]] || die "CLASHLENS_ARCHIVE_ENDPOINT must not contain a path"
  [[ "$CLASHLENS_ARCHIVE_ENDPOINT" =~ ^[A-Za-z0-9._:-]+$ ]] || die "CLASHLENS_ARCHIVE_ENDPOINT contains unsafe characters"
  [[ "$CLASHLENS_ARCHIVE_BUCKET" =~ ^[A-Za-z0-9._-]{3,63}$ ]] || die "CLASHLENS_ARCHIVE_BUCKET is malformed"
  if [[ -n "${CLASHLENS_ARCHIVE_INSTANCE_ID:-}" ]]; then
    [[ "$CLASHLENS_ARCHIVE_INSTANCE_ID" =~ ^[A-Za-z0-9._:-]{1,128}$ ]] || die "CLASHLENS_ARCHIVE_INSTANCE_ID is malformed"
    [[ "$CLASHLENS_ARCHIVE_MARKER_KEY" =~ ^[A-Za-z0-9._/-]+$ && "$CLASHLENS_ARCHIVE_MARKER_KEY" != /* && "$CLASHLENS_ARCHIVE_MARKER_KEY" != *..* && -n "$CLASHLENS_ARCHIVE_MARKER_KEY" ]] || die "CLASHLENS_ARCHIVE_MARKER_KEY is unsafe"
    [[ "$CLASHLENS_ARCHIVE_MARKER_HASH" =~ ^[0-9a-f]{64}$ ]] || die "CLASHLENS_ARCHIVE_MARKER_HASH must be lowercase SHA-256"
    [[ "$CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION" =~ ^[A-Za-z0-9._-]{1,32}$ ]] || die "CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION is malformed"
  fi

  [[ "$CLASHLENS_API_KEY_HOST_DIR" == /* ]] || die "CLASHLENS_API_KEY_HOST_DIR must be an absolute directory"
  [[ "$CLASHLENS_HEALTH_HOST" == "127.0.0.1" ]] || die "CLASHLENS_HEALTH_HOST must be 127.0.0.1"
  [[ "$CLASHLENS_HEALTH_PORT" =~ ^[0-9]+$ ]] || die "CLASHLENS_HEALTH_PORT must be a number"
  (( CLASHLENS_HEALTH_PORT >= 1 && CLASHLENS_HEALTH_PORT <= 65535 )) || die "CLASHLENS_HEALTH_PORT is outside the valid range"

  [[ "$CLASHLENS_WORKER_LEASE_SECONDS" =~ ^[0-9]+$ ]] && (( CLASHLENS_WORKER_LEASE_SECONDS >= 1 )) || \
    die "CLASHLENS_WORKER_LEASE_SECONDS must be a positive integer"

  [[ "$CLASHLENS_WORKER_REPLICAS" =~ ^[0-9]+$ ]] && \
    (( CLASHLENS_WORKER_REPLICAS >= 1 && CLASHLENS_WORKER_REPLICAS <= WORKER_REPLICA_MAX )) || \
    die "CLASHLENS_WORKER_REPLICAS must be an integer between 1 and $WORKER_REPLICA_MAX"

  [[ "$CLASHLENS_WORKER_CONCURRENCY" =~ ^[0-9]+$ ]] && \
    (( CLASHLENS_WORKER_CONCURRENCY >= 1 && CLASHLENS_WORKER_CONCURRENCY <= WORKER_CONCURRENCY_MAX )) || \
    die "CLASHLENS_WORKER_CONCURRENCY must be an integer between 1 and $WORKER_CONCURRENCY_MAX"

  [[ "$CLASHLENS_WORKER_DATABASE_POOL_SIZE" =~ ^[0-9]+$ ]] && \
    (( CLASHLENS_WORKER_DATABASE_POOL_SIZE >= 1 && CLASHLENS_WORKER_DATABASE_POOL_SIZE <= WORKER_POOL_SIZE_MAX )) || \
    die "CLASHLENS_WORKER_DATABASE_POOL_SIZE must be an integer between 1 and $WORKER_POOL_SIZE_MAX"

  [[ "$CLASHLENS_WORKER_ARCHIVE_POOL_SIZE" =~ ^[0-9]+$ ]] && \
    (( CLASHLENS_WORKER_ARCHIVE_POOL_SIZE >= 1 && CLASHLENS_WORKER_ARCHIVE_POOL_SIZE <= WORKER_POOL_SIZE_MAX )) || \
    die "CLASHLENS_WORKER_ARCHIVE_POOL_SIZE must be an integer between 1 and $WORKER_POOL_SIZE_MAX"

  [[ "$CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE" =~ ^[0-9]+$ ]] && \
    (( CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE >= 1 && CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE <= COLLECTOR_POOL_SIZE_MAX )) || \
    die "CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE must be an integer between 1 and $COLLECTOR_POOL_SIZE_MAX"

  validate_key_specs CLASHLENS_NORMAL_API_KEY_FILES "$CLASHLENS_NORMAL_API_KEY_FILES" 4
  validate_key_specs CLASHLENS_INTERACTIVE_API_KEY_FILES "$CLASHLENS_INTERACTIVE_API_KEY_FILES" 1

  [[ "$CLASHLENS_INTERACTIVE_API_KEY_FILE" =~ ^/run/secrets/[^/]+$ ]] || \
    die "CLASHLENS_INTERACTIVE_API_KEY_FILE must be a direct file path under /run/secrets"
  [[ "$CLASHLENS_HMAC_SECRET_FILE" =~ ^/run/secrets/[^/]+$ ]] || \
    die "CLASHLENS_HMAC_SECRET_FILE must be a direct file path under /run/secrets"
  if [[ -n "${CLASHLENS_HMAC_PREVIOUS_KEY_ID:-}" || -n "${CLASHLENS_HMAC_PREVIOUS_SECRET_FILE:-}" ]]; then
    [[ -n "${CLASHLENS_HMAC_PREVIOUS_KEY_ID:-}" && -n "${CLASHLENS_HMAC_PREVIOUS_SECRET_FILE:-}" ]] || \
      die "CLASHLENS_HMAC_PREVIOUS_KEY_ID and CLASHLENS_HMAC_PREVIOUS_SECRET_FILE must be configured together"
    [[ "$CLASHLENS_HMAC_PREVIOUS_KEY_ID" != "$CLASHLENS_HMAC_KEY_ID" ]] || \
      die "current and previous HMAC key IDs must differ"
    [[ "$CLASHLENS_HMAC_PREVIOUS_SECRET_FILE" =~ ^/run/secrets/[^/]+$ ]] || \
      die "CLASHLENS_HMAC_PREVIOUS_SECRET_FILE must be a direct file path under /run/secrets"
  fi

  [[ "$CLASHLENS_WEBSITE_HOST" == "127.0.0.1" || "$CLASHLENS_WEBSITE_HOST" == "0.0.0.0" || \
    "$CLASHLENS_WEBSITE_HOST" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || \
    die "CLASHLENS_WEBSITE_HOST must be 127.0.0.1, 0.0.0.0, or a plain IPv4 address"
  if [[ "$CLASHLENS_WEBSITE_HOST" != "127.0.0.1" && "$CLASHLENS_WEBSITE_HOST" != "0.0.0.0" ]]; then
    local octet
    local -a octets=()
    IFS='.' read -r -a octets <<< "$CLASHLENS_WEBSITE_HOST"
    for octet in "${octets[@]}"; do
      (( 10#$octet <= 255 )) || die "CLASHLENS_WEBSITE_HOST must be a valid IPv4 address"
    done
  fi
  [[ "$CLASHLENS_WEBSITE_PORT" =~ ^[0-9]+$ ]] && \
    (( CLASHLENS_WEBSITE_PORT >= 1 && CLASHLENS_WEBSITE_PORT <= 65535 )) || \
    die "CLASHLENS_WEBSITE_PORT is outside the valid range"

  validate_resource_budgets
}

# A single key file setting names an in-container /run/secrets path; the host
# file is the basename inside CLASHLENS_API_KEY_HOST_DIR.
validate_single_key_file() {
  local name=$1
  local path=${!name:-}
  local source
  source="$CLASHLENS_API_KEY_HOST_DIR/${path##*/}"
  [[ -f "$source" ]] || die "key file for $name is missing"
  [[ -r "$source" ]] || die "key file for $name is not readable"
  [[ "$(stat -c '%a' "$source")" == "600" ]] || die "key file for $name must have mode 600"
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

source_revision_for_build() {
  local checkout_status source_revision
  if ! checkout_status=$("$GIT_BIN" -C "$ROOT_DIR" status --porcelain=v1 --untracked-files=all 2>/dev/null); then
    die "unable to verify git checkout state"
  fi
  [[ -z "$checkout_status" ]] || die "refusing to build from a dirty git checkout"

  if ! source_revision=$("$GIT_BIN" -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}' 2>/dev/null); then
    die "git HEAD is missing or unverifiable"
  fi
  [[ "$source_revision" =~ ^[0-9a-f]{40}$ || "$source_revision" =~ ^[0-9a-f]{64}$ ]] || \
    die "git HEAD is missing or unverifiable"
  printf '%s' "$source_revision"
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

create_secret_from_file() {
  local name=$1
  local source=$2
  "$PODMAN_BIN" secret create --replace "$name" "$source" >/dev/null
}

secret_rm() {
  "$PODMAN_BIN" secret rm "$1" >/dev/null 2>&1 || true
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

runtime_contract_version() { printf '5'; }

ensure_spool_root() {
  [[ "$CLASHLENS_SPOOL_ROOT" = /* && "$CLASHLENS_SPOOL_ROOT" != "/" ]] || die "CLASHLENS_SPOOL_ROOT must be an absolute non-root path"
  [[ ! -L "$CLASHLENS_SPOOL_ROOT" ]] || die "CLASHLENS_SPOOL_ROOT must not be a symlink"
  mkdir -p "$CLASHLENS_SPOOL_ROOT/.control/reservations" "$CLASHLENS_SPOOL_ROOT/.locks" "$CLASHLENS_SPOOL_ROOT/tmp" "$CLASHLENS_SPOOL_ROOT/sha256"
  chmod 700 "$CLASHLENS_SPOOL_ROOT" "$CLASHLENS_SPOOL_ROOT/.control" "$CLASHLENS_SPOOL_ROOT/.control/reservations" "$CLASHLENS_SPOOL_ROOT/.locks" "$CLASHLENS_SPOOL_ROOT/tmp" "$CLASHLENS_SPOOL_ROOT/sha256"
  chown -R 10001:10001 "$CLASHLENS_SPOOL_ROOT" 2>/dev/null || true
}

ensure_postgres() {
  replace_secret_value clashlens-postgres-password "$POSTGRES_PASSWORD"
  if container_exists "$POSTGRES_CONTAINER"; then
    local configured_shm_size configured_metrics_profile
    configured_shm_size=$("$PODMAN_BIN" container inspect \
      --format '{{index .Config.Labels "org.clashlens.postgres-shm-size"}}' \
      "$POSTGRES_CONTAINER" 2>/dev/null || true)
    [[ "$configured_shm_size" == "$CLASHLENS_POSTGRES_SHM_SIZE" ]] || \
      die "existing PostgreSQL container does not use CLASHLENS_POSTGRES_SHM_SIZE=$CLASHLENS_POSTGRES_SHM_SIZE; run stack-down, then up to recreate containers while preserving the named data volume"
    configured_metrics_profile=$("$PODMAN_BIN" container inspect \
      --format '{{index .Config.Labels "org.clashlens.postgres-metrics-profile"}}' \
      "$POSTGRES_CONTAINER" 2>/dev/null || true)
    [[ "$configured_metrics_profile" == "step6-v1" ]] || \
      die "existing PostgreSQL container does not enable the Step 6 metrics profile; run stack-down, then up to recreate containers while preserving the named data volume"
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
    --memory "$CLASHLENS_POSTGRES_MEMORY" \
    --shm-size "$CLASHLENS_POSTGRES_SHM_SIZE" \
    --pids-limit "$CLASHLENS_POSTGRES_PIDS" \
    --cpus "$CLASHLENS_POSTGRES_CPUS" \
    --health-cmd "pg_isready -U $POSTGRES_USER -d $POSTGRES_DB" \
    --health-interval 10s \
    --health-timeout 5s \
    --health-retries 6 \
    --restart unless-stopped \
    --label org.clashlens.component=postgres \
    --label "org.clashlens.postgres-shm-size=$CLASHLENS_POSTGRES_SHM_SIZE" \
    --label org.clashlens.postgres-metrics-profile=step6-v1 \
    "$POSTGRES_IMAGE" \
    postgres \
    -c shared_preload_libraries=pg_stat_statements \
    -c pg_stat_statements.track=all \
    -c track_io_timing=on \
    -c track_wal_io_timing=on >/dev/null
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

schema_migration_applied() {
  local version=$1 applied
  applied=$("$PODMAN_BIN" exec "$POSTGRES_CONTAINER" \
    psql --tuples-only --no-align --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --command "SELECT EXISTS (SELECT 1 FROM clash_lens_schema_migrations WHERE version = $version)" \
    2>/dev/null || true)
  [[ "$(trim "$applied")" == "t" ]]
}

require_current_schema() {
  local required_version=${#MIGRATION_FILES[@]}
  schema_migration_applied "$required_version" || \
    die "forward migration $required_version is required; run up first"
}

ensure_archive_instance_contract() {
  [[ -n "${CLASHLENS_ARCHIVE_INSTANCE_ID:-}" ]] || return 0
  local sql
  sql="INSERT INTO archive_instances (instance_id, endpoint, region, bucket, marker_key, marker_hash, marker_payload_version) VALUES ('${CLASHLENS_ARCHIVE_INSTANCE_ID//\'/\'\'}', '${CLASHLENS_ARCHIVE_ENDPOINT//\'/\'\'}', '${CLASHLENS_ARCHIVE_REGION//\'/\'\'}', '${CLASHLENS_ARCHIVE_BUCKET//\'/\'\'}', '${CLASHLENS_ARCHIVE_MARKER_KEY//\'/\'\'}', '${CLASHLENS_ARCHIVE_MARKER_HASH//\'/\'\'}', '${CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION//\'/\'\'}') ON CONFLICT (instance_id) DO UPDATE SET endpoint = EXCLUDED.endpoint, region = EXCLUDED.region, bucket = EXCLUDED.bucket, marker_key = EXCLUDED.marker_key, marker_hash = EXCLUDED.marker_hash, marker_payload_version = EXCLUDED.marker_payload_version WHERE archive_instances.endpoint = EXCLUDED.endpoint AND archive_instances.region = EXCLUDED.region AND archive_instances.bucket = EXCLUDED.bucket AND archive_instances.marker_key = EXCLUDED.marker_key AND archive_instances.marker_hash = EXCLUDED.marker_hash AND archive_instances.marker_payload_version = EXCLUDED.marker_payload_version;"
  printf '%s\n' "$sql" | "$PODMAN_BIN" exec --interactive "$POSTGRES_CONTAINER" psql --quiet --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
}

apply_pending_forward_migrations() {
  local index migration_file version
  for ((index = 1; index < ${#MIGRATION_FILES[@]}; index++)); do
    version=$((index + 1))
    migration_file=${MIGRATION_FILES[$index]}
    if (( version == 9 )) && [[ -z "${CLASHLENS_ARCHIVE_INSTANCE_ID:-}" ]]; then continue; fi
    if ! schema_migration_applied "$version"; then
      if (( version == 4 || version == 12 )); then
        # Parser v2 changes the interpretation of source rows. Drain the old
        # image before installing its claim fence so no retained lease can
        # publish with the superseded interpretation after the migration.
        stop_all_worker_containers
      elif (( version == 13 )); then
        # The army backfill class changes claim ordering; drain old workers
        # before installing the new priority fence.
        stop_all_worker_containers
      fi
      apply_migration_file "$migration_file"
    fi
  done
}

# Detect the deployed contract explicitly. An empty or failing read means the
# database has no contract yet; anything outside 1 through 5 is unsupported.
contract_version() {
  local version
  version=$("$PODMAN_BIN" exec "$POSTGRES_CONTAINER" \
    psql --tuples-only --no-align --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --command 'SELECT version FROM clash_lens_contract WHERE singleton' 2>/dev/null || true)
  version=$(trim "$version")
  case "$version" in
    "")
      printf 'absent\n'
      ;;
    1|2)
      printf '%s\n' "$version"
      ;;
    3)
      [[ -n "${CLASHLENS_ARCHIVE_INSTANCE_ID:-}" ]] && printf '%s\n' "$version" || printf 'unknown\n'
      ;;
    4|5)
      printf '%s\n' "$version"
      ;;
    *)
      printf 'unknown\n'
      ;;
  esac
}

admin_database_url() {
  printf 'postgresql://%s:' "$POSTGRES_USER"
  printf '%s' "$POSTGRES_PASSWORD"
  printf '@postgres:5432/%s?sslmode=disable' "$POSTGRES_DB"
}

role_database_url() {
  local role=$1
  local password=$2
  printf 'postgresql://%s:' "$role"
  printf '%s' "$password"
  printf '@postgres:5432/%s?sslmode=disable' "$POSTGRES_DB"
}

# Set and rotate the three runtime role passwords through admin psql stdin.
# The SQL text, including the passwords, never appears in process arguments.
configure_runtime_roles() {
  local sql
  sql="ALTER ROLE $COLLECTOR_ROLE WITH LOGIN PASSWORD '$CLASHLENS_COLLECTOR_DB_PASSWORD';"
  sql+=" ALTER ROLE $WORKER_ROLE WITH LOGIN PASSWORD '$CLASHLENS_WORKER_DB_PASSWORD';"
  sql+=" ALTER ROLE $API_ROLE WITH LOGIN PASSWORD '$CLASHLENS_API_DB_PASSWORD';"
  sql+=" REVOKE ALL PRIVILEGES ON TABLE python_processing_jobs FROM $WORKER_ROLE;"
  sql+=" GRANT SELECT (id, lease_generation) ON TABLE python_processing_jobs TO $WORKER_ROLE;"
  printf '%s\n' "$sql" | "$PODMAN_BIN" exec --interactive "$POSTGRES_CONTAINER" \
    psql --quiet --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
}

build_collector_image() {
  local source_revision=${1:-}
  [[ -n "$source_revision" ]] || source_revision=$(source_revision_for_build)
  "$PODMAN_BIN" build \
    --pull=missing \
    --file "$ROOT_DIR/Containerfile" \
    --label "org.opencontainers.image.source=$CANONICAL_REPOSITORY_URL" \
    --label "org.opencontainers.image.revision=$source_revision" \
    --tag "$COLLECTOR_IMAGE" \
    "$ROOT_DIR"
}

build_python_image() {
  local source_revision=${1:-}
  [[ -n "$source_revision" ]] || source_revision=$(source_revision_for_build)
  "$PODMAN_BIN" build \
    --pull=missing \
    --file "$ROOT_DIR/python/Containerfile" \
    --label "org.opencontainers.image.source=$CANONICAL_REPOSITORY_URL" \
    --label "org.opencontainers.image.revision=$source_revision" \
    --tag "$PYTHON_IMAGE" \
    "$ROOT_DIR/python"
}

build_website_image() {
  local source_revision=${1:-}
  [[ -n "$source_revision" ]] || source_revision=$(source_revision_for_build)
  "$PODMAN_BIN" build \
    --pull=missing \
    --file "$ROOT_DIR/website/Containerfile" \
    --label "org.opencontainers.image.source=$CANONICAL_REPOSITORY_URL" \
    --label "org.opencontainers.image.revision=$source_revision" \
    --tag "$WEBSITE_IMAGE" \
    "$ROOT_DIR/website"
}

write_deployment_receipt() {
  local scope=$1 environment=$2 results_dir=$3
  "$PYTHON_BIN" "$ROOT_DIR/scripts/deployment_receipt.py" \
    --root "$ROOT_DIR" \
    --scope "$scope" \
    --environment "$environment" \
    --results-dir "$results_dir" \
    --podman-bin "$PODMAN_BIN" \
    --git-bin "$GIT_BIN" \
    --postgres-container "$POSTGRES_CONTAINER" \
    --postgres-user "$POSTGRES_USER" \
    --postgres-database "$POSTGRES_DB" \
    --collector-image "$COLLECTOR_IMAGE" \
    --python-image "$PYTHON_IMAGE" \
    --website-image "$WEBSITE_IMAGE" \
    --collector-container "$COLLECTOR_CONTAINER" \
    --python-container "$PYTHON_API_CONTAINER" \
    --website-container "$WEBSITE_CONTAINER" \
    --safe-config "collector_database_pool_size=$CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE" \
    --safe-config "spool_free_inode_floor=$CLASHLENS_SPOOL_FREE_INODE_FLOOR" \
    --safe-config "spool_free_space_floor=$CLASHLENS_SPOOL_FREE_SPACE_FLOOR" \
    --safe-config "spool_max_body_bytes=$CLASHLENS_MAX_BODY_BYTES" \
    --safe-config "spool_max_bytes=$CLASHLENS_SPOOL_MAX_BYTES" \
    --safe-config "spool_max_objects=$CLASHLENS_SPOOL_MAX_OBJECTS" \
    --safe-config "worker_archive_pool_size=$CLASHLENS_WORKER_ARCHIVE_POOL_SIZE" \
    --safe-config "worker_concurrency=$CLASHLENS_WORKER_CONCURRENCY" \
    --safe-config "worker_database_pool_size=$CLASHLENS_WORKER_DATABASE_POOL_SIZE" \
    --safe-config "worker_lease_seconds=$CLASHLENS_WORKER_LEASE_SECONDS" \
    --safe-config "worker_replicas=$CLASHLENS_WORKER_REPLICAS"
}

prepare_candidate_database() {
  local i name version
  required_setting CLASHLENS_ARCHIVE_INSTANCE_ID
  [[ "$NETWORK_NAME" != "clashlens-private" && \
     "$POSTGRES_VOLUME" != "clashlens-postgres-data" && \
     "$POSTGRES_CONTAINER" != "clashlens-postgres" && \
     "$COLLECTOR_CONTAINER" != "clashlens-collector" && \
     "$PYTHON_API_CONTAINER" != "clashlens-python-api" && \
     "$PYTHON_WORKER_CONTAINER" != "clashlens-python-worker" && \
     "$WEBSITE_CONTAINER" != "clashlens-website" ]] || \
    die "candidate-prepare requires dedicated non-default Podman resource names"
  container_exists "$POSTGRES_CONTAINER" && \
    die "candidate-prepare refuses an existing PostgreSQL container"
  "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1 && \
    die "candidate-prepare refuses an existing Podman network"
  "$PODMAN_BIN" volume exists "$POSTGRES_VOLUME" >/dev/null 2>&1 && \
    die "candidate-prepare refuses an existing PostgreSQL volume"
  for name in "$COLLECTOR_CONTAINER" "$PYTHON_API_CONTAINER" \
    "$PYTHON_WORKER_CONTAINER" "$WEBSITE_CONTAINER"; do
    container_exists "$name" && \
      die "candidate-prepare refuses an existing application container: $name"
  done
  for ((i = 1; i <= WORKER_REPLICA_MAX; i++)); do
    name="${PYTHON_WORKER_CONTAINER}-${i}"
    container_exists "$name" && \
      die "candidate-prepare refuses an existing application container: $name"
  done
  initialize_runtime
  version=$(contract_version)
  case "$version" in
    absent)
      apply_initial_contract
      ;;
    1|2|3|4|5)
      ;;
    *)
      die "unsupported contract version $version"
      ;;
  esac
  apply_pending_forward_migrations
  ensure_archive_instance_contract
  [[ "$(contract_version)" == "$(runtime_contract_version)" ]] || \
    die "candidate database did not reach contract version $(runtime_contract_version)"
  for ((i = 1; i <= ${#MIGRATION_FILES[@]}; i++)); do
    schema_migration_applied "$i" || \
      die "candidate database is missing forward migration $i"
  done
  printf 'disposable candidate database is ready through migration %s\n' \
    "${#MIGRATION_FILES[@]}"
}

image_exists() {
  "$PODMAN_BIN" image exists "$COLLECTOR_IMAGE" >/dev/null 2>&1
}

python_image_exists() {
  "$PODMAN_BIN" image exists "$PYTHON_IMAGE" >/dev/null 2>&1
}

website_image_exists() {
  "$PODMAN_BIN" image exists "$WEBSITE_IMAGE" >/dev/null 2>&1
}

# Forward only non-secret collector settings. Secret-bearing and deployment
# metadata names are excluded; role credentials travel as mounted file paths.
collector_env_args() {
  local -n result=$1
  local name

  for name in "${!ENV_PRESENT[@]}"; do
    case "$name" in
      CLASHLENS_DATABASE_URL|CLASHLENS_DATABASE_URL_FILE|CLASHLENS_*_DB_PASSWORD|CLASHLENS_ARCHIVE_ACCESS_KEY|CLASHLENS_ARCHIVE_SECRET_KEY|CLASHLENS_WORKER_ARCHIVE_*|CLASHLENS_NORMAL_API_KEYS|CLASHLENS_INTERACTIVE_API_KEYS|CLASHLENS_NORMAL_API_KEY_FILES|CLASHLENS_INTERACTIVE_API_KEY_FILES|CLASHLENS_API_KEY_HOST_DIR|CLASHLENS_HEALTH_HOST|CLASHLENS_HEALTH_PORT|CLASHLENS_PODMAN_*|CLASHLENS_POSTGRES_CONTAINER|CLASHLENS_POSTGRES_IMAGE|CLASHLENS_COLLECTOR_CONTAINER|CLASHLENS_COLLECTOR_IMAGE|CLASHLENS_PYTHON_*|CLASHLENS_HMAC_*|CLASHLENS_INTERACTIVE_API_KEY_FILE|CLASHLENS_OFFICIAL_API_ORIGIN|CLASHLENS_OFFICIAL_API_PROXY_URL|CLASHLENS_OFFICIAL_KEY_FILE|CLASHLENS_OFFICIAL_PROXY_URL|CLASHLENS_API_HOST|CLASHLENS_API_PORT|CLASHLENS_SCHEMA_VERSION|CLASHLENS_SHARED_TRAFFIC_GATE_MODE|CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE|CLASHLENS_WORKER_LEASE_SECONDS|CLASHLENS_WORKER_REPLICAS|CLASHLENS_WORKER_CONCURRENCY|CLASHLENS_WORKER_DATABASE_POOL_SIZE|CLASHLENS_WORKER_ARCHIVE_POOL_SIZE|CLASHLENS_POSTGRES_MEMORY|CLASHLENS_POSTGRES_SHM_SIZE|CLASHLENS_POSTGRES_CPUS|CLASHLENS_POSTGRES_PIDS|CLASHLENS_COLLECTOR_MEMORY|CLASHLENS_COLLECTOR_CPUS|CLASHLENS_COLLECTOR_PIDS|CLASHLENS_API_MEMORY|CLASHLENS_API_CPUS|CLASHLENS_API_PIDS|CLASHLENS_WORKER_MEMORY|CLASHLENS_WORKER_CPUS|CLASHLENS_WORKER_PIDS)
        ;;
      CLASHLENS_*)
        result+=(--env "$name")
        ;;
    esac
  done

  result+=(--env "CLASHLENS_OFFICIAL_API_ORIGIN=$CLASHLENS_OFFICIAL_API_ORIGIN")
  result+=(--env "CLASHLENS_OFFICIAL_API_PROXY_URL=$CLASHLENS_OFFICIAL_API_PROXY_URL")
  result+=(--env "CLASHLENS_NORMAL_API_KEY_FILES=$CLASHLENS_NORMAL_API_KEY_FILES")
  result+=(--env "CLASHLENS_INTERACTIVE_API_KEY_FILES=$CLASHLENS_INTERACTIVE_API_KEY_FILES")
  result+=(--env "CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE=$CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE")
  result+=(--env "CLASHLENS_HEALTH_LISTEN=0.0.0.0:8081")
  result+=(--env "CLASHLENS_SPOOL_ROOT=/spool")
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
    create_secret_from_file "$secret_name" "$source"
    result+=(--secret "$secret_name,type=mount,target=$path,uid=10001,gid=10001,mode=0400")
  done
}

# Both collector modes need the collector's read-write archive credentials.
# The worker's read-only archive pair never reaches either collector.
collector_archive_secret_args() {
  local -n secret_result=$1
  local -n env_result=$2

  replace_secret_value clashlens-collector-archive-access-key "$CLASHLENS_ARCHIVE_ACCESS_KEY"
  replace_secret_value clashlens-collector-archive-secret-key "$CLASHLENS_ARCHIVE_SECRET_KEY"
  secret_result+=(--secret "clashlens-collector-archive-access-key,type=mount,target=/run/secrets/archive-access-key,uid=10001,gid=10001,mode=0400")
  secret_result+=(--secret "clashlens-collector-archive-secret-key,type=mount,target=/run/secrets/archive-secret-key,uid=10001,gid=10001,mode=0400")
  env_result+=(--env "CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key")
  env_result+=(--env "CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key")
}

# The required collector additionally uses its least-privilege database role.
collector_credential_secret_args() {
  local -n secret_result=$1
  local -n env_result=$2

  replace_secret_value clashlens-collector-database-url "$(role_database_url "$COLLECTOR_ROLE" "$CLASHLENS_COLLECTOR_DB_PASSWORD")"
  secret_result+=(--secret "clashlens-collector-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400")
  env_result+=(--env "CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url")
  collector_archive_secret_args "$1" "$2"
}

# The bridge accepts contract version 1 only and therefore receives the admin
# URL. It is replaced and its admin secret is removed immediately after the
# contract advances, so no long-lived runtime container holds the admin URL.
collector_run() {
  ensure_spool_root
  local container=$1
  local schema_version=$2
  local traffic_mode=$3
  local enable_global_rankings=$4
  local -a secrets=() env_args=()

  collector_secret_args secrets "$CLASHLENS_NORMAL_API_KEY_FILES"
  collector_secret_args secrets "$CLASHLENS_INTERACTIVE_API_KEY_FILES"
  collector_env_args env_args
  if [[ "$container" == "$COLLECTOR_BRIDGE_CONTAINER" ]]; then
    replace_secret_value clashlens-bridge-database-url "$(admin_database_url)"
    secrets+=(--secret "clashlens-bridge-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400")
    env_args+=(--env "CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url")
    collector_archive_secret_args secrets env_args
  else
    collector_credential_secret_args secrets env_args
    env_args+=(--env "CLASHLENS_ENABLE_GLOBAL_RANKINGS=$enable_global_rankings")
  fi
  env_args+=(--env "CLASHLENS_SCHEMA_VERSION=$schema_version")
  env_args+=(--env "CLASHLENS_SHARED_TRAFFIC_GATE_MODE=$traffic_mode")

  if container_exists "$container"; then
    stop_and_remove "$container" "$COLLECTOR_STOP_GRACE"
  fi

  "$PODMAN_BIN" run \
    --detach \
    --name "$container" \
    --network "$NETWORK_NAME" \
    "${env_args[@]}" \
    --volume "$CLASHLENS_SPOOL_ROOT:/spool:rw,z" \
    --publish "$CLASHLENS_HEALTH_HOST:$CLASHLENS_HEALTH_PORT:8081/tcp" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --cap-drop all \
    --security-opt no-new-privileges \
    --memory "$CLASHLENS_COLLECTOR_MEMORY" \
    --pids-limit "$CLASHLENS_COLLECTOR_PIDS" \
    --cpus "$CLASHLENS_COLLECTOR_CPUS" \
    --health-cmd "wget -qO- http://127.0.0.1:8081/readyz | grep -q '\"ready\":true'" \
    --health-interval 30s \
    --health-timeout 10s \
    --health-retries 3 \
    --restart unless-stopped \
    --label org.clashlens.component=collector \
    "${secrets[@]}" \
    "$COLLECTOR_IMAGE" run --role both >/dev/null
}

start_bridge_collector() {
  collector_run "$COLLECTOR_BRIDGE_CONTAINER" 1 bridge false
}

start_required_collector() {
  ensure_spool_root
  collector_run "$COLLECTOR_CONTAINER" "$(runtime_contract_version)" required false
}

enable_global_rankings() {
  # A worker-only rollback may intentionally run without a local collector image.
  image_exists || return 1
  ensure_spool_root
  collector_run "$COLLECTOR_CONTAINER" "$(runtime_contract_version)" required true
  wait_for_collector
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
}

require_python_runtime() {
  "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1 || \
    die "private network is missing; run up first"
  container_running "$POSTGRES_CONTAINER" || die "PostgreSQL is not running; run up first"

  local version
  version=$(contract_version)
  [[ "$version" == "$(runtime_contract_version)" ]] || die "production contract version $(runtime_contract_version) is required (found $version)"
  require_current_schema

  python_image_exists || die "python image is missing; run python-up first"
}

# The private API receives only its database role, its HMAC caller proof
# files, one interactive official key file, and the fixed-egress proxy URL.
api_secret_args() {
  local -n secret_result=$1
  local -n env_result=$2

  replace_secret_value clashlens-python-api-database-url "$(role_database_url "$API_ROLE" "$CLASHLENS_API_DB_PASSWORD")"
  create_secret_from_file clashlens-python-api-hmac-current \
    "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_HMAC_SECRET_FILE##*/}"
  create_secret_from_file clashlens-python-api-interactive-key \
    "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_INTERACTIVE_API_KEY_FILE##*/}"
  secret_result+=(--secret "clashlens-python-api-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400")
  secret_result+=(--secret "clashlens-python-api-hmac-current,type=mount,target=$CLASHLENS_HMAC_SECRET_FILE,uid=10001,gid=10001,mode=0400")
  secret_result+=(--secret "clashlens-python-api-interactive-key,type=mount,target=$CLASHLENS_INTERACTIVE_API_KEY_FILE,uid=10001,gid=10001,mode=0400")
  env_result+=(--env "CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url")
  env_result+=(--env "CLASHLENS_HMAC_SECRET_FILE=$CLASHLENS_HMAC_SECRET_FILE")
  # The CLI serve seam reads the interactive official key through the
  # official-key-file setting and the fixed-egress proxy through the
  # official-proxy-url setting. The deployment keeps secret values out of
  # command arguments; only the in-container file path is an environment
  # value, and the proxy URL is non-secret deployment metadata.
  env_result+=(--env "CLASHLENS_OFFICIAL_KEY_FILE=$CLASHLENS_INTERACTIVE_API_KEY_FILE")
  env_result+=(--env "CLASHLENS_OFFICIAL_PROXY_URL=$CLASHLENS_OFFICIAL_API_PROXY_URL")
  if [[ -n "${CLASHLENS_HMAC_PREVIOUS_SECRET_FILE:-}" ]]; then
    create_secret_from_file clashlens-python-api-hmac-previous \
      "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_HMAC_PREVIOUS_SECRET_FILE##*/}"
    secret_result+=(--secret "clashlens-python-api-hmac-previous,type=mount,target=$CLASHLENS_HMAC_PREVIOUS_SECRET_FILE,uid=10001,gid=10001,mode=0400")
    env_result+=(--env "CLASHLENS_HMAC_PREVIOUS_SECRET_FILE=$CLASHLENS_HMAC_PREVIOUS_SECRET_FILE")
    env_result+=(--env "CLASHLENS_HMAC_PREVIOUS_KEY_ID=$CLASHLENS_HMAC_PREVIOUS_KEY_ID")
  fi
}

start_python_api() {
  local -a secrets=() env_args=()
  api_secret_args secrets env_args

  if container_exists "$PYTHON_API_CONTAINER"; then
    stop_and_remove "$PYTHON_API_CONTAINER" "$API_STOP_GRACE"
  fi

  "$PODMAN_BIN" run \
    --detach \
    --name "$PYTHON_API_CONTAINER" \
    --network "$NETWORK_NAME" \
    --network-alias "$API_NETWORK_ALIAS" \
    "${env_args[@]}" \
    --env "CLASHLENS_API_HOST=0.0.0.0" \
    --env "CLASHLENS_API_PORT=$API_LISTEN_PORT" \
    --env "CLASHLENS_HMAC_CALLER=$CLASHLENS_HMAC_CALLER" \
    --env "CLASHLENS_HMAC_KEY_ID=$CLASHLENS_HMAC_KEY_ID" \
    --env "CLASHLENS_OFFICIAL_PROXY_URL=$CLASHLENS_OFFICIAL_API_PROXY_URL" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --cap-drop all \
    --security-opt no-new-privileges \
    --memory "$CLASHLENS_API_MEMORY" \
    --pids-limit "$CLASHLENS_API_PIDS" \
    --cpus "$CLASHLENS_API_CPUS" \
    --health-cmd "python -m clashlens.cli probe --url http://127.0.0.1:$API_LISTEN_PORT/readyz --secret-file $CLASHLENS_HMAC_SECRET_FILE --timeout-seconds 3 >/dev/null 2>&1" \
    --health-interval 30s \
    --health-timeout 20s \
    --health-retries 3 \
    --restart unless-stopped \
    --label org.clashlens.component=python-api \
    "${secrets[@]}" \
    "$PYTHON_IMAGE" serve --host 0.0.0.0 --port "$API_LISTEN_PORT" >/dev/null
}

wait_for_python_api() {
  local attempt
  for ((attempt = 1; attempt <= 60; attempt++)); do
    if container_running "$PYTHON_API_CONTAINER" && \
      "$PODMAN_BIN" healthcheck run "$PYTHON_API_CONTAINER" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  die "private Python API did not become healthy"
}

# Phase 1 browser login needs the browser session key and both provider
# client secrets as protected host files. A partially configured login must
# fail before any container change; with no login settings the website starts
# with login disabled and public data stays available. File contents are
# validated here too, so a wrong secret fails closed before containers move.
validate_website_login_config() {
  local -a required=(
    CLASHLENS_LOGIN_SECRET_FILE
    CLASHLENS_GOOGLE_CLIENT_ID
    CLASHLENS_GOOGLE_CLIENT_SECRET_FILE
    CLASHLENS_DISCORD_CLIENT_ID
    CLASHLENS_DISCORD_CLIENT_SECRET_FILE
    CLASHLENS_PUBLIC_ORIGIN
  )
  local configured=0 name value source
  for name in "${required[@]}"; do
    [[ -n "${!name:-}" ]] && configured=$((configured + 1))
  done
  (( configured == 0 )) && return 1
  if (( configured != ${#required[@]} )); then
    die "website login configuration is incomplete: set every login setting in app.env or none"
  fi
  for name in "${required[@]}"; do
    not_placeholder "$name"
  done
  case "$CLASHLENS_PUBLIC_ORIGIN" in
    https://*) ;;
    *)
      die "CLASHLENS_PUBLIC_ORIGIN must use https in production deployment"
      ;;
  esac
  # The exact scheme+host[:port] origin only: no path, query, or fragment.
  [[ "$CLASHLENS_PUBLIC_ORIGIN" =~ ^https://[A-Za-z0-9._~-]+(:[0-9]+)?$ ]] || \
    die "CLASHLENS_PUBLIC_ORIGIN must be the exact https origin without a path"
  [[ "$CLASHLENS_GOOGLE_CLIENT_ID" =~ ^[A-Za-z0-9._~-]{1,256}$ ]] || \
    die "CLASHLENS_GOOGLE_CLIENT_ID is malformed"
  [[ "$CLASHLENS_DISCORD_CLIENT_ID" =~ ^[0-9]{17,20}$ ]] || \
    die "CLASHLENS_DISCORD_CLIENT_ID is malformed (expected a numeric Discord application ID)"
  for name in CLASHLENS_LOGIN_SECRET_FILE CLASHLENS_GOOGLE_CLIENT_SECRET_FILE CLASHLENS_DISCORD_CLIENT_SECRET_FILE; do
    validate_single_key_file "$name"
  done
  validate_login_secret_content CLASHLENS_LOGIN_SECRET_FILE
  for name in CLASHLENS_GOOGLE_CLIENT_SECRET_FILE CLASHLENS_DISCORD_CLIENT_SECRET_FILE; do
    validate_provider_secret_content "$name"
  done
  return 0
}

# The browser session key must decode to exactly 32 bytes, mirroring the
# website's strict one-unpadded-base64url-value requirement.
validate_login_secret_content() {
  local name=$1
  local path=${!name}
  local source="$CLASHLENS_API_KEY_HOST_DIR/${path##*/}"
  local raw decoded
  raw=$(cat -- "$source") || die "cannot read $name"
  raw=${raw%$'\r'}
  raw=${raw%$'\n'}
  [[ "$raw" =~ ^[A-Za-z0-9_-]{43}$ ]] || \
    die "$name must contain exactly one unpadded base64url value"
  decoded=$(printf '%s' "$raw" | tr '_-' '/+' | base64 -d 2>/dev/null | wc -c)
  [[ "$decoded" == "32" ]] || \
    die "$name must decode to exactly 32 bytes"
}

# Provider client secrets must satisfy the website's bounded non-placeholder
# requirements: 1-512 printable ASCII characters with no whitespace or
# control bytes.
validate_provider_secret_content() {
  local name=$1
  local path=${!name}
  local source="$CLASHLENS_API_KEY_HOST_DIR/${path##*/}"
  local raw
  raw=$(cat -- "$source") || die "cannot read $name"
  raw=${raw%$'\r'}
  raw=${raw%$'\n'}
  case "$raw" in
    CHANGE_ME|REPLACE_ME|replace-me|change-me)
      die "$name must contain a deployment value"
      ;;
  esac
  [[ "$raw" =~ ^[[:graph:]]{1,512}$ ]] || \
    die "$name must contain 1-512 printable characters with no whitespace or control characters"
}

website_login_secret_args() {
  local -n login_secret_result=$1
  local -n login_env_result=$2

  create_secret_from_file clashlens-login-secret-current "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_LOGIN_SECRET_FILE##*/}"
  create_secret_from_file clashlens-google-client-secret "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_GOOGLE_CLIENT_SECRET_FILE##*/}"
  create_secret_from_file clashlens-discord-client-secret "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_DISCORD_CLIENT_SECRET_FILE##*/}"
  login_secret_result+=(
    --secret "clashlens-login-secret-current,type=mount,target=/run/secrets/clashlens-login-secret,uid=1000,gid=1000,mode=0400"
    --secret "clashlens-google-client-secret,type=mount,target=/run/secrets/clashlens-google-client-secret,uid=1000,gid=1000,mode=0400"
    --secret "clashlens-discord-client-secret,type=mount,target=/run/secrets/clashlens-discord-client-secret,uid=1000,gid=1000,mode=0400"
  )
  login_env_result+=(
    --env CLASHLENS_LOGIN_ENABLED=true
    --env "CLASHLENS_PUBLIC_ORIGIN=$CLASHLENS_PUBLIC_ORIGIN"
    --env "CLASHLENS_GOOGLE_CLIENT_ID=$CLASHLENS_GOOGLE_CLIENT_ID"
    --env CLASHLENS_LOGIN_SECRET_FILE=/run/secrets/clashlens-login-secret
    --env CLASHLENS_GOOGLE_CLIENT_SECRET_FILE=/run/secrets/clashlens-google-client-secret
    --env "CLASHLENS_DISCORD_CLIENT_ID=$CLASHLENS_DISCORD_CLIENT_ID"
    --env CLASHLENS_DISCORD_CLIENT_SECRET_FILE=/run/secrets/clashlens-discord-client-secret
  )
}

require_website_runtime() {
  "$PODMAN_BIN" network exists "$NETWORK_NAME" >/dev/null 2>&1 || die "private network is missing; run up first"
  local version
  version=$(contract_version)
  [[ "$version" == "$(runtime_contract_version)" ]] || die "production contract version $(runtime_contract_version) is required (found $version)"
  container_running "$PYTHON_API_CONTAINER" || die "private Python API is not running; run python-up first"
  "$PODMAN_BIN" healthcheck run "$PYTHON_API_CONTAINER" >/dev/null 2>&1 || die "private Python API is not healthy; run python-up first"
  website_image_exists || die "website image is missing; run website-up first"
}

start_website() {
  local -a login_secrets=() login_env=()
  if validate_website_login_config; then
    website_login_secret_args login_secrets login_env
  fi
  create_secret_from_file clashlens-python-api-hmac-current "$CLASHLENS_API_KEY_HOST_DIR/${CLASHLENS_HMAC_SECRET_FILE##*/}"
  if container_exists "$WEBSITE_CONTAINER"; then
    stop_and_remove "$WEBSITE_CONTAINER" "$API_STOP_GRACE"
  fi
  "$PODMAN_BIN" run \
    --detach --name "$WEBSITE_CONTAINER" --network "$NETWORK_NAME" \
    --publish "$CLASHLENS_WEBSITE_HOST:$CLASHLENS_WEBSITE_PORT:3000/tcp" \
    --env NODE_ENV=production --env CLASHLENS_PYTHON_API_URL=http://python-api:8000 \
    --env "CLASHLENS_PYTHON_HMAC_CALLER=$CLASHLENS_HMAC_CALLER" \
    --env "CLASHLENS_PYTHON_HMAC_KEY_ID=$CLASHLENS_HMAC_KEY_ID" \
    --env CLASHLENS_PYTHON_HMAC_SECRET_FILE=/run/secrets/clashlens-python-hmac \
    --env CLASHLENS_TRUST_PROXY=false \
    --secret clashlens-python-api-hmac-current,type=mount,target=/run/secrets/clashlens-python-hmac,uid=1000,gid=1000,mode=0400 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m --cap-drop all --security-opt no-new-privileges \
    --memory "$CLASHLENS_WEBSITE_MEMORY" --pids-limit "$CLASHLENS_WEBSITE_PIDS" --cpus "$CLASHLENS_WEBSITE_CPUS" \
    --health-cmd "node -e \"require('http').get('http://127.0.0.1:3000/healthz', r => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))\"" \
    --health-interval 10s --health-timeout 3s --health-retries 12 --restart unless-stopped \
    "${login_secrets[@]}" "${login_env[@]}" \
    --label org.clashlens.component=website "$WEBSITE_IMAGE" >/dev/null
}

wait_for_website() {
  local attempt
  for ((attempt = 1; attempt <= 60; attempt++)); do
    if container_running "$WEBSITE_CONTAINER" && "$PODMAN_BIN" healthcheck run "$WEBSITE_CONTAINER" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  die "website did not become healthy"
}

website_start() {
  require_website_runtime
  validate_single_key_file CLASHLENS_HMAC_SECRET_FILE
  start_website
  wait_for_website
  printf 'website is healthy at http://%s:%s/healthz\n' "$CLASHLENS_WEBSITE_HOST" "$CLASHLENS_WEBSITE_PORT"
}

# The worker receives its own database role and a read-only archive
# credential. It never receives official API keys, HMAC keys, or the API role.
# All replicas share the same three file-backed secrets.
worker_secret_args() {
  local -n secret_result=$1
  local -n env_result=$2

  replace_secret_value clashlens-python-worker-database-url "$(role_database_url "$WORKER_ROLE" "$CLASHLENS_WORKER_DB_PASSWORD")"
  replace_secret_value clashlens-python-worker-archive-access-key "$CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY"
  replace_secret_value clashlens-python-worker-archive-secret-key "$CLASHLENS_WORKER_ARCHIVE_SECRET_KEY"
  secret_result+=(--secret "clashlens-python-worker-database-url,type=mount,target=/run/secrets/database-url,uid=10001,gid=10001,mode=0400")
  secret_result+=(--secret "clashlens-python-worker-archive-access-key,type=mount,target=/run/secrets/archive-access-key,uid=10001,gid=10001,mode=0400")
  secret_result+=(--secret "clashlens-python-worker-archive-secret-key,type=mount,target=/run/secrets/archive-secret-key,uid=10001,gid=10001,mode=0400")
  env_result+=(--env "CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url")
  env_result+=(--env "CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key")
  env_result+=(--env "CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key")
}

# Stop and remove every worker container: the configured replicas, any
# surplus replica left from a previous higher count, and the legacy
# pre-replica container name. Start and down commands must stop all of them
# before the shared worker secrets are replaced, so no running container
# mounts a replaced secret.
stop_all_worker_containers() {
  local i
  for ((i = 1; i <= WORKER_REPLICA_MAX; i++)); do
    if container_exists "${PYTHON_WORKER_CONTAINER}-${i}"; then
      stop_and_remove "${PYTHON_WORKER_CONTAINER}-${i}" "$WORKER_STOP_GRACE"
    fi
  done
  if container_exists "$PYTHON_WORKER_CONTAINER"; then
    stop_and_remove "$PYTHON_WORKER_CONTAINER" "$WORKER_STOP_GRACE"
  fi
}

start_python_workers() {
  # All replicas share one queue, one database role, and one read-only
  # archive pair. Stop every existing worker container first, then replace
  # the shared secrets once, then start the configured identical replicas.
  stop_all_worker_containers

  local -a secrets=() env_args=()
  worker_secret_args secrets env_args

  local i
  for ((i = 1; i <= CLASHLENS_WORKER_REPLICAS; i++)); do
    "$PODMAN_BIN" run \
      --detach \
      --name "${PYTHON_WORKER_CONTAINER}-${i}" \
      --network "$NETWORK_NAME" \
      "${env_args[@]}" \
      --volume "$CLASHLENS_SPOOL_ROOT:/spool:rw,z" \
      --env CLASHLENS_ARCHIVE_ENDPOINT \
      --env CLASHLENS_ARCHIVE_REGION \
      --env CLASHLENS_ARCHIVE_BUCKET \
      --env CLASHLENS_ARCHIVE_INSTANCE_ID \
      --env CLASHLENS_ARCHIVE_MARKER_KEY \
      --env CLASHLENS_ARCHIVE_MARKER_HASH \
      --env CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION \
      --env CLASHLENS_SPOOL_ROOT=/spool \
      --env CLASHLENS_MAX_BODY_BYTES \
      --env CLASHLENS_SPOOL_MAX_BYTES \
      --env CLASHLENS_SPOOL_MAX_OBJECTS \
      --env CLASHLENS_SPOOL_FREE_SPACE_FLOOR \
      --env CLASHLENS_SPOOL_FREE_INODE_FLOOR \
      --read-only \
      --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
      --cap-drop all \
      --security-opt no-new-privileges \
      --memory "$CLASHLENS_WORKER_MEMORY" \
      --pids-limit "$CLASHLENS_WORKER_PIDS" \
      --cpus "$CLASHLENS_WORKER_CPUS" \
      --health-cmd "python -m clashlens.cli ready --expected-contract-version $(runtime_contract_version)" \
      --health-interval 30s \
      --health-timeout 20s \
      --health-retries 3 \
      --restart unless-stopped \
      --label org.clashlens.component=python-worker \
      "${secrets[@]}" \
      "$PYTHON_IMAGE" worker --owner "production-python-${i}" --max-jobs 100 --lease-seconds "$CLASHLENS_WORKER_LEASE_SECONDS" --concurrency "$CLASHLENS_WORKER_CONCURRENCY" --database-pool-size "$CLASHLENS_WORKER_DATABASE_POOL_SIZE" --archive-pool-size "$CLASHLENS_WORKER_ARCHIVE_POOL_SIZE" --operating-snapshot-file /tmp/clashlens-worker-operating.json --run-forever >/dev/null
  done
}

wait_for_python_workers() {
  local attempt i
  for ((attempt = 1; attempt <= 60; attempt++)); do
    local all_healthy=true
    for ((i = 1; i <= CLASHLENS_WORKER_REPLICAS; i++)); do
      if ! container_running "${PYTHON_WORKER_CONTAINER}-${i}" || \
        ! "$PODMAN_BIN" healthcheck run "${PYTHON_WORKER_CONTAINER}-${i}" >/dev/null 2>&1; then
        all_healthy=false
        break
      fi
    done
    if [[ "$all_healthy" == true ]]; then
      return
    fi
    sleep 1
  done
  die "production Python worker replicas did not become healthy"
}

python_start() {
  require_python_runtime
  start_python_api
  wait_for_python_api
  start_python_workers
  wait_for_python_workers
  if enable_global_rankings; then
    printf 'production Python API and %s worker replicas are healthy; Global Top-200 is enabled\n' "$CLASHLENS_WORKER_REPLICAS"
  else
    printf 'production Python API and %s worker replicas are healthy; Global Top-200 enablement was skipped because the collector image is absent\n' "$CLASHLENS_WORKER_REPLICAS"
  fi
}

status_of_container() {
  local label=$1
  local name=$2
  if ! container_exists "$name"; then
    printf '%s: absent\n' "$label"
  elif container_running "$name"; then
    local health
    health=$("$PODMAN_BIN" container inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || true)
    if [[ -n "$health" && "$health" != "unknown" && "$health" != "<nil>" ]]; then
      printf '%s: running (%s)\n' "$label" "$health"
    else
      printf '%s: running\n' "$label"
    fi
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
  status_of_container python-api "$PYTHON_API_CONTAINER"
  local i
  for ((i = 1; i <= CLASHLENS_WORKER_REPLICAS; i++)); do
    status_of_container "python-worker-$i" "${PYTHON_WORKER_CONTAINER}-${i}"
  done
  if container_exists "$PYTHON_WORKER_CONTAINER"; then
    printf 'python-worker: stale (removed on next start)\n'
  fi
  for ((i = CLASHLENS_WORKER_REPLICAS + 1; i <= WORKER_REPLICA_MAX; i++)); do
    if container_exists "${PYTHON_WORKER_CONTAINER}-${i}"; then
      printf 'python-worker-%s: surplus (removed on next start)\n' "$i"
    fi
  done
  status_of_container website "$WEBSITE_CONTAINER"
  if container_running "${PYTHON_WORKER_CONTAINER}-1"; then
    if queue_output=$("$PODMAN_BIN" exec "${PYTHON_WORKER_CONTAINER}-1" python -m clashlens.cli queue-status 2>/dev/null); then
      printf 'python queue: %s\n' "$(printf '%s' "$queue_output" | head -n 1)"
    else
      printf 'python queue: unavailable\n'
    fi
  else
    printf 'python queue: worker not running\n'
  fi
}

# Graceful lifecycle stop: SIGTERM with a grace period, then removal. The
# worker grace is the configured job lease plus margin so a leased job can
# drain, and stays below the systemd TimeoutStopSec stop timeout.
stop_and_remove() {
  local name=$1
  local grace=$2
  if container_exists "$name"; then
    if container_running "$name"; then
      "$PODMAN_BIN" stop --time "$grace" "$name" >/dev/null
    fi
    "$PODMAN_BIN" rm "$name" >/dev/null
  fi
}

run_collector_command() {
  container_exists "$COLLECTOR_CONTAINER" || die "collector container does not exist; run up first"
  container_running "$COLLECTOR_CONTAINER" || die "collector container is not running"
  "$PODMAN_BIN" exec "$COLLECTOR_CONTAINER" /usr/local/bin/collector "$@"
}

run_operating_check() {
  local -a previous_args=()
  if [[ $# == 2 && "$1" == "--previous-snapshot" && -n "$2" ]]; then
    previous_args=(--previous-snapshot "$2")
  elif [[ $# != 0 ]]; then
    die "operating-check accepts only --previous-snapshot <path>"
  fi
  env "PYTHONPATH=$ROOT_DIR/python/src" "$PYTHON_BIN" \
    "$ROOT_DIR/scripts/operating_check.py" \
    --podman-bin "$PODMAN_BIN" \
    --curl-bin "$CURL_BIN" \
    --postgres-container "$POSTGRES_CONTAINER" \
    --postgres-user "$POSTGRES_USER" \
    --postgres-database "$POSTGRES_DB" \
    --collector-metrics-url "http://$HEALTH_HOST:$HEALTH_PORT/metrics" \
    --python-api-container "$PYTHON_API_CONTAINER" \
    --api-hmac-caller "$CLASHLENS_HMAC_CALLER" \
    --api-hmac-key-id "$CLASHLENS_HMAC_KEY_ID" \
    --api-hmac-secret-file "$CLASHLENS_HMAC_SECRET_FILE" \
    --python-worker-container "$PYTHON_WORKER_CONTAINER" \
    --worker-replicas "$CLASHLENS_WORKER_REPLICAS" \
    --spool-max-body-bytes "$CLASHLENS_MAX_BODY_BYTES" \
    --spool-max-bytes "$CLASHLENS_SPOOL_MAX_BYTES" \
    --spool-max-objects "$CLASHLENS_SPOOL_MAX_OBJECTS" \
    --spool-free-space-floor "$CLASHLENS_SPOOL_FREE_SPACE_FLOOR" \
    --spool-free-inode-floor "$CLASHLENS_SPOOL_FREE_INODE_FLOOR" \
    "${previous_args[@]}"
}

command=${1:-}
if [[ -z "$command" ]]; then
  usage
  exit 2
fi
shift

full_configuration=false
case "$command" in
  init|up|restart|build-collector|build-python|build-website|candidate-prepare|deployment-receipt|operating-check|python-up|python-start|api-start|worker-start|website-up|website-start)
    load_env_file
    full_configuration=true
    ;;
  status|logs|down|stack-down|enqueue|maintenance|queue-status|support-recovery-exec|python-down|api-down|worker-down|website-down)
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
PYTHON_API_CONTAINER=${CLASHLENS_PYTHON_API_CONTAINER:-$PYTHON_API_CONTAINER}
WEBSITE_CONTAINER=${CLASHLENS_WEBSITE_CONTAINER:-$WEBSITE_CONTAINER}
POSTGRES_IMAGE=${CLASHLENS_POSTGRES_IMAGE:-$POSTGRES_IMAGE}
COLLECTOR_IMAGE=${CLASHLENS_COLLECTOR_IMAGE:-$COLLECTOR_IMAGE}
PYTHON_IMAGE=${CLASHLENS_PYTHON_IMAGE:-$PYTHON_IMAGE}
WEBSITE_IMAGE=${CLASHLENS_WEBSITE_IMAGE:-$WEBSITE_IMAGE}
CLASHLENS_ARCHIVE_SECURE=${CLASHLENS_ARCHIVE_SECURE:-true}
CLASHLENS_HEALTH_HOST=${CLASHLENS_HEALTH_HOST:-$HEALTH_HOST}
CLASHLENS_HEALTH_PORT=${CLASHLENS_HEALTH_PORT:-$HEALTH_PORT}
CLASHLENS_WORKER_LEASE_SECONDS=${CLASHLENS_WORKER_LEASE_SECONDS:-60}
CLASHLENS_WORKER_REPLICAS=${CLASHLENS_WORKER_REPLICAS:-1}
CLASHLENS_WORKER_CONCURRENCY=${CLASHLENS_WORKER_CONCURRENCY:-1}
CLASHLENS_WORKER_DATABASE_POOL_SIZE=${CLASHLENS_WORKER_DATABASE_POOL_SIZE:-4}
CLASHLENS_WORKER_ARCHIVE_POOL_SIZE=${CLASHLENS_WORKER_ARCHIVE_POOL_SIZE:-4}
CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE=${CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE:-16}
CLASHLENS_SPOOL_ROOT=${CLASHLENS_SPOOL_ROOT:-/tmp/clashlens-spool}
CLASHLENS_SPOOL_MAX_BYTES=${CLASHLENS_SPOOL_MAX_BYTES:-17179869184}
CLASHLENS_SPOOL_MAX_OBJECTS=${CLASHLENS_SPOOL_MAX_OBJECTS:-1000000}
CLASHLENS_SPOOL_FREE_SPACE_FLOOR=${CLASHLENS_SPOOL_FREE_SPACE_FLOOR:-1073741824}
CLASHLENS_SPOOL_FREE_INODE_FLOOR=${CLASHLENS_SPOOL_FREE_INODE_FLOOR:-10000}
CLASHLENS_MAX_BODY_BYTES=${CLASHLENS_MAX_BODY_BYTES:-4194304}
HEALTH_HOST=$CLASHLENS_HEALTH_HOST
HEALTH_PORT=$CLASHLENS_HEALTH_PORT
WORKER_STOP_GRACE=$((CLASHLENS_WORKER_LEASE_SECONDS + 10))

if [[ "$full_configuration" == "true" ]]; then
  validate_common_settings
fi

case "$command" in
  init)
    [[ $# == 0 ]] || die "init accepts no arguments"
    initialize_runtime
    version=$(contract_version)
    case "$version" in
      absent)
        apply_initial_contract
        printf 'database initialized; data volume is %s\n' "$POSTGRES_VOLUME"
        ;;
      1|2|3|4|5)
        die "database is already initialized at contract version $version; use up or restart"
        ;;
      *)
        die "unsupported contract version $version"
        ;;
    esac
    ;;
  up)
    [[ $# == 0 ]] || die "up accepts no arguments"
    validate_key_files "$CLASHLENS_NORMAL_API_KEY_FILES"
    validate_key_files "$CLASHLENS_INTERACTIVE_API_KEY_FILES"
    validate_single_key_file CLASHLENS_HMAC_SECRET_FILE
    validate_single_key_file CLASHLENS_INTERACTIVE_API_KEY_FILE
    if [[ -n "${CLASHLENS_HMAC_PREVIOUS_SECRET_FILE:-}" ]]; then
      validate_single_key_file CLASHLENS_HMAC_PREVIOUS_SECRET_FILE
    fi
    build_collector_image
    initialize_runtime
    version=$(contract_version)
    fresh_bootstrap=false
    case "$version" in
      absent)
        apply_initial_contract
        apply_pending_forward_migrations
        configure_runtime_roles
        version=2
        fresh_bootstrap=true
      ;;
      1|2|3|4|5) ;;
      *)
        die "unsupported contract version $version"
        ;;
    esac
    if [[ "$version" == "1" ]]; then
      # A deployed v1 collector already owns the health port. Stop it before
      # the contract-v1 bridge starts, or the bridge cannot bind that port.
      # Drain old Python workers too: the rebuilt bridge labels new source
      # work parser-v2, which the previous worker must never claim.
      stop_and_remove "$COLLECTOR_CONTAINER" "$COLLECTOR_STOP_GRACE"
      stop_all_worker_containers
      start_bridge_collector
      wait_for_collector
      apply_pending_forward_migrations
      ensure_archive_instance_contract
      configure_runtime_roles
      stop_and_remove "$COLLECTOR_BRIDGE_CONTAINER" "$COLLECTOR_STOP_GRACE"
      secret_rm clashlens-bridge-database-url
    elif [[ ("$version" == "2" || "$version" == "3" || "$version" == "4" || "$version" == "5") && "$fresh_bootstrap" != true ]]; then
      # Migrations 0007, 0009, 0010, 0011, 0012, and 0013 change claim or
      # publication contracts. Stop every old worker before applying any.
      if ! schema_migration_applied 7 || ([[ -n "${CLASHLENS_ARCHIVE_INSTANCE_ID:-}" ]] && ! schema_migration_applied 9) || ! schema_migration_applied 10 || ! schema_migration_applied 11 || ! schema_migration_applied 12 || ! schema_migration_applied 13; then
        stop_and_remove "$COLLECTOR_CONTAINER" "$COLLECTOR_STOP_GRACE"
        stop_all_worker_containers
      fi
      apply_pending_forward_migrations
      ensure_archive_instance_contract
      configure_runtime_roles
    fi
    ensure_archive_instance_contract
    start_required_collector
    wait_for_collector
    printf 'collector is ready at http://%s:%s/readyz\n' "$HEALTH_HOST" "$HEALTH_PORT"
    ;;
  restart)
    [[ $# == 0 ]] || die "restart accepts no arguments"
    validate_key_files "$CLASHLENS_NORMAL_API_KEY_FILES"
    validate_key_files "$CLASHLENS_INTERACTIVE_API_KEY_FILES"
    validate_single_key_file CLASHLENS_HMAC_SECRET_FILE
    validate_single_key_file CLASHLENS_INTERACTIVE_API_KEY_FILE
    if [[ -n "${CLASHLENS_HMAC_PREVIOUS_SECRET_FILE:-}" ]]; then
      validate_single_key_file CLASHLENS_HMAC_PREVIOUS_SECRET_FILE
    fi
    require_podman
    require_rootless_podman
    ensure_network
    ensure_volume
    ensure_postgres
    wait_for_postgres
    version=$(contract_version)
    [[ "$version" == "$(runtime_contract_version)" ]] || die "restart requires contract version $(runtime_contract_version) (found $version); run up first"
    require_current_schema
    image_exists || die "collector image is missing; run up first"
    start_required_collector
    wait_for_collector
    printf 'collector restarted and is ready at http://%s:%s/readyz\n' "$HEALTH_HOST" "$HEALTH_PORT"
    ;;
  build-collector)
    [[ $# == 0 ]] || die "build-collector accepts no arguments"
    source_revision=$(source_revision_for_build)
    require_podman
    require_rootless_podman
    build_collector_image "$source_revision"
    ;;
  build-python)
    [[ $# == 0 ]] || die "build-python accepts no arguments"
    source_revision=$(source_revision_for_build)
    require_podman
    require_rootless_podman
    build_python_image "$source_revision"
    ;;
  build-website)
    [[ $# == 0 ]] || die "build-website accepts no arguments"
    source_revision=$(source_revision_for_build)
    require_podman
    require_rootless_podman
    build_website_image "$source_revision"
    ;;
  candidate-prepare)
    [[ $# == 0 ]] || die "candidate-prepare accepts no arguments"
    require_podman
    require_rootless_podman
    prepare_candidate_database
    ;;
  deployment-receipt)
    [[ $# == 3 ]] || die "deployment-receipt requires <scope> <environment> <results-dir>"
    require_podman
    require_rootless_podman
    write_deployment_receipt "$1" "$2" "$3"
    ;;
  operating-check)
    run_operating_check "$@"
    ;;
  python-up)
    [[ $# == 0 ]] || die "python-up accepts no arguments"
    source_revision=$(source_revision_for_build)
    require_podman
    require_rootless_podman
    build_python_image "$source_revision"
    python_start
    ;;
  python-start)
    [[ $# == 0 ]] || die "python-start accepts no arguments"
    require_podman
    require_rootless_podman
    python_start
    ;;
  api-start)
    [[ $# == 0 ]] || die "api-start accepts no arguments"
    require_podman
    require_rootless_podman
    require_python_runtime
    start_python_api
    wait_for_python_api
    printf 'private Python API is healthy\n'
    ;;
  worker-start)
    [[ $# == 0 ]] || die "worker-start accepts no arguments"
    require_podman
    require_rootless_podman
    require_python_runtime
    start_python_workers
    wait_for_python_workers
    if enable_global_rankings; then
      printf 'production Python worker replicas (%s) are healthy; Global Top-200 is enabled\n' "$CLASHLENS_WORKER_REPLICAS"
    else
      printf 'production Python worker replicas (%s) are healthy; Global Top-200 enablement was skipped because the collector image is absent\n' "$CLASHLENS_WORKER_REPLICAS"
    fi
    ;;
  website-up)
    [[ $# == 0 ]] || die "website-up accepts no arguments"
    source_revision=$(source_revision_for_build)
    require_podman
    require_rootless_podman
    # Reject malformed auth settings before building or replacing anything;
    # a fully absent auth block intentionally leaves public-only mode enabled.
    validate_website_login_config || true
    build_website_image "$source_revision"
    website_start
    ;;
  website-start)
    [[ $# == 0 ]] || die "website-start accepts no arguments"
    require_podman
    require_rootless_podman
    website_start
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
    elif [[ "$component" == "python-api" ]]; then
      target=$PYTHON_API_CONTAINER
    elif [[ "$component" == "python-worker" ]]; then
      target="${PYTHON_WORKER_CONTAINER}-1"
    elif [[ "$component" =~ ^python-worker-([0-9]+)$ ]]; then
      replica=${BASH_REMATCH[1]}
      (( replica >= 1 && replica <= WORKER_REPLICA_MAX )) || \
        die "logs target python-worker-N must be between 1 and $WORKER_REPLICA_MAX"
      target="${PYTHON_WORKER_CONTAINER}-${replica}"
    elif [[ "$component" == "website" ]]; then
      target=$WEBSITE_CONTAINER
    else
      die "logs target must be collector, postgres, python-api, python-worker, python-worker-N, or website"
    fi
    shift || true
    container_exists "$target" || die "$component container does not exist"
    "$PODMAN_BIN" logs "$@" "$target"
    ;;
  down)
    [[ $# == 0 ]] || die "down accepts no arguments"
    require_podman
    stop_and_remove "$WEBSITE_CONTAINER" "$API_STOP_GRACE"
    stop_all_worker_containers
    stop_and_remove "$PYTHON_API_CONTAINER" "$API_STOP_GRACE"
    stop_and_remove "$COLLECTOR_CONTAINER" "$COLLECTOR_STOP_GRACE"
    stop_and_remove "$COLLECTOR_BRIDGE_CONTAINER" "$COLLECTOR_STOP_GRACE"
    stop_and_remove "$POSTGRES_CONTAINER" "$POSTGRES_STOP_GRACE"
    secret_rm clashlens-bridge-database-url
    printf 'containers removed; network and data volume were kept\n'
    ;;
  stack-down)
    [[ $# == 0 ]] || die "stack-down accepts no arguments"
    require_podman
    stop_and_remove "$WEBSITE_CONTAINER" "$API_STOP_GRACE"
    stop_and_remove "$COLLECTOR_CONTAINER" "$COLLECTOR_STOP_GRACE"
    stop_and_remove "$COLLECTOR_BRIDGE_CONTAINER" "$COLLECTOR_STOP_GRACE"
    stop_and_remove "$POSTGRES_CONTAINER" "$POSTGRES_STOP_GRACE"
    secret_rm clashlens-bridge-database-url
    printf 'collector stack containers removed; network and data volume were kept\n'
    ;;
  python-down)
    [[ $# == 0 ]] || die "python-down accepts no arguments"
    require_podman
    stop_and_remove "$WEBSITE_CONTAINER" "$API_STOP_GRACE"
    stop_all_worker_containers
    stop_and_remove "$PYTHON_API_CONTAINER" "$API_STOP_GRACE"
    printf 'production Python containers removed\n'
    ;;
  api-down)
    [[ $# == 0 ]] || die "api-down accepts no arguments"
    require_podman
    stop_and_remove "$WEBSITE_CONTAINER" "$API_STOP_GRACE"
    stop_and_remove "$PYTHON_API_CONTAINER" "$API_STOP_GRACE"
    printf 'private Python API container removed\n'
    ;;
  worker-down)
    [[ $# == 0 ]] || die "worker-down accepts no arguments"
    require_podman
    stop_all_worker_containers
    printf 'production Python worker containers removed\n'
    ;;
  website-down)
    [[ $# == 0 ]] || die "website-down accepts no arguments"
    require_podman
    stop_and_remove "$WEBSITE_CONTAINER" "$API_STOP_GRACE"
    printf 'website container removed\n'
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
  queue-status)
    [[ $# == 0 ]] || die "queue-status accepts no arguments"
    require_podman
    container_exists "${PYTHON_WORKER_CONTAINER}-1" || die "python worker replica 1 does not exist; run python-up first"
    container_running "${PYTHON_WORKER_CONTAINER}-1" || die "python worker replica 1 is not running"
    "$PODMAN_BIN" exec "${PYTHON_WORKER_CONTAINER}-1" python -m clashlens.cli queue-status
    ;;
  support-recovery-exec)
    require_podman
    require_rootless_podman
    container_running "$PYTHON_API_CONTAINER" || die "private Python API is not running"
    "$PODMAN_BIN" exec --interactive "$PYTHON_API_CONTAINER" \
      /usr/local/bin/python -m clashlens.cli recover-discord "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
