"""Tests for scripts/step9_driver.sh.

Ordering/cleanup tests stub every hook under tmp_path: no host traffic,
no containers, no database. The real-integration tests at the end run the
actual driver with the real `check watchdog` scheduled stop (shortened
clocks, fake podman binary) and the real `check drain-monitor`, plus
parent-equivalent hook stubs; a fake zero hook alone never stands in for
the scheduled stop or the drain observation.
"""

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DRIVER = Path(__file__).with_name("step9_driver.sh")
STEP9 = Path(__file__).with_name("step9_check.py")

CHECK_STUB = """#!/usr/bin/env bash
echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"
if [ "${STEP9_DRIVER_FAIL_AT:-}" = "$1" ]; then exit 1; fi
exit 0
"""

HOOK_STUB = """#!/usr/bin/env bash
echo "hook:HOOKNAME" >> "$STEP9_DRIVER_TEST_LOG"
if [ "${STEP9_DRIVER_FAIL_HOOK:-}" = "HOOKNAME" ]; then exit 1; fi
exit 0
"""

# A real scheduled stop blocks while the minute loop runs, so the fake waits
# until the background sample has logged before recording itself.
SCHEDULED_STUB = """#!/usr/bin/env bash
for _ in $(seq 1 500); do
  if grep -q "^check:sample " "$STEP9_DRIVER_TEST_LOG" 2>/dev/null; then
    break
  fi
  sleep 0.02
done
echo "hook:scheduled_stop" >> "$STEP9_DRIVER_TEST_LOG"
if [ "${STEP9_DRIVER_FAIL_HOOK:-}" = "scheduled_stop" ]; then exit 1; fi
exit 0
"""

