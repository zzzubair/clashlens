#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WORK_DIR=$(mktemp -d)
trap 'rm -rf -- "$WORK_DIR"' EXIT

ENV_FILE="$WORK_DIR/app.env"
printf '%s\n' \
  'CLASHLENS_DATABASE_URL=postgresql://clashlens:do-not-print@postgres:5432/clashlens' \
  'CLASHLENS_ARCHIVE_ENDPOINT=archive.example.test' \
  'CLASHLENS_ARCHIVE_BUCKET=evidence' \
  'CLASHLENS_ARCHIVE_ACCESS_KEY=archive-access' \
  'CLASHLENS_ARCHIVE_SECRET_KEY=do-not-print' \
  'CLASHLENS_OFFICIAL_API_ORIGIN=https://api.clashofclans.com' \
  'CLASHLENS_NORMAL_API_KEY_FILES=normal-1=/run/secrets/normal-1' \
  'CLASHLENS_INTERACTIVE_API_KEY_FILES=interactive-1=/run/secrets/interactive-1' \
  >"$ENV_FILE"
chmod 0644 "$ENV_FILE"

if output=$(DEPLOY_ENV_FILE="$ENV_FILE" "$ROOT_DIR/deploy.sh" status 2>&1); then
  printf 'status accepted an app.env file with mode 0644\n' >&2
  exit 1
fi

if [[ "$output" != *"mode 600"* ]]; then
  printf 'status did not report the required app.env mode: %s\n' "$output" >&2
  exit 1
fi

if [[ "$output" == *"do-not-print"* ]]; then
  printf 'status printed a secret value\n' >&2
  exit 1
fi

printf 'ok: status rejects an unsafe app.env mode without printing values\n'

RECOVERY_BIN="$WORK_DIR/recovery-bin"
RECOVERY_ENV="$WORK_DIR/recovery.env"
mkdir -p "$RECOVERY_BIN"
cat >"$RECOVERY_BIN/podman" <<'EOF'
#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "network exists"|"volume exists"|"container exists") exit 1 ;;
esac
exit 0
EOF
chmod 0700 "$RECOVERY_BIN/podman"
printf '%s\n' 'CLASHLENS_ARCHIVE_BUCKET=incomplete-recovery-config' >"$RECOVERY_ENV"
chmod 0600 "$RECOVERY_ENV"
if ! output=$(DEPLOY_ENV_FILE="$RECOVERY_ENV" PODMAN_BIN="$RECOVERY_BIN/podman" "$ROOT_DIR/deploy.sh" status 2>&1); then
  printf 'status rejected an incomplete recovery configuration: %s\n' "$output" >&2
  exit 1
fi
[[ "$output" == *'collector: absent'* ]] || {
  printf 'status did not inspect containers with incomplete configuration\n' >&2
  exit 1
}
printf 'ok: status works without unrelated production settings\n'

MAINTENANCE_BIN="$WORK_DIR/maintenance-bin"
MAINTENANCE_LOG="$WORK_DIR/maintenance.log"
mkdir -p "$MAINTENANCE_BIN"
cat >"$MAINTENANCE_BIN/podman" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >>"$MAINTENANCE_LOG"
printf '\n' >>"$MAINTENANCE_LOG"
case "${1:-} ${2:-}" in
  "container exists") exit 0 ;;
  "container inspect") printf 'true\n' ;;
  "exec /usr/local/bin/collector") exit 0 ;;
esac
exit 0
EOF
chmod 0700 "$MAINTENANCE_BIN/podman"
MAINTENANCE_LOG="$MAINTENANCE_LOG" DEPLOY_ENV_FILE="$RECOVERY_ENV" PODMAN_BIN="$MAINTENANCE_BIN/podman" \
  "$ROOT_DIR/deploy.sh" maintenance list-failed --limit 20 >/dev/null
grep -q 'maintenance list-failed --limit 20' "$MAINTENANCE_LOG" || {
  printf 'maintenance did not reach the running collector with incomplete configuration\n' >&2
  exit 1
}
printf 'ok: maintenance works without unrelated production settings\n'

FAKE_BIN="$WORK_DIR/bin"
KEY_DIR="$WORK_DIR/keys"
PODMAN_LOG="$WORK_DIR/podman.log"
mkdir -p "$FAKE_BIN" "$KEY_DIR"

