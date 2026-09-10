#!/usr/bin/env bash
# Phase 4 prospective observer driver for issue #92 Step 9 (Slice B).
#
# Relay-safe order: seed the exact durable budget rows (no traffic) ->
# pre-traffic verification while admission is blocked
# (`check start` fails closed on receipt/budget/cohort/deadline before any
# run exists) -> baseline/start observer -> authorize/start traffic ->
# scheduled stop (real `check watchdog`: planned core-end exit 0) ->
# monitored drain -> terminal evidence while relay and dependencies stay
# alive -> finalize -> manifest -> strict validate -> stop
# relay/dependencies only last.
#
# Failure path: the parent group-stop hook terms the whole producer group
# FIRST (collector + Python archive workers, no collector-only grace
# first), then traffic/workers stops, then a bounded failure capture and
# one finalization attempt with relay/dependencies alive, then relay/deps
# last. Traffic is never restarted. The minute loop and the scheduled
# stop run concurrently: whichever fails first cancels the other at once,
# so a fast sample failure can never leave producers running.
# Success path: monitored downstream drain, then workers stop, then
# terminal evidence/finalize while relay/dependencies stay alive; the
# failure path bypasses the drain and stops the group immediately.
#
# Hook contract (exact order, parent-owned launch provides the hooks):
#   traffic-start, then concurrently: sample loop + scheduled-stop.
#   scheduled-stop exit 0 (planned core-end) -> drain-monitor supervises
#   downstream-drain under the pinned caps, then workers-stop, evidence,
#   finalize, validate, relay-stop, deps-stop.
#   Any nonzero exit or signal -> group-stop, traffic-stop, workers-stop,
#   reap jobs, evidence, finalize attempt, relay-stop, deps-stop.
#
# All configuration arrives as CLI flags; the driver copies no credentials,
# host names, dates, or passwords. Every hook is an executable invoked with
# fixed arguments. Short stop/evidence/cleanup/finalize/validate hooks run
# under a fixed hook timeout, while the scheduled day stop runs under the
# separate absolute run deadline (24h default): wrapping the scheduled
# stop in the short hook timeout would kill every day run after minutes.
set -euo pipefail
# Monitor mode: every background job runs in its own process group, so
# cancelling a job can SIGKILL the whole tree. Plain kill of the job PID
# would orphan grandchildren (timeout/hook/sleep) still holding our pipes.
set -m

command -v timeout >/dev/null || {
    echo "step9_driver: coreutils timeout is required" >&2; exit 2; }

RUN_DIR=""
MODE="live-day"
DB_URL_FILE=""
STEP9_CHECK="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/step9_check.py"
TRAFFIC_START=""
TRAFFIC_STOP=""
SCHEDULED_STOP=""
GROUP_STOP=""
RELAY_STOP=""
WORKERS_STOP=""
DRAIN_CMD=""
RUN_DEADLINE_SECS=86400
SEED_DB_URL_FILE=""
BUDGET_RUN_ID=""
BUDGET_CAP_PROFILE=""
BUDGET_CAP_GLOBAL_RANKINGS=""
BUDGET_CAP_BATTLE_LOG=""
BUDGET_DEADLINE_AT=""
EVIDENCE_CMD="true"
DEPS_STOP="true"
HOOK_TIMEOUT_SECS=300
START_ARGS=()