def _exe(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return str(path)


def _stage(tmp_path: Path, *, fail_at: str = "", fail_hook: str = "") -> dict:
    hooks = {}
    for name in ("traffic_start", "traffic_stop", "group_stop",
                 "workers_stop", "downstream_drain", "relay_stop",
                 "evidence", "deps_stop"):
        body = HOOK_STUB.replace("HOOKNAME", name)
        hooks[name] = _exe(tmp_path / f"hook-{name}.sh", body)
    hooks["scheduled_stop"] = _exe(tmp_path / "hook-scheduled_stop.sh",
                                     SCHEDULED_STUB)
    env = dict(os.environ)
    env["STEP9_DRIVER_TEST_LOG"] = str(tmp_path / "order.log")
    env["STEP9_DRIVER_FAIL_AT"] = fail_at
    env["STEP9_DRIVER_FAIL_HOOK"] = fail_hook
    return {"hooks": hooks, "env": env,
            "check": _exe(tmp_path / "fake-check.sh", CHECK_STUB)}


def _base_args(stage: dict, run_dir: Path, hook_timeout_secs=None,
                run_deadline_secs=None) -> list:
    hooks = stage["hooks"]
    timeout_args = [] if hook_timeout_secs is None else [
        "--hook-timeout-secs", str(hook_timeout_secs)]
    deadline_args = [] if run_deadline_secs is None else [
        "--run-deadline-secs", str(run_deadline_secs)]
    return [str(DRIVER), "--run-dir", str(run_dir), "--mode", "preflight",
            "--check", stage["check"],
            *timeout_args,
            *deadline_args,
            "--seed-database-url-file", "seed-url-file",
            "--budget-run-id", "budget1",
            "--budget-cap-profile", "13500",
            "--budget-cap-global-rankings", "1",
            "--budget-cap-battle-log", "0",
            "--budget-deadline-at", "2026-10-05T06:00:00+00:00",
            "--workers-stop", hooks["workers_stop"],
            "--downstream-drain", hooks["downstream_drain"],
            "--traffic-start", hooks["traffic_start"],
            "--traffic-stop", hooks["traffic_stop"],
            "--scheduled-stop", hooks["scheduled_stop"],
            "--group-stop", hooks["group_stop"],
            "--relay-stop", hooks["relay_stop"],
            "--evidence-cmd", hooks["evidence"],
            "--deps-stop", hooks["deps_stop"],
            "--", "--cohort-file", "cohort"]


def _lines(tmp_path: Path) -> list:
    log = tmp_path / "order.log"
    if not log.exists():
        return []
    return [line.split(" ", 1)[0]
            for line in log.read_text(encoding="utf-8").splitlines()]


def test_driver_success_ordering(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    result = subprocess.run(_base_args(stage, tmp_path / "run"),
                            env=stage["env"], capture_output=True, text=True, check=False,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert _lines(tmp_path) == [
        "check:seed-budget", "check:start", "hook:traffic_start",
        "check:sample", "hook:scheduled_stop", "hook:downstream_drain",
        "check:drain-monitor", "hook:workers_stop", "hook:evidence",
        "check:finalize", "check:validate", "hook:relay_stop",
        "hook:deps_stop",
    ]
    log = (tmp_path / "order.log").read_text(encoding="utf-8")
    assert "--run-dir" in log and "--cohort-file cohort" in log


def test_driver_finalize_failure_cleanup(tmp_path: Path) -> None:
    stage = _stage(tmp_path, fail_at="finalize")
    result = subprocess.run(_base_args(stage, tmp_path / "run"),
                            env=stage["env"], capture_output=True, text=True, check=False,
                            timeout=60)
    assert result.returncode != 0
    lines = _lines(tmp_path)
    # The success path drains, stops workers, and captures evidence before
    # the failing finalize; cleanup then stops traffic first, retries a
    # bounded failure capture/finalize, and stops relay/dependencies last;
    # traffic never restarts.
    first_finalize = lines.index("check:finalize")
    assert lines.index("hook:downstream_drain") < \
        lines.index("hook:workers_stop") < first_finalize
    assert first_finalize < lines.index("hook:group_stop")
    assert lines.index("hook:group_stop") < lines.index("hook:traffic_stop")
    assert lines.index("hook:traffic_stop") < lines.index("hook:relay_stop")
    assert lines.index("hook:relay_stop") < lines.index("hook:deps_stop")
    assert lines.count("hook:traffic_start") == 1
    assert lines.count("check:finalize") == 2
    assert "hook:traffic_stop" in lines


def test_driver_scheduled_stop_failure_kills_sample(tmp_path: Path) -> None:
    stage = _stage(tmp_path, fail_hook="scheduled_stop")
    # Blocking sample proves the failure path kills the background loop.
    check = stage["check"]
    Path(check).write_text(
        "#!/usr/bin/env bash\n"
        'echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"\n'
        'if [ "$1" != "sample" ]; then exit 0; fi\n'
        "exec sleep 60\n",
        encoding="utf-8")
    result = subprocess.run(_base_args(stage, tmp_path / "run"),
                            env=stage["env"], capture_output=True, text=True, check=False,
                            timeout=90)
    assert result.returncode != 0
    lines = _lines(tmp_path)
    assert lines[:8] == ["check:seed-budget", "check:start",
                         "hook:traffic_start", "check:sample",
                         "hook:scheduled_stop", "hook:group_stop",
                         "hook:traffic_stop", "hook:workers_stop"]
    assert lines.count("hook:traffic_start") == 1
    assert "hook:traffic_stop" in lines
    assert lines.index("hook:traffic_stop") < lines.index("hook:relay_stop")


def test_driver_sample_failure_cancels_sleeping_watchdog(tmp_path: Path) -> None:
    import time
    # Immediate sample/archive-cap failure must cancel the scheduled stop
    # at once instead of leaving producers running behind a sleeping hook.
    stage = _stage(tmp_path)
    check = stage["check"]
    Path(check).write_text(
        "#!/usr/bin/env bash\n"
        'echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"\n'
        'if [ "$1" != "sample" ]; then exit 0; fi\n'
        "sleep 0.5\n"
        "exit 1\n",
        encoding="utf-8")
    collector = stage["hooks"]["scheduled_stop"]
    Path(collector).write_text(
        "#!/usr/bin/env bash\n"
        "for _ in $(seq 1 500); do\n"
        '  if grep -q "^check:sample " "$STEP9_DRIVER_TEST_LOG"'
        " 2>/dev/null; then break; fi\n"
        "  sleep 0.02\n"
        "done\n"
        'echo "hook:scheduled_stop" >> "$STEP9_DRIVER_TEST_LOG"\n'
        "exec sleep 60\n",
        encoding="utf-8")
    started = time.monotonic()
    result = subprocess.run(_base_args(stage, tmp_path / "run"),
                            env=stage["env"], capture_output=True, text=True, check=False,
                            timeout=90)
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 30
    lines = _lines(tmp_path)
    assert lines[:8] == ["check:seed-budget", "check:start",
                         "hook:traffic_start", "check:sample",
                         "hook:scheduled_stop", "hook:group_stop",
                         "hook:traffic_stop", "hook:workers_stop"]
    assert lines.count("hook:traffic_start") == 1
    assert lines.index("hook:workers_stop") < lines.index("hook:relay_stop")
    # The failure path bypasses the downstream drain and stops producers.
    assert "hook:downstream_drain" not in lines


def test_driver_hanging_hook_cannot_hang_driver(tmp_path: Path) -> None:
    import time
    # A scheduled watchdog that never returns is SIGKILLed at the absolute
    # run deadline (never the short hook timeout) and the run still fails
    # closed with full cleanup.
    stage = _stage(tmp_path)
    collector = stage["hooks"]["scheduled_stop"]
    Path(collector).write_text(
        "#!/usr/bin/env bash\n"
        'echo "hook:scheduled_stop" >> "$STEP9_DRIVER_TEST_LOG"\n'
        "exec sleep 3600\n",
        encoding="utf-8")
    started = time.monotonic()
    result = subprocess.run(
        _base_args(stage, tmp_path / "run", hook_timeout_secs=3,
                    run_deadline_secs=5),
        env=stage["env"], capture_output=True, text=True, check=False,
        timeout=90)
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 30
    lines = _lines(tmp_path)
    assert "hook:group_stop" in lines
    assert "hook:traffic_stop" in lines
    assert "hook:workers_stop" in lines
    assert "hook:relay_stop" in lines
    assert lines.count("hook:traffic_start") == 1


def test_driver_term_runs_failure_cleanup(tmp_path: Path) -> None:
    import signal
    import time
    # SIGTERM mid-run must halt producers and run full cleanup, not strand
    # the loop or relay behind the default disposition.
    stage = _stage(tmp_path)
    check = stage["check"]
    Path(check).write_text(
        "#!/usr/bin/env bash\n"
        'echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"\n'
        'if [ "$1" != "sample" ]; then exit 0; fi\n'
        "exec sleep 60\n",
        encoding="utf-8")
    log = tmp_path / "order.log"
    with subprocess.Popen(_base_args(stage, tmp_path / "run"),
                           env=stage["env"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True) as proc:
        for _ in range(500):
            if log.exists() and "check:sample " in log.read_text(
                    encoding="utf-8"):
                break
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        try:
            _, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("driver ignored SIGTERM")
    assert proc.returncode == 128 + signal.SIGTERM
    lines = _lines(tmp_path)
    assert lines.count("hook:traffic_start") == 1
    assert lines.index("hook:group_stop") < lines.index("hook:traffic_stop")
    assert lines.index("hook:traffic_stop") < lines.index("hook:relay_stop")
    assert "hook:workers_stop" in lines


def test_driver_int_runs_failure_cleanup(tmp_path: Path) -> None:
    import signal
    import time
    # SIGINT mid-run must preserve signal status 130 (even when the
    # interrupted command had returned 0) with group-stop first.
    stage = _stage(tmp_path)
    check = stage["check"]
    Path(check).write_text(
        "#!/usr/bin/env bash\n"
        'echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"\n'
        'if [ "$1" != "sample" ]; then exit 0; fi\n'
        "exec sleep 60\n",
        encoding="utf-8")
    log = tmp_path / "order.log"
    with subprocess.Popen(_base_args(stage, tmp_path / "run"),
                           env=stage["env"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True) as proc:
        for _ in range(500):
            if log.exists() and "check:sample " in log.read_text(
                    encoding="utf-8"):
                break
            time.sleep(0.02)
        proc.send_signal(signal.SIGINT)
        try:
            _, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("driver ignored SIGINT")
    assert proc.returncode == 128 + signal.SIGINT
    lines = _lines(tmp_path)
    assert lines.count("hook:traffic_start") == 1
    assert lines.index("hook:group_stop") < lines.index("hook:traffic_stop")
    assert lines.index("hook:traffic_stop") < lines.index("hook:relay_stop")
    assert "hook:workers_stop" in lines


def test_driver_loser_never_starts_traffic(tmp_path: Path) -> None:
    # Launch-directory collision: a second driver whose start is refused
    # must never start traffic; it still runs stop-first failure cleanup.
    import time
    stage = _stage(tmp_path)
    check = stage["check"]
    Path(check).write_text(
        "#!/usr/bin/env bash\n"
        'echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"\n'
        'if [ "$1" != "sample" ]; then\n'
        '  if [ "${STEP9_DRIVER_FAIL_AT:-}" = "$1" ]; then exit 1; fi\n'
        "  exit 0\n"
        "fi\n"
        "sleep 5\n",
        encoding="utf-8")
    winner_log = tmp_path / "order.log"
    with subprocess.Popen(_base_args(stage, tmp_path / "run"),
                           env=stage["env"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True) as winner:
        for _ in range(500):
            if winner_log.exists() and "check:sample " in winner_log.read_text(
                    encoding="utf-8"):
                break
            time.sleep(0.02)
        loser_env = dict(stage["env"])
        loser_env["STEP9_DRIVER_TEST_LOG"] = str(tmp_path / "loser.log")
        loser_env["STEP9_DRIVER_FAIL_AT"] = "start"
        loser = subprocess.run(_base_args(stage, tmp_path / "run"),
                               env=loser_env, capture_output=True, text=True,
                               check=False, timeout=60)
        winner_out, _ = winner.communicate(timeout=60)
    assert loser.returncode != 0
    loser_lines = [line.split(" ", 1)[0]
                   for line in (tmp_path / "loser.log").read_text(
                       encoding="utf-8").splitlines()]
    assert loser_lines == ["check:seed-budget", "check:start",
                           "hook:group_stop", "hook:traffic_stop",
                           "hook:workers_stop",
                           "hook:evidence", "check:finalize",
                           "hook:relay_stop", "hook:deps_stop"]
    assert "hook:traffic_start" not in loser_lines
    assert winner.returncode == 0, winner_out


def test_driver_watchdog_outlives_short_hook_timeout(tmp_path: Path) -> None:
    import time
    # The scheduled day watchdog runs under the absolute run deadline, not
    # the short hook timeout: a healthy stop slower than the hook grace
    # must still complete instead of being SIGKILLed minutes into the day.
    stage = _stage(tmp_path)
    check = stage["check"]
    Path(check).write_text(
        "#!/usr/bin/env bash\n"
        'echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"\n'
        'if [ "$1" != "sample" ]; then exit 0; fi\n'
        "sleep 0.5\n",
        encoding="utf-8")
    collector = stage["hooks"]["scheduled_stop"]
    Path(collector).write_text(
        "#!/usr/bin/env bash\n"
        'echo "hook:scheduled_stop" >> "$STEP9_DRIVER_TEST_LOG"\n'
        "sleep 6\n",
        encoding="utf-8")
    started = time.monotonic()
    result = subprocess.run(
        _base_args(stage, tmp_path / "run", hook_timeout_secs=3),
        env=stage["env"], capture_output=True, text=True, check=False,
        timeout=90)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 30
    lines = _lines(tmp_path)
    assert lines.index("hook:scheduled_stop") < \
        lines.index("hook:downstream_drain")
    assert lines.index("hook:downstream_drain") < \
        lines.index("hook:workers_stop")
    assert lines.index("hook:workers_stop") < \
        lines.index("check:validate")


def test_driver_run_deadline_secs_rejects_nonpositive(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    args = _base_args(stage, tmp_path / "run")
    dash = args.index("--")
    args = args[:dash] + ["--run-deadline-secs", "0"] + args[dash:]
    result = subprocess.run(
        args,
        env=stage["env"], capture_output=True, text=True, check=False,
        timeout=30)
    assert result.returncode == 2
    assert "run-deadline-secs" in result.stderr


def test_driver_relay_available_through_validate(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    result = subprocess.run(_base_args(stage, tmp_path / "run"),
                            env=stage["env"], capture_output=True, text=True, check=False,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    lines = _lines(tmp_path)
    assert lines.index("hook:relay_stop") > lines.index("check:validate")
    assert lines.index("hook:deps_stop") > lines.index("hook:relay_stop")


# --- Real integration: driver + real watchdog/drain-monitor ----------------
# The scheduled stop below is the actual step9_check.py watchdog binary
# (shortened poll/grace, fake podman binary on its own argv) and the drain
# phase runs the actual drain-monitor subcommand: a fake zero hook never
# stands in for either. DB-bound subcommands (seed/start/sample/finalize/
# validate) stay dispatcher stubs; the real run.json is pre-crafted.

import importlib.util as _importlib_util

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "python" / "src"))
_SPEC = _importlib_util.spec_from_file_location("step9_check_driver", STEP9)
assert _SPEC and _SPEC.loader
_step9 = _importlib_util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_step9)

FAKE_PODMAN = """#!/usr/bin/env bash
# Parent-equivalent podman double: state in $FAKEPODMAN_DIR.
echo "$*" >> "$FAKEPODMAN_DIR/calls.log"
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then
  if [[ "$*" == *HostConfig* ]]; then cat "$FAKEPODMAN_DIR/policy"; echo
  else cat "$FAKEPODMAN_DIR/running"; echo "sha256:image"; fi
elif [ "$1" = "update" ]; then echo "no" > "$FAKEPODMAN_DIR/policy"
elif [ "$1" = "stop" ]; then echo "false" > "$FAKEPODMAN_DIR/running"
else echo "unexpected podman call: $*" >&2; exit 1; fi
"""

SCHEDULED_REAL = """#!/usr/bin/env bash
echo "hook:scheduled_stop" >> "$STEP9_DRIVER_TEST_LOG"
exec "$STEP9_PYTHON" "$STEP9_REAL_BIN" watchdog \\
  --run-dir "$STEP9_REAL_RUN" --podman-bin "$STEP9_FAKE_PODMAN" \\
  --collector-container test-collector --deadline "$STEP9_DEADLINE" \\
  --max-sample-age-seconds 125 --systemd-unit test-unit \\
  --poll-seconds 1 --stop-grace-seconds 1
"""

DISPATCH_CHECK = """#!/usr/bin/env bash
if [ "$1" = "drain-monitor" ]; then
  exec "$STEP9_PYTHON" "$STEP9_REAL_BIN" "$@"
fi
echo "check:$1 $*" >> "$STEP9_DRIVER_TEST_LOG"
if [ "${STEP9_DRIVER_FAIL_AT:-}" = "$1" ]; then exit 1; fi
exit 0
"""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fake_podman(tmp_path: Path) -> tuple:
    box = tmp_path / "fakepodman"
    box.mkdir()
    (box / "running").write_text("true\n", encoding="utf-8")
    (box / "policy").write_text("unless-stopped\n", encoding="utf-8")
    (box / "calls.log").write_text("", encoding="utf-8")
    return box, _exe(box / "podman", FAKE_PODMAN)


def _real_run(tmp_path: Path, *, mode: str, tiny_spool: bool = False,
                core_ago: timedelta = timedelta(minutes=30)) -> Path:
    """Hand-crafted run dir for the real watchdog/monitor (no database).

    Wire/resource baselines are snapshots of this host's real collectors,
    so live-day/preflight envelopes evaluate clean; the tiny-spool variant
    pins a 1-byte spool cap that any real spool deterministically breaches.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    wire = _step9.collect_wire_facts(interfaces=["lo"], route_host=None)
    wire["boot_id"] = _step9._boot_id()
    res = _step9.collect_resource_facts(
        spool_path=str(run_dir), postgres_path=str(run_dir),
        db=None, metrics=None)
    run = {
        "schema": "step9-live-day-v2" if mode == "live-day"
        else "step9-preflight-v2",
        "mode": mode, "run_id": "testrun01",
        "core_start": _iso(now - core_ago),
        "boot_id": wire["boot_id"],
        "containers": {"collector": "test-collector",
                       "collector_image": "sha256:image"},
        "archive_interfaces": ["lo"], "archive_route_host": None,
        "spool_path": str(run_dir), "postgres_path": str(run_dir),
        "transfer_prior_bytes": 0,
        "wire_baseline": wire, "resource_baseline": res,
    }
    if mode == "live-day":
        res["archive"] = {"logical_bytes": 0, "objects": 0,
                          "physical_bytes": 0, "error": None}
        run.update({"archive_retained_cap_bytes": 2**40,
                    "transfer_cap_bytes": 2**50,
                    "s3_cap_attempts": 100000,
                    "spool_allocated_cap_bytes": 1 if tiny_spool else 2**40})
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return run_dir


def _real_stage(tmp_path: Path, run_dir: Path, deadline: str,
                drain_body: str) -> dict:
    """Stage with real scheduled-stop and real drain-monitor routing."""
    box, podman = _fake_podman(tmp_path)
    stage = _stage(tmp_path)
    stage["check"] = _exe(tmp_path / "dispatch-check.sh", DISPATCH_CHECK)
    stage["hooks"]["scheduled_stop"] = _exe(
        tmp_path / "hook-scheduled_stop.sh", SCHEDULED_REAL)
    stage["hooks"]["downstream_drain"] = _exe(
        tmp_path / "hook-downstream_drain.sh",
        "#!/usr/bin/env bash\n"
        'echo "hook:downstream_drain" >> "$STEP9_DRIVER_TEST_LOG"\n'
        + drain_body)
    env = dict(stage["env"])
    env["STEP9_PYTHON"] = sys.executable
    env["STEP9_REAL_BIN"] = str(STEP9)
    env["STEP9_REAL_RUN"] = str(run_dir)
    env["STEP9_DEADLINE"] = deadline
    env["STEP9_FAKE_PODMAN"] = str(podman)
    env["FAKEPODMAN_DIR"] = str(box)
    stage["env"] = env
    stage["podman_dir"] = box
    return stage


def test_real_planned_stop_reaches_monitored_drain(tmp_path: Path) -> None:
    import time
    # Planned core-end deadline: the real watchdog exits 0, the driver runs
    # the real drain-monitor over a live drain child, then workers stop and
    # terminal evidence follows. No group stop, no +5m stale wait.
    # Core started seconds ago: the sampler is inside startup grace with
    # no samples yet, so the watchdog polls until the exact deadline.
    run_dir = _real_run(tmp_path, mode="preflight",
                        core_ago=timedelta(seconds=30))
    deadline = _iso(datetime.now(UTC) + timedelta(seconds=4))
    stage = _real_stage(tmp_path, run_dir, deadline, "sleep 2\n")
    started = time.monotonic()
    result = subprocess.run(_base_args(stage, run_dir),
                            env=stage["env"], capture_output=True, text=True,
                            check=False, timeout=120)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 60
    assert _lines(tmp_path) == [
        "check:seed-budget", "check:start", "hook:traffic_start",
        "check:sample", "hook:scheduled_stop", "hook:downstream_drain",
        "hook:workers_stop", "hook:evidence",
        "check:finalize", "check:validate", "hook:relay_stop",
        "hook:deps_stop",
    ]
    assert "hook:group_stop" not in _lines(tmp_path)
    watchdog = json.loads((run_dir / "watchdog-outcome.json").read_text())
    assert watchdog["trigger"] == "deadline_reached"
    monitor = json.loads((run_dir / "drain-monitor.json").read_text())
    assert monitor["outcome"] == "drained"
    assert monitor["finished_at"] > monitor["started_at"]


def test_real_safety_stop_terms_group_first(tmp_path: Path) -> None:
    import time
    # Stale sampler with a future deadline: the real watchdog returns
    # nonzero at once (no 30s stop grace), and the driver terms the whole
    # group before traffic/workers stops.
    run_dir = _real_run(tmp_path, mode="preflight")
    samples = run_dir / "samples"
    samples.mkdir()
    (samples / "minute-0000.json").write_text(json.dumps(
        {"captured_utc": _iso(datetime.now(UTC)
                               - timedelta(seconds=600))}), encoding="utf-8")
    deadline = _iso(datetime.now(UTC) + timedelta(seconds=120))
    stage = _real_stage(tmp_path, run_dir, deadline, "sleep 2\n")
    started = time.monotonic()
    result = subprocess.run(_base_args(stage, run_dir),
                            env=stage["env"], capture_output=True, text=True,
                            check=False, timeout=120)
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 30
    lines = _lines(tmp_path)
    assert lines[:8] == ["check:seed-budget", "check:start",
                         "hook:traffic_start", "check:sample",
                         "hook:scheduled_stop", "hook:group_stop",
                         "hook:traffic_stop", "hook:workers_stop"]
    calls = (stage["podman_dir"] / "calls.log").read_text(encoding="utf-8")
    assert "\nstop " not in calls and not calls.startswith("stop ")
    outcome = json.loads((run_dir / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "sample_stale"
    assert outcome["stop"] == "delegated_to_group_stop"
    assert list((run_dir / "failures").glob("sample_stale-*.json"))


def test_real_cap_during_drain_stops_group(tmp_path: Path) -> None:
    import time
    # A 1-byte spool pin is deterministically breached by the real spool:
    # the real monitor fails the drain and the group stops first.
    run_dir = _real_run(tmp_path, mode="live-day", tiny_spool=True,
                        core_ago=timedelta(seconds=30))
    deadline = _iso(datetime.now(UTC) + timedelta(seconds=4))
    stage = _real_stage(tmp_path, run_dir, deadline, "sleep 5\n")
    started = time.monotonic()
    result = subprocess.run(_base_args(stage, run_dir),
                            env=stage["env"], capture_output=True, text=True,
                            check=False, timeout=120)
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 60
    lines = _lines(tmp_path)
    assert lines.index("hook:group_stop") < lines.index("hook:traffic_stop")
    assert lines.index("hook:traffic_stop") < lines.index("hook:relay_stop")
    monitor = json.loads((run_dir / "drain-monitor.json").read_text())
    assert monitor["outcome"] not in ("drained", None)


def _seed_live_day_run(run_dir: Path) -> None:
    """Minimal final-core-sample seed for live-day crafted runs."""
    run = json.loads((run_dir / "run.json").read_text())
    containers = run.get("containers", {}) or {}
    containers["worker_replicas"] = 1
    run["containers"] = containers
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    samples = run_dir / "samples"
    samples.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    (samples / "minute-1439.json").write_text(json.dumps({
        "slot": 1439,
        "captured_utc": _iso(now - timedelta(seconds=60)),
        "wire": {"conservative_host_wire_bytes": 0},
        "s3_attempts_cumulative": 21,
        "resources": {"archive_retained": {"current_bytes": 0}},
        "s3": {"producers": [None, None]},
    }), encoding="utf-8")


def test_real_instant_exit_still_checks_producers(tmp_path: Path) -> None:
    import time
    # A drain child already gone does not skip producer completeness:
    # with no terminal snapshots at all the real monitor fails at once
    # and the group stops first.
    run_dir = _real_run(tmp_path, mode="live-day",
                        core_ago=timedelta(seconds=30))
    _seed_live_day_run(run_dir)
    deadline = _iso(datetime.now(UTC) + timedelta(seconds=4))
    stage = _real_stage(tmp_path, run_dir, deadline, "true\n")
    started = time.monotonic()
    result = subprocess.run(_base_args(stage, run_dir),
                            env=stage["env"], capture_output=True, text=True,
                            check=False, timeout=120)
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 30
    lines = _lines(tmp_path)
    assert lines.index("hook:group_stop") < lines.index("hook:traffic_stop")
    monitor = json.loads((run_dir / "drain-monitor.json").read_text())
    assert monitor["outcome"] == "terminal_capture_missing"


EXEC_PODMAN = """#!/usr/bin/env bash
# Fake podman serving staged per-replica live files over exec cat, plus
# the state-based inspect/update/stop surface the watchdog needs.
echo "$*" >> "$FAKEPODMAN_DIR/calls.log"
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then
  if [[ "$*" == *HostConfig* ]]; then cat "$FAKEPODMAN_DIR/policy"; echo
  else cat "$FAKEPODMAN_DIR/running"; echo "sha256:image"; fi
elif [ "$1" = "update" ]; then echo "no" > "$FAKEPODMAN_DIR/policy"
elif [ "$1" = "stop" ]; then echo "false" > "$FAKEPODMAN_DIR/running"
elif [ "$1" = "exec" ]; then
  name=$(basename "$4")
  staged="$FAKEPODMAN_DIR/live-files/$name"
  if [ "$3" != "cat" ] || [ ! -f "$staged" ]; then exit 1; fi
  cat "$staged"
else echo "unexpected podman call: $*" >&2; exit 1; fi
"""


def test_real_live_workers_served_over_podman_path(tmp_path: Path) -> None:
    # Stopped collector (terminal file) plus live workers served over the
    # deployed per-replica podman path: the exec argv must use the exact
    # persistent live path, never the retired /tmp singleton.
    run_dir = _real_run(tmp_path, mode="live-day",
                        core_ago=timedelta(seconds=30))
    _seed_live_day_run(run_dir)
    run = json.loads((run_dir / "run.json").read_text())
    run["containers"]["python_worker"] = "test-worker"
    box = tmp_path / "execpodman"
    box.mkdir()
    (box / "running").write_text("true\n", encoding="utf-8")
    (box / "policy").write_text("unless-stopped\n", encoding="utf-8")
    (box / "calls.log").write_text("", encoding="utf-8")
    live_files = box / "live-files"
    live_files.mkdir()
    (live_files / "worker-1.json").write_text(json.dumps(
        {"archive": {"remote_attempts": {"get": 2}}}), encoding="utf-8")
    podman = _exe(box / "podman", EXEC_PODMAN)
    run["podman_bin"] = str(podman)
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    terminal = Path(run["spool_path"]) / ".control" / "terminal"
    terminal.mkdir(parents=True, exist_ok=True)
    (terminal / "collector.json").write_text(json.dumps({
        "schema": "clashlens-collector-terminal-v1",
        "producer": "collector", "process_id": "test-collector-pid",
        "process_started_at": run["core_start"],
        "captured_at": _iso(datetime.now(UTC)), "terminal": True,
        "operations": {},
    }), encoding="utf-8")
    deadline = _iso(datetime.now(UTC) + timedelta(seconds=4))
    stage = _real_stage(tmp_path, run_dir, deadline, "true\n")
    stage["env"]["FAKEPODMAN_DIR"] = str(box)
    stage["env"]["STEP9_FAKE_PODMAN"] = str(podman)
    result = subprocess.run(_base_args(stage, run_dir),
                            env=stage["env"], capture_output=True, text=True,
                            check=False, timeout=120)
    calls = (box / "calls.log").read_text(encoding="utf-8")
    assert "exec test-worker-1 cat /spool/.control/live/worker-1.json" \
        in calls
    assert "/tmp/clashlens-worker-operating.json" not in calls
    assert result.returncode != 0
    assert "hook:group_stop" in _lines(tmp_path)


def test_real_monitor_database_url_file_contract(tmp_path: Path) -> None:
    import subprocess as _subprocess
    # The monitor accepts a private URL file or argv URL (never both),
    # rejects group-readable files, and never echoes the value.
    run_dir = _real_run(tmp_path, mode="preflight",
                        core_ago=timedelta(seconds=30))
    marker = "postgresql://DBMARKER_SECRET@localhost:1/db"
    good = tmp_path / "db.url"
    good.write_text(marker + "\n", encoding="utf-8")
    os.chmod(good, 0o600)
    dead = _subprocess.Popen(["true"])
    assert dead.wait(timeout=30) == 0
    base = [sys.executable, str(STEP9), "drain-monitor",
            "--run-dir", str(run_dir), "--drain-pid", str(dead.pid),
            "--poll-seconds", "1", "--timeout-seconds", "10"]
    ok = _subprocess.run([*base, "--database-url-file", str(good)],
                         capture_output=True, text=True, check=False,
                         timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert "DBMARKER_SECRET" not in ok.stdout + ok.stderr
    both = _subprocess.run(
        [*base, "--database-url", marker,
         "--database-url-file", str(good)],
        capture_output=True, text=True, check=False, timeout=60)
    assert both.returncode != 0
    assert "DBMARKER_SECRET" not in both.stdout + both.stderr
    open_file = tmp_path / "open.url"
    open_file.write_text(marker + "\n", encoding="utf-8")
    os.chmod(open_file, 0o644)
    rejected = _subprocess.run(
        [*base, "--database-url-file", str(open_file)],
        capture_output=True, text=True, check=False, timeout=60)
    assert rejected.returncode != 0
    assert "DBMARKER_SECRET" not in rejected.stdout + rejected.stderr


def test_real_stall_during_drain_stops_group(tmp_path: Path) -> None:
    import time
    # A drain child that never exits hits the monitor timeout and the group
    # stops; unobserved archive activity stays bounded by the hook timeout.
    run_dir = _real_run(tmp_path, mode="preflight",
                        core_ago=timedelta(seconds=30))
    deadline = _iso(datetime.now(UTC) + timedelta(seconds=4))
    stage = _real_stage(tmp_path, run_dir, deadline, "exec sleep 60\n")
    started = time.monotonic()
    result = subprocess.run(
        _base_args(stage, run_dir, hook_timeout_secs=8),
        env=stage["env"], capture_output=True, text=True, check=False,
        timeout=120)
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 30
    lines = _lines(tmp_path)
    assert lines.index("hook:group_stop") < lines.index("hook:traffic_stop")
    monitor = json.loads((run_dir / "drain-monitor.json").read_text())
    assert monitor["outcome"] == "drain_timeout"