for name in normal-1 normal-2 normal-3 normal-4 interactive-1; do
  printf 'test-key-%s\n' "$name" >"$KEY_DIR/$name"
  chmod 0600 "$KEY_DIR/$name"
done

cat >"$FAKE_BIN/podman" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >>"$FAKE_PODMAN_LOG"
printf '\n' >>"$FAKE_PODMAN_LOG"
case "${1:-} ${2:-}" in
  "info --format") printf 'true\n' ;;
  "network exists") [[ "${FAKE_PRODUCTION_READY:-}" == "1" ]] ;;
  "volume exists"|"image exists") exit 1 ;;
  "container exists") [[ "${FAKE_PRODUCTION_READY:-}" == "1" ]] ;;
  "container inspect") printf 'true\n' ;;
  "healthcheck run") exit 0 ;;
  "exec --interactive") cat >/dev/null ;;
  "exec clashlens-postgres")
    if [[ "$*" == *"SELECT version FROM clash_lens_contract"* ]]; then
      printf '2\n'
    fi
    ;;
  "exec "*) exit 0 ;;
esac
EOF
chmod 0700 "$FAKE_BIN/podman"

cat >"$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
printf '{"ready":true}\n'
EOF
chmod 0700 "$FAKE_BIN/curl"

cat >"$ENV_FILE" <<EOF
POSTGRES_DB=clashlens
POSTGRES_USER=clashlens
POSTGRES_PASSWORD=test-db-password
CLASHLENS_DATABASE_URL=postgresql://clashlens:test-db-password@postgres:5432/clashlens?sslmode=disable
CLASHLENS_ARCHIVE_ENDPOINT=storage.googleapis.com
CLASHLENS_ARCHIVE_SECURE=true
CLASHLENS_ARCHIVE_BUCKET=clash-lens-test
CLASHLENS_ARCHIVE_ACCESS_KEY=test-archive-access
CLASHLENS_ARCHIVE_SECRET_KEY=test-archive-secret
CLASHLENS_OFFICIAL_API_ORIGIN=https://api.clashofclans.com
CLASHLENS_SCHEMA_VERSION=2
CLASHLENS_API_KEY_HOST_DIR=$KEY_DIR
CLASHLENS_NORMAL_API_KEY_FILES=normal-1=/run/secrets/normal-1,normal-2=/run/secrets/normal-2,normal-3=/run/secrets/normal-3,normal-4=/run/secrets/normal-4
CLASHLENS_INTERACTIVE_API_KEY_FILES=interactive-1=/run/secrets/interactive-1
CLASHLENS_HEALTH_HOST=127.0.0.1
CLASHLENS_HEALTH_PORT=18081
EOF
chmod 0600 "$ENV_FILE"

INLINE_ENV_FILE="$WORK_DIR/inline.env"
cp "$ENV_FILE" "$INLINE_ENV_FILE"
printf '%s\n' \
  'CLASHLENS_NORMAL_API_KEYS=normal-inline=inline-secret-do-not-print' \
  'CLASHLENS_INTERACTIVE_API_KEYS=interactive-inline=inline-secret-do-not-print' \
  >>"$INLINE_ENV_FILE"
chmod 0600 "$INLINE_ENV_FILE"
if output=$(FAKE_PODMAN_LOG="$PODMAN_LOG" DEPLOY_ENV_FILE="$INLINE_ENV_FILE" PODMAN_BIN="$FAKE_BIN/podman" CURL_BIN="$FAKE_BIN/curl" "$ROOT_DIR/deploy.sh" up 2>&1); then
  printf 'deployment accepted inline API-key secrets\n' >&2
  exit 1
fi
[[ "$output" == *'inline API keys must not be set'* ]] || {
  printf 'deployment did not report the inline API-key guard\n' >&2
  exit 1
}
[[ "$output" != *'inline-secret-do-not-print'* ]] || {
  printf 'deployment printed an inline API-key secret\n' >&2
  exit 1
}
printf 'ok: deployment rejects inline API-key secrets\n'

FAKE_PODMAN_LOG="$PODMAN_LOG" \
  DEPLOY_ENV_FILE="$ENV_FILE" \
  PODMAN_BIN="$FAKE_BIN/podman" \
  CURL_BIN="$FAKE_BIN/curl" \
  "$ROOT_DIR/deploy.sh" up >/dev/null

