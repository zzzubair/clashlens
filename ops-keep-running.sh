# Sourced by ./ops: what up checks before it stops anything, stopping the
# stack, leaving the collector running through an up that changes nothing it
# runs on, and the alerts' view of all that.
#
# The collector Requires the database and pod and exits about a second into a
# database outage, so it keeps running only if all of these keep running too.
# The collector's scheduling memory, such as the Season 0 wait, survives only
# while its process does.
KEEP_UNITS=(clashlens-collector clashlens-postgres clashlens-pod clashlens-network clashlens-postgres-volume)
KEEP_RECORD="$STATE_DIR/kept-services.env"
KEEP_LABELS=(MODE 'deployment mode' COLLECTOR_UNIT 'collector service definition' COLLECTOR_ENV 'collector settings'
  COLLECTOR_SECRETS 'collector secrets' POSTGRES_UNIT 'database service definition' POSTGRES_ENV 'database settings'
  POSTGRES_SECRETS 'database secrets' POD_UNIT 'pod definition' NETWORK_UNIT 'network definition'
  VOLUME_UNIT 'database volume definition' MIGRATIONS 'database migrations')
RESTART_COLLECTOR=false
KEEP_RUNNING=false
declare -Ag KEEP_PARTS=()

stop_units() {
  local unit state keep=' '
  [[ "$KEEP_RUNNING" != true ]] || keep=" ${KEEP_UNITS[*]} "
  "$SYSTEMCTL_BIN" --user stop "${TIMER_UNITS[@]}" >/dev/null 2>&1 || true
  if [[ "$KEEP_RUNNING" == true ]]; then
    # Stopping the target stops every PartOf member, so it stays active; up starts it again.
    "$SYSTEMCTL_BIN" --user disable clashlens.target >/dev/null 2>&1 || true
  else
    "$SYSTEMCTL_BIN" --user disable --now clashlens.target >/dev/null 2>&1 || true
  fi
  for unit in clashlens-website clashlens-collector clashlens-api clashlens-worker clashlens-login clashlens-clash-api clashlens-archive clashlens-postgres clashlens-pod clashlens-network clashlens-postgres-volume clashlens-python-api clashlens-python-worker clashlens; do
    [[ "$keep" != *" $unit "* ]] || continue
    "$SYSTEMCTL_BIN" --user stop "$unit.service" >/dev/null 2>&1 || true
  done
  state=$($SYSTEMCTL_BIN --user is-enabled clashlens.target 2>/dev/null || true)
  [[ "$state" != enabled && "$state" != enabled-runtime ]] || die "could not disable clashlens.target"
  for unit in clashlens-website clashlens-worker clashlens-api clashlens-collector clashlens-login clashlens-clash-api clashlens-archive clashlens-postgres clashlens-pod clashlens-network clashlens-postgres-volume clashlens-python-api clashlens-python-worker clashlens; do
    [[ "$keep" != *" $unit "* ]] || continue
    state=$($SYSTEMCTL_BIN --user is-active "$unit.service" 2>/dev/null || true)
    case "$state" in active|activating|deactivating|reloading) die "could not stop $unit.service" ;; esac
  done
}

# Every build labels its images with the commit, so image IDs always differ.
# Compare what runs instead: the file layers and run settings.
image_identity() {
  local fields
  fields=$("$PODMAN_BIN" image inspect --format '{{json .RootFS.Layers}} {{json .Config.Entrypoint}} {{json .Config.Cmd}} {{json .Config.Env}} {{json .Config.User}} {{json .Config.WorkingDir}} {{json .Config.Volumes}} {{json .Config.StopSignal}}' "$1" 2>/dev/null) && [[ -n "$fields" ]] || return 1
  printf '%s' "$fields" | sha256sum | cut -d' ' -f1
}