usage() {
    cat >&2 <<'EOF'
usage: step9_driver.sh --run-dir DIR [options] [-- extra args for 'check start']
  --mode MODE              live-day (default) or preflight
  --db-url-file FILE       forwarded to sample/finalize
  --check EXE              step9 observer entrypoint
  --traffic-start EXE      authorize/start collection traffic (required)
  --traffic-stop EXE       halt collection traffic (required)
  --scheduled-stop EXE     scheduled end-of-day control command, normally
                           `check watchdog --deadline <core-end-exact>` with
                           --poll-seconds <=5 in production (required).
                           Planned core-end exit 0 continues to the
                           monitored drain; any nonzero safety exit stops
                           the whole group at once via --group-stop.
  --group-stop EXE         parent-owned immediate whole-group TERM
                           (collector + Python archive producers), fast and
                           idempotent (required). Runs FIRST on any failure
                           path, before traffic/workers stops.
  --relay-stop EXE         stop DB relay/evidence deps, runs last (required)
  --workers-stop EXE       stop Python archive-producing workers; runs first
                           in the failure path alongside traffic stop,
                           and on success after the downstream drain
                           (required)
  --downstream-drain EXE   bounded quiesce of the Python archive pipeline;
                           runs on success only, before workers stop
                           (required)
  --run-deadline-secs N    absolute deadline for the scheduled collector
                           watchdog stop (default 86400); short hook timeout
                           never applies to the day watchdog
  --seed-database-url-file FILE
                           privileged URL file for budget seeding (required)
  --budget-run-id ID       budget run to seed/bind (required)
  --budget-cap-profile N, --budget-cap-global-rankings N,
  --budget-cap-battle-log N, --budget-deadline-at TS
                           exact approved caps/deadline to seed (required)
  --hook-timeout-secs N    fixed timeout for stop/evidence/cleanup/finalize/
                           validate hooks; expiry fails the phase (default 300)
  --evidence-cmd EXE       terminal evidence capture (default: true)
  --deps-stop EXE          stop remaining dependencies, runs last (default: true)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --run-dir) RUN_DIR="${2:?missing value}"; shift 2;;
        --mode) MODE="${2:?missing value}"; shift 2;;
        --db-url-file) DB_URL_FILE="${2:?missing value}"; shift 2;;
        --check) STEP9_CHECK="${2:?missing value}"; shift 2;;
        --traffic-start) TRAFFIC_START="${2:?missing value}"; shift 2;;
        --traffic-stop) TRAFFIC_STOP="${2:?missing value}"; shift 2;;
        --scheduled-stop) SCHEDULED_STOP="${2:?missing value}"; shift 2;;
        --group-stop) GROUP_STOP="${2:?missing value}"; shift 2;;
        --relay-stop) RELAY_STOP="${2:?missing value}"; shift 2;;
        --workers-stop) WORKERS_STOP="${2:?missing value}"; shift 2;;
        --downstream-drain) DRAIN_CMD="${2:?missing value}"; shift 2;;
        --run-deadline-secs) RUN_DEADLINE_SECS="${2:?missing value}"; shift 2;;
        --seed-database-url-file) SEED_DB_URL_FILE="${2:?missing value}"; shift 2;;
        --budget-run-id) BUDGET_RUN_ID="${2:?missing value}"; shift 2;;
        --budget-cap-profile) BUDGET_CAP_PROFILE="${2:?missing value}"; shift 2;;
        --budget-cap-global-rankings) BUDGET_CAP_GLOBAL_RANKINGS="${2:?missing value}"; shift 2;;
        --budget-cap-battle-log) BUDGET_CAP_BATTLE_LOG="${2:?missing value}"; shift 2;;
        --budget-deadline-at) BUDGET_DEADLINE_AT="${2:?missing value}"; shift 2;;
        --evidence-cmd) EVIDENCE_CMD="${2:?missing value}"; shift 2;;
        --deps-stop) DEPS_STOP="${2:?missing value}"; shift 2;;
        --hook-timeout-secs) HOOK_TIMEOUT_SECS="${2:?missing value}"; shift 2;;
        --help|-h) usage; exit 0;;
        --) shift; while [ $# -gt 0 ]; do START_ARGS+=("$1"); shift; done;;
        *) echo "step9_driver: unknown argument: $1" >&2; usage; exit 2;;
    esac
done