postgres_run=$(grep 'postgres:17-alpine' "$PODMAN_LOG")
collector_run=$(grep 'clashlens-collector:deployment' "$PODMAN_LOG" | grep '^run ')
normalized_postgres_run=${postgres_run//\\/}
normalized_collector_run=${collector_run//\\/}

migration_exec_count=$(grep -c '^exec --interactive clashlens-postgres psql ' "$PODMAN_LOG")
[[ "$migration_exec_count" == "2" ]] || {
  printf 'deployment applied %s migration files, want 2\n' "$migration_exec_count" >&2
  exit 1
}

collector_build_line=$(grep -n '^build .*clashlens-collector:deployment' "$PODMAN_LOG")
collector_build_line=${collector_build_line%%:*}
collector_start_line=$(grep -n '^run .*clashlens-collector:deployment' "$PODMAN_LOG")
collector_start_line=${collector_start_line%%:*}
migration_lines=()
while IFS=: read -r line_number _; do
  migration_lines+=("$line_number")
done < <(grep -n '^exec --interactive clashlens-postgres psql ' "$PODMAN_LOG")
second_migration_line=${migration_lines[1]:-}
[[ -n "$collector_build_line" && -n "$second_migration_line" && "$collector_build_line" -lt "$second_migration_line" ]] || {
  printf 'collector bridge image was not built before migration 0002\n' >&2
  exit 1
}
[[ -n "$collector_start_line" && "$collector_start_line" -lt "$second_migration_line" ]] || {
  printf 'collector bridge was not started before migration 0002\n' >&2
  exit 1
}

[[ "$normalized_collector_run" == *'--env CLASHLENS_SCHEMA_VERSION '* ]] || {
  printf 'collector bridge did not receive the schema version setting\n' >&2
  exit 1
}
grep -q '^CLASHLENS_SCHEMA_VERSION=2$' "$ENV_FILE" || {
  printf 'deployment test did not configure schema version 2\n' >&2
  exit 1
}

if [[ "$postgres_run" == *"--env-file"* ]]; then
  printf 'PostgreSQL received the full app.env file\n' >&2
  exit 1
fi

if [[ "$postgres_run" == *'--env POSTGRES_PASSWORD '* ]]; then
  printf 'PostgreSQL password was passed through container environment metadata\n' >&2
  exit 1
fi
[[ "$normalized_postgres_run" == *'--env POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password'* ]] || {
  printf 'PostgreSQL password file setting is missing\n' >&2
  exit 1
}
[[ "$normalized_postgres_run" == *'clashlens-postgres-password,type=mount,target=/run/secrets/postgres-password,uid=70,gid=70,mode=0400'* ]] || {
  printf 'PostgreSQL password secret mount is missing\n' >&2
  exit 1
}

if [[ "$collector_run" != *'CLASHLENS_HEALTH_LISTEN=0.0.0.0:8081'* ]]; then
  printf 'collector did not use fixed internal health port 8081\n' >&2
  exit 1
fi

if [[ "$collector_run" != *'127.0.0.1:18081:8081/tcp'* ]]; then
  printf 'collector health publication did not map the configured host port to internal port 8081\n' >&2
  exit 1
fi

if [[ "$collector_run" == *'type=bind'* ]]; then
  printf 'collector used direct bind mounts for API keys\n' >&2
  exit 1
fi

for setting in CLASHLENS_DATABASE_URL CLASHLENS_ARCHIVE_ACCESS_KEY CLASHLENS_ARCHIVE_SECRET_KEY; do
  if [[ "$collector_run" == *"--env $setting "* ]]; then
    printf '%s was passed through collector environment metadata\n' "$setting" >&2
    exit 1
  fi
done

for specification in \
  'CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url' \
  'CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key' \
  'CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key'; do
  [[ "$normalized_collector_run" == *"--env $specification"* ]] || {
    printf 'collector credential file setting %s is missing\n' "$specification" >&2
    exit 1
  }
done

for name in database-url archive-access-key archive-secret-key; do
  grep -q "^secret create --replace clashlens-$name -" "$PODMAN_LOG" || {
    printf 'Podman credential secret %s was not created or replaced\n' "$name" >&2
    exit 1
  }
  [[ "$normalized_collector_run" == *"clashlens-$name,type=mount,target=/run/secrets/$name,uid=10001,gid=10001,mode=0400"* ]] || {
    printf 'Podman credential secret %s was not mounted for the collector UID\n' "$name" >&2
    exit 1
  }
done

for name in normal-1 normal-2 normal-3 normal-4 interactive-1; do
  grep -q "^secret create --replace clashlens-$name " "$PODMAN_LOG" || {
    printf 'Podman secret %s was not created or replaced\n' "$name" >&2
    exit 1
  }
  [[ "$normalized_collector_run" == *"clashlens-$name,type=mount,target=/run/secrets/$name,uid=10001,gid=10001,mode=0400"* ]] || {
    printf 'Podman secret %s was not mounted for the collector UID\n' "$name" >&2
    exit 1
  }
done

printf 'ok: deployment limits secret scope and maps the health port correctly\n'

FAKE_PRODUCTION_READY=1 \
  FAKE_PODMAN_LOG="$PODMAN_LOG" \
  DEPLOY_ENV_FILE="$ENV_FILE" \
  PODMAN_BIN="$FAKE_BIN/podman" \
  "$ROOT_DIR/deploy.sh" python-up >/dev/null

python_worker_run=$(grep 'clashlens-python-worker:deployment' "$PODMAN_LOG" | grep '^run ')
normalized_python_worker_run=${python_worker_run//\\/}
[[ -n "$python_worker_run" ]] || {
  printf 'production Python worker was not started\n' >&2
  exit 1
}
[[ "$normalized_python_worker_run" == *'--network clashlens-private'* ]] || {
  printf 'production Python worker did not use the private network\n' >&2
  exit 1
}
[[ "$normalized_python_worker_run" != *'--publish'* ]] || {
  printf 'production Python worker published a host port\n' >&2
  exit 1
}
for specification in \
  '--read-only' \
  '--cap-drop all' \
  '--security-opt no-new-privileges' \
  '--memory 384m' \
  '--pids-limit 256' \
  '--cpus 1.0' \
  '--health-cmd python -m clashlens_prototype.cli ready --expected-contract-version 2' \
  'worker --owner production-python-1 --max-jobs 100 --lease-seconds 60 --run-forever'; do
  [[ "$normalized_python_worker_run" == *"$specification"* ]] || {
    printf 'production Python worker is missing runtime setting %s\n' "$specification" >&2
    exit 1
  }
done
for name in database-url archive-access-key archive-secret-key; do
  grep -q "^secret create --replace clashlens-python-worker-$name -" "$PODMAN_LOG" || {
    printf 'production Python worker secret %s was not refreshed\n' "$name" >&2
    exit 1
  }
  [[ "$normalized_python_worker_run" == *"clashlens-python-worker-$name,type=mount,target=/run/secrets/$name,uid=10001,gid=10001,mode=0400"* ]] || {
    printf 'production Python worker secret %s was not mounted for UID 10001\n' "$name" >&2
    exit 1
  }
done
for setting in \
  'CLASHLENS_DATABASE_URL_FILE=/run/secrets/database-url' \
  'CLASHLENS_ARCHIVE_ACCESS_KEY_FILE=/run/secrets/archive-access-key' \
  'CLASHLENS_ARCHIVE_SECRET_KEY_FILE=/run/secrets/archive-secret-key'; do
  [[ "$normalized_python_worker_run" == *"--env $setting"* ]] || {
    printf 'production Python worker credential file setting %s is missing\n' "$setting" >&2
    exit 1
  }
done
[[ "$normalized_python_worker_run" != *'test-db-password'* && "$normalized_python_worker_run" != *'test-archive-secret'* ]] || {
  printf 'production Python worker command exposed a credential value\n' >&2
  exit 1
}

unit=$(<"$ROOT_DIR/deploy/systemd/clashlens-python-worker.service")
[[ "$unit" == *'ExecStart=%h/clashlens/deploy.sh python-up'* ]] || {
  printf 'production Python worker unit has no tracked start command\n' >&2
  exit 1
}
[[ "$unit" == *'ExecStop=%h/clashlens/deploy.sh python-down'* ]] || {
  printf 'production Python worker unit has no tracked stop command\n' >&2
  exit 1
}

printf 'ok: production Python worker is private, bounded, secret-backed, and systemd-managed\n'