# Before stopping anything: keep the collector only if it, the database, pod
# and network run, a record of their configuration exists and both images match.
keep_running_plan() {
  local reason= unit service image running current
  KEEP_RUNNING=false
  if [[ "$RESTART_COLLECTOR" == true ]]; then reason='--restart-collector was given'
  elif [[ ! -f "$KEEP_RECORD" || -L "$KEEP_RECORD" ]]; then reason='no record of the configuration it runs with'
  else
    for unit in "${KEEP_UNITS[@]}"; do
      [[ "$($SYSTEMCTL_BIN --user is-active "$unit.service" 2>/dev/null || true)" == active ]] || { reason="$unit is not running"; break; }
    done
    for service in collector postgres; do
      [[ -z "$reason" ]] || break
      current=$(container_state "$(container_name "$PREFIX" "$service")")
      [[ "$current" == healthy || "$current" == running ]] || { reason="the $service container is $current"; break; }
      image=$($PODMAN_BIN inspect --format '{{.Image}}' "$(container_name "$PREFIX" "$service")" 2>/dev/null) || { reason="the $service container could not be inspected"; break; }
      image=$(normalize_image_id "$image")
      running=$(image_identity "$image" || true)
      current=$(image_identity "${RELEASE[${service^^}_IMAGE]}" || true)
      [[ -n "$running" && "$running" == "$current" ]] || { reason="the $service image changed"; break; }
      # Same contents: keep the running image pinned so units and the active release match it.
      RELEASE[${service^^}_IMAGE]=$image
    done
  fi
  if [[ -n "$reason" ]]; then
    rm -f -- "$KEEP_RECORD"
    printf 'Restarting the collector with the database, pod and network: %s.\n' "$reason"
    return
  fi
  KEEP_RUNNING=true
}

file_digest() { sed '/^Image=/d' -- "$1" | sha256sum | cut -d' ' -f1; }

secret_digest() {
  local name data
  for name in $(sed -n 's/^Secret=\([^,]*\),.*/\1/p' "$1"); do
    data=$("$PODMAN_BIN" secret inspect --showsecret --format '{{.SecretData}}' "$name" 2>/dev/null) || return 1
    printf '%s\0%s\n' "$name" "$(printf '%s' "$data" | sha256sum | cut -d' ' -f1)"
  done | sha256sum | cut -d' ' -f1
}

# Digests of the rendered files, never values; secrets are read only to hash them.
keep_running_parts() {
  local migration
  KEEP_PARTS=([MODE]="$MODE")
  KEEP_PARTS[COLLECTOR_UNIT]=$(file_digest "$QUADLET_DIR/clashlens-collector.container")
  KEEP_PARTS[POSTGRES_UNIT]=$(file_digest "$QUADLET_DIR/clashlens-postgres.container")
  KEEP_PARTS[POD_UNIT]=$(file_digest "$QUADLET_DIR/clashlens.pod")
  KEEP_PARTS[NETWORK_UNIT]=$(file_digest "$QUADLET_DIR/clashlens.network")
  KEEP_PARTS[VOLUME_UNIT]=$(file_digest "$QUADLET_DIR/clashlens-postgres.volume")
  KEEP_PARTS[COLLECTOR_ENV]=$(file_digest "$STATE_DIR/env/collector.env")
  KEEP_PARTS[POSTGRES_ENV]=$(file_digest "$STATE_DIR/env/postgres.env")
  KEEP_PARTS[MIGRATIONS]=$(for migration in "$ROOT"/deploy/migrations/*.sql; do printf '%s\0%s\n' "${migration##*/}" "$(sha256sum < "$migration")"; done | sha256sum | cut -d' ' -f1)
  KEEP_PARTS[COLLECTOR_SECRETS]=$(secret_digest "$QUADLET_DIR/clashlens-collector.container") || return 1
  KEEP_PARTS[POSTGRES_SECRETS]=$(secret_digest "$QUADLET_DIR/clashlens-postgres.container") || return 1
}