[ -n "$RUN_DIR" ] || { echo "step9_driver: --run-dir is required" >&2; exit 2; }
[ -n "$TRAFFIC_START" ] || { echo "step9_driver: --traffic-start hook is required" >&2; exit 2; }
[ -n "$TRAFFIC_STOP" ] || { echo "step9_driver: --traffic-stop hook is required" >&2; exit 2; }
[ -n "$SCHEDULED_STOP" ] || { echo "step9_driver: --scheduled-stop hook is required" >&2; exit 2; }
[ -n "$GROUP_STOP" ] || { echo "step9_driver: --group-stop hook is required" >&2; exit 2; }
[ -n "$RELAY_STOP" ] || { echo "step9_driver: --relay-stop hook is required" >&2; exit 2; }
[ -n "$WORKERS_STOP" ] || { echo "step9_driver: --workers-stop hook is required" >&2; exit 2; }
[ -n "$DRAIN_CMD" ] || { echo "step9_driver: --downstream-drain hook is required" >&2; exit 2; }
case "$RUN_DEADLINE_SECS" in ''|*[!0-9]*|0)
    echo "step9_driver: --run-deadline-secs must be a positive integer" >&2
    exit 2;;
esac
[ -n "$SEED_DB_URL_FILE" ] || { echo "step9_driver: --seed-database-url-file is required" >&2; exit 2; }
[ -n "$BUDGET_RUN_ID" ] || { echo "step9_driver: --budget-run-id is required" >&2; exit 2; }
[ -n "$BUDGET_CAP_PROFILE" ] || { echo "step9_driver: --budget-cap-profile is required" >&2; exit 2; }
[ -n "$BUDGET_CAP_GLOBAL_RANKINGS" ] || { echo "step9_driver: --budget-cap-global-rankings is required" >&2; exit 2; }
[ -n "$BUDGET_CAP_BATTLE_LOG" ] || { echo "step9_driver: --budget-cap-battle-log is required" >&2; exit 2; }
[ -n "$BUDGET_DEADLINE_AT" ] || { echo "step9_driver: --budget-deadline-at is required" >&2; exit 2; }
case "$HOOK_TIMEOUT_SECS" in ''|*[!0-9]*|0)
    echo "step9_driver: --hook-timeout-secs must be a positive integer" >&2
    exit 2;;
esac

DB_ARGS=()
[ -n "$DB_URL_FILE" ] && DB_ARGS+=(--database-url-file "$DB_URL_FILE")

# The exit trap is installed only after this point, so usage errors and
# --help never run failure cleanup with unconfigured hooks.
SAMPLE_PID=""
STOP_PID=""
DRAIN_PID=""
FINISHED=0
IN_CLEANUP=0
SIGNALLED=0

bounded() {
    timeout -s KILL "$HOOK_TIMEOUT_SECS" "$@"
}

killjob() {
    # $1 is a background job PID (hence its group leader under set -m).
    kill -KILL -- "-$1" 2>/dev/null || true
    wait "$1" 2>/dev/null || true
}

on_signal() {
    # Record the signal death first: the EXIT trap below preserves it
    # even when the interrupted command had already returned 0.
    SIGNALLED=$1
    exit "$1"
}

on_exit() {
    rc=$?
    if [ "$SIGNALLED" -ne 0 ]; then
        rc=$SIGNALLED
    fi
    if [ "$FINISHED" -eq 0 ] && [ "$IN_CLEANUP" -eq 0 ]; then
        IN_CLEANUP=1
        # Failure (or signal) path: the parent group-stop terms the whole
        # producer group FIRST (collector + Python archive workers, no
        # collector-only grace first) and producers are never restarted,
        # so no further archive bytes are produced; queued jobs are
        # preserved (nothing drains or purges them) and the recorded
        # incomplete failure stands; relay/dependencies stay up for
        # bounded failure capture and one finalization attempt, then stop
        # last.
        bounded "$GROUP_STOP" || true
        bounded "$TRAFFIC_STOP" || true
        bounded "$WORKERS_STOP" || true
        for pid in "$SAMPLE_PID" "$STOP_PID" "$DRAIN_PID"; do
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                killjob "$pid"
            fi
        done
        bounded "$EVIDENCE_CMD" || true
        bounded "$STEP9_CHECK" finalize --run-dir "$RUN_DIR" \
            "${DB_ARGS[@]}" || true
        bounded "$RELAY_STOP" || true
        bounded "$DEPS_STOP" || true
    fi
    exit "$rc"
}
trap on_exit EXIT
trap 'on_signal 143' TERM
trap 'on_signal 130' INT