# After the new files are written: restart them all if anything they read changed.
keep_running_check() {
  local index value changed=() reason=
  if ! keep_running_parts; then
    KEEP_PARTS=()
    reason='a secret could not be read to compare'
  elif [[ "$KEEP_RUNNING" == true ]]; then
    local -A recorded=()
    while IFS='=' read -r index value; do if [[ -n "$index" ]]; then recorded[$index]=$value; fi; done < "$KEEP_RECORD"
    for ((index = 0; index < ${#KEEP_LABELS[@]}; index += 2)); do
      [[ "${recorded[${KEEP_LABELS[index]}]-}" == "${KEEP_PARTS[${KEEP_LABELS[index]}]}" ]] || changed+=("${KEEP_LABELS[index + 1]}")
    done
  fi
  [[ "$KEEP_RUNNING" == true ]] || return 0
  ((${#changed[@]} == 0)) || reason="changed $(IFS=,; printf '%s' "${changed[*]}" | sed 's/,/, /g')"
  if [[ -z "$reason" ]]; then
    printf 'Leaving the collector, database, pod and network running: their images and configuration are unchanged.\n'
    return
  fi
  rm -f -- "$KEEP_RECORD"
  printf 'Restarting the collector with the database, pod and network: %s.\n' "$reason"
  KEEP_RUNNING=false
  stop_units
}

keep_running_record() {
  local temporary index
  ((${#KEEP_PARTS[@]})) || return 0
  temporary=$(mktemp "$STATE_DIR/kept-services.env.XXXXXX")
  chmod 600 "$temporary"
  for ((index = 0; index < ${#KEEP_LABELS[@]}; index += 2)); do
    printf '%s=%s\n' "${KEEP_LABELS[index]}" "${KEEP_PARTS[${KEEP_LABELS[index]}]}" >> "$temporary"
  done
  mv -f "$temporary" "$KEEP_RECORD"
}

# What the alert check should make of the stack: stopped while ./ops stops or
# starts it on purpose, running once up succeeds, failed when up left it stopped.
write_alert_intent() {
  local temporary
  temporary=$(mktemp "$STATE_DIR/alert-intent.XXXXXX")
  chmod 600 "$temporary"
  printf '%s\n' "$1" > "$temporary"
  mv -f "$temporary" "$STATE_DIR/alert-intent"
}

alert_webhook_file() {
  setting CLASHLENS_DISCORD_ALERT_WEBHOOK_FILE "$(setting CLASHLENS_API_KEY_HOST_DIR /srv/clashlens-secrets)/clashlens-discord-alert-webhook"
}

# Before anything stops: the release must ship every migration the database
# has applied, or its code would run on tables it does not know. A rollback is
# a new release that keeps them; ./ops never reverses a migration.
check_migrations() {
  local applied known missing migration
  [[ "$(container_state "$(container_name "$PREFIX" postgres)")" == healthy ]] || return 0
  applied=$(psql_exec --tuples-only --no-align --command 'SELECT version FROM clash_lens_schema_migrations') || \
    die "could not read the database's migrations; nothing was stopped"
  known=$(for migration in "$ROOT"/deploy/migrations/*.sql; do migration=${migration##*/}; printf '%d\n' "$((10#${migration%%_*}))"; done)
  missing=$(comm -23 <(sort <<< "$applied") <(sort <<< "$known") | sort -n | paste -sd, -)
  [[ -z "$missing" ]] || die "the database has migrations $missing that this release lacks; nothing was stopped"
}

# A failed up has stopped the stack and the alert timer with it, so it sends
# this alert itself; the next ./ops up that succeeds clears it.
deploy_failed_alert() {
  write_alert_intent failed || return 0
  [[ "$MODE" == production ]] || return 0
  python3 "$ROOT/python/src/clashlens/alerts.py" --deploy-failed "$STATE_DIR" "$(alert_webhook_file)" || \
    printf 'ops: the failed-deploy alert was not delivered; ./ops alert-check retries it\n' >&2
}