# Seed the exact durable endpoint-budget rows before observer admission
# (no traffic involved); start then binds them and fails closed on conflict.
"$STEP9_CHECK" seed-budget --budget-run-id "$BUDGET_RUN_ID" \
    --budget-cap-profile "$BUDGET_CAP_PROFILE" \
    --budget-cap-global-rankings "$BUDGET_CAP_GLOBAL_RANKINGS" \
    --budget-cap-battle-log "$BUDGET_CAP_BATTLE_LOG" \
    --budget-deadline-at "$BUDGET_DEADLINE_AT" \
    --database-url-file "$SEED_DB_URL_FILE"
# Pre-traffic verification while admission is blocked: start fails closed on
# receipt/budget/cohort/deadline evidence before any run exists or traffic
# flows. It also pins baselines including the observer header.
"$STEP9_CHECK" start --run-dir "$RUN_DIR" --mode "$MODE" "${START_ARGS[@]}"
# Authorize and start collection traffic.
"$TRAFFIC_START"
# Minute loop and scheduled stop run concurrently and the first
# completion wins: a fast sample/archive-cap failure cancels the scheduled
# stop at once (and vice versa), so producers never run past a known
# failure. A planned core-end stop (exit 0) waits out the sample loop and
# continues to the monitored drain; a safety stop (nonzero) kills the
# loop and fails into immediate whole-group stop.
"$STEP9_CHECK" sample --run-dir "$RUN_DIR" "${DB_ARGS[@]}" &
SAMPLE_PID=$!
# The scheduled stop runs under the absolute run deadline, never the
# short hook timeout: a day stop must outlive minutes-long evidence
# grace. Background timeout itself, not the bounded() wrapper: GNU timeout
# moves itself and its child into a fresh process group led by timeout's
# own PID, so only timeout's PID is a valid group-kill target. A wrapper
# subshell would leave timeout and its descendants orphaned past a cancel.
timeout -s KILL "$RUN_DEADLINE_SECS" "$SCHEDULED_STOP" &
STOP_PID=$!
FIRST_RC=0
DONE_PID=""
wait -n -p DONE_PID "$SAMPLE_PID" "$STOP_PID" || FIRST_RC=$?
if [ "$DONE_PID" = "$SAMPLE_PID" ]; then
    if [ "$FIRST_RC" -ne 0 ]; then
        killjob "$STOP_PID"
        exit "$FIRST_RC"
    fi
    wait "$STOP_PID" || exit "$?"
else
    if [ "$FIRST_RC" -ne 0 ]; then
        killjob "$SAMPLE_PID"
        exit "$FIRST_RC"
    fi
    wait "$SAMPLE_PID" || exit "$?"
fi
# Success path only: the real drain-monitor watches the pinned caps and
# drain-child stall while the downstream drain runs; any breach or timeout
# kills the drain and fails into the group-stop cleanup above, so no
# archive activity runs unobserved. Then workers stop, then terminal
# producer/resource/operating evidence while relay/dependencies live.
"$DRAIN_CMD" &
DRAIN_PID=$!
# The monitor's own budget stays strictly inside the outer hook timeout
# so its timeout record is always written before the outer SIGKILL.
MONITOR_SECS=$((HOOK_TIMEOUT_SECS - 5))
[ "$MONITOR_SECS" -ge 1 ] || MONITOR_SECS=1
if bounded "$STEP9_CHECK" drain-monitor --run-dir "$RUN_DIR" \
        --drain-pid "$DRAIN_PID" --timeout-seconds "$MONITOR_SECS" \
        "${DB_ARGS[@]}"; then
    wait "$DRAIN_PID" || exit "$?"
else
    killjob "$DRAIN_PID"
    exit 1
fi
DRAIN_PID=""
bounded "$WORKERS_STOP"
bounded "$EVIDENCE_CMD"
# Finalize (writes manifest) then strict validate; relay stops only after.
bounded "$STEP9_CHECK" finalize --run-dir "$RUN_DIR" "${DB_ARGS[@]}"
bounded "$STEP9_CHECK" validate --run-dir "$RUN_DIR"
bounded "$RELAY_STOP"
bounded "$DEPS_STOP"
FINISHED=1
