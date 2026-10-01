"""Exercise the ops command boundary with a disposable container-manager substitute."""

import configparser
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2] / "ops"
MODE_CONFIG = r"""
source "$1" help >/dev/null
MODE=$TEST_MODE
if [[ "$MODE" == fixture ]]; then load_fixture_config; else load_production_config; fi
"""

REGULAR_KEYS = ["normal-1", "normal-2", "normal-3", "normal-4", "extra-1", "extra-2"]
FAKE_SECRET_STORE = f"""#!{sys.executable}
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["secret", "create"]:
    target = Path(os.environ["SECRET_STORE"]) / args[-2]
    if args[-1] == "-":
        target.write_bytes(sys.stdin.buffer.read())
    else:
        shutil.copyfile(args[-1], target)
else:
    sys.exit(1)
"""


@pytest.fixture
def mode_config(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in (
        *(f"clashlens-{label}" for label in REGULAR_KEYS),
        "clashlens-interactive-1",
        "clashlens-hmac-current",
        "login",
        "google",
        "discord",
    ):
        secret = secrets / name
        secret.write_text("a" * 43 + "\n")
        secret.chmod(0o600)
    settings = {
        "POSTGRES_DB": "clashlens",
        "POSTGRES_USER": "clashlens",
        "POSTGRES_PASSWORD": "a" * 32,
        "CLASHLENS_COLLECTOR_DB_PASSWORD": "a" * 32,
        "CLASHLENS_WORKER_DB_PASSWORD": "a" * 32,
        "CLASHLENS_API_DB_PASSWORD": "a" * 32,
        "CLASHLENS_ARCHIVE_ENDPOINT": "storage.example",
        "CLASHLENS_ARCHIVE_SECURE": "true",
        "CLASHLENS_ARCHIVE_REGION": "test-region",
        "CLASHLENS_ARCHIVE_BUCKET": "evidence",
        "CLASHLENS_ARCHIVE_INSTANCE_ID": "test-instance",
        "CLASHLENS_ARCHIVE_MARKER_KEY": "archive-instance.json",
        "CLASHLENS_ARCHIVE_MARKER_HASH": "a" * 64,
        "CLASHLENS_ARCHIVE_MARKER_PAYLOAD_VERSION": "v1",
        "CLASHLENS_ARCHIVE_ACCESS_KEY": "test-access-key",
        "CLASHLENS_ARCHIVE_SECRET_KEY": "test-secret-key",
        "CLASHLENS_WORKER_ARCHIVE_ACCESS_KEY": "test-worker-access-key",
        "CLASHLENS_WORKER_ARCHIVE_SECRET_KEY": "test-worker-secret-key",
        "CLASHLENS_SPOOL_ROOT": str(spool),
        "CLASHLENS_OFFICIAL_API_ORIGIN": "https://api.example",
        "CLASHLENS_OFFICIAL_API_PROXY_URL": "http://proxy.example:3128",
        "CLASHLENS_API_KEY_HOST_DIR": str(secrets),
        "CLASHLENS_HMAC_SECRET_FILE": "/run/secrets/clashlens-hmac-current",
    }
    config = inputs / "app.env"
    config.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
    config.chmod(0o600)
    return dict(
        os.environ,
        OPS_ENV_FILE=str(config),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        PODMAN_BIN="forbidden-service-operation",
        SYSTEMCTL_BIN="forbidden-service-operation",
        LOGINCTL_BIN="forbidden-service-operation",
    )


@pytest.fixture
def runtime(tmp_path):
    state = tmp_path / "state" / "clashlens"
    state.mkdir(parents=True)
    image = "sha256:" + "a" * 64
    ops_digest = hashlib.sha256(OPS.read_bytes()).hexdigest().encode()
    source_fingerprint = hashlib.sha256(b"ops\0" + ops_digest + b"\n").hexdigest()
    manifest = state / "active-release.env"
    manifest.write_text(
        "RELEASE_MODE=production\n"
        f"SOURCE_FINGERPRINT={source_fingerprint}\n"
        + "".join(
            f"{name}_IMAGE={image}\n"
            for name in ("POSTGRES", "PYTHON", "COLLECTOR", "WEBSITE")
        )
    )
    manifest.chmod(0o600)
    manager = tmp_path / "manager"
    manager.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if "--format" in args:
    fmt = args[args.index("--format") + 1]
    if "managed" in fmt: print("ops")
    elif "Rootless" in fmt or "Running" in fmt: print("true")
    elif "Image" in fmt: print("sha256:" + "a" * 64)
    elif "Pod" in fmt or "Id" in fmt: print("test-pod")
elif "backup-list" in args:
    Path(os.environ["REMOTE_ACTIVITY"]).touch()
    print(os.environ["BACKUPS"])
elif "backup-push" in args:
    Path(os.environ["REMOTE_ACTIVITY"]).touch()
    sys.exit(int(os.environ.get("UPLOAD_EXIT", "0")))
elif "delete" in args:
    Path(os.environ["REMOTE_ACTIVITY"]).touch()
    if "--confirm" in args:
        boundary = args[args.index("before") + 1]
        rows = json.loads(os.environ["BACKUPS"])
        start = next(r["start_time"] for r in rows if r["backup_name"] == boundary)
        Path(os.environ["REMAINING"]).write_text(json.dumps([
            r["backup_name"] for r in rows if r["start_time"] >= start
        ]))
elif args[:2] == ["--user", "is-active"]:
    print("active")
"""
    )
    manager.chmod(0o700)
    git = tmp_path / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'ops\\0')\n"
    )
    git.chmod(0o700)
    env = dict(
        os.environ,
        PODMAN_BIN=str(manager),
        SYSTEMCTL_BIN=str(manager),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        REMAINING=str(tmp_path / "remaining"),
        REMOTE_ACTIVITY=str(tmp_path / "remote-activity"),
        PATH=f"{tmp_path}:{os.environ['PATH']}",
    )
    return env, Path(env["REMAINING"])


def backup_row(number, days, *, duration_hours=0):
    start = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return {
        "backup_name": f"base_{number:024X}",
        "start_time": start.isoformat(),
        "finish_time": (start + dt.timedelta(hours=duration_hours)).isoformat(),
    }


def run_ops(runtime, rows, *args, upload_exit=0):
    env, _ = runtime
    return subprocess.run(
        ["bash", str(OPS), *args],
        env=dict(env, BACKUPS=json.dumps(rows), UPLOAD_EXIT=str(upload_exit)),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "proxy_ip", [None, "", "10.89.14.2", "127.0.0.1", "::1", "::ffff:127.0.0.1"]
)
@pytest.mark.parametrize(
    ("mode", "login_enabled"),
    [("production", False), ("production", True), ("fixture", True)],
)
def test_website_environment_trusts_pod_unless_empty_or_explicit(
    tmp_path, mode_config, proxy_ip, mode, login_enabled
):
    environment_file = tmp_path / "state" / "clashlens" / "env" / "website.env"
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text("CLASHLENS_TRUSTED_PROXY_IP=192.0.2.1\n")
    with Path(mode_config["OPS_ENV_FILE"]).open("a") as config:
        if proxy_ip is not None:
            config.write(f"CLASHLENS_TRUSTED_PROXY_IP={proxy_ip}\n")
        if login_enabled:
            config.write(
                "CLASHLENS_PUBLIC_ORIGIN=https://clashlens.example\n"
                "CLASHLENS_LOGIN_SECRET_FILE=/run/secrets/login\n"
                "CLASHLENS_GOOGLE_CLIENT_ID=test-google-client\n"
                "CLASHLENS_GOOGLE_CLIENT_SECRET_FILE=/run/secrets/google\n"
                "CLASHLENS_DISCORD_CLIENT_ID=12345678901234567\n"
                "CLASHLENS_DISCORD_CLIENT_SECRET_FILE=/run/secrets/discord\n"
            )
    result = subprocess.run(
        [
            "bash",
            "-c",
            MODE_CONFIG + "write_environment\n",
            "environment-generation-test",
            str(OPS),
        ],
        env=dict(
            mode_config,
            TEST_MODE=mode,
        ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    environment = dict(
        line.split("=", 1) for line in environment_file.read_text().splitlines()
    )
    expected_proxy = "10.89.14.2" if proxy_ip is None else proxy_ip
    if mode == "production" and expected_proxy:
        assert environment["CLASHLENS_TRUSTED_PROXY_IP"] == expected_proxy
    else:
        assert "CLASHLENS_TRUSTED_PROXY_IP" not in environment
    assert environment["NODE_ENV"] == ("test" if mode == "fixture" else "production")
    assert environment["CLASHLENS_LOGIN_ENABLED"] == str(login_enabled).lower()
    assert environment["CLASHLENS_PYTHON_API_URL"] == "http://127.0.0.1:8000"
    assert environment_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["production", "fixture"])
@pytest.mark.parametrize("rate", [1, 25, 29])
def test_six_regular_keys_and_key_rate_reach_the_collector(tmp_path, mode_config, mode, rate):
    with Path(mode_config["OPS_ENV_FILE"]).open("a") as config:
        config.write(f"CLASHLENS_REQUESTS_PER_SECOND_PER_KEY={rate}\n")
    secrets = Path(mode_config["OPS_ENV_FILE"]).parent.parent / "secrets"
    for label in [*REGULAR_KEYS, "interactive-1"]:
        (secrets / f"clashlens-{label}").write_text(f"fixture-{label}\n")
    store = tmp_path / "podman-secrets"
    store.mkdir()
    podman = tmp_path / "podman"
    podman.write_text(FAKE_SECRET_STORE)
    podman.chmod(0o700)
    result = subprocess.run(
        [
            "bash",
            "-c",
            MODE_CONFIG + "prepare_secrets\nwrite_environment\n",
            "key-loading-test",
            str(OPS),
        ],
        env=dict(
            mode_config, TEST_MODE=mode, PODMAN_BIN=str(podman), SECRET_STORE=str(store)
        ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "fixture-" not in result.stdout + result.stderr
    regular = dict(
        entry.split("=", 1)
        for entry in (store / "clashlens-normal-api-keys").read_text().split(",")
    )
    assert list(regular) == REGULAR_KEYS
    interactive = (store / "clashlens-interactive-api-keys").read_text()
    if mode == "production":
        assert regular == {label: f"fixture-{label}" for label in REGULAR_KEYS}
        assert interactive == "interactive-1=fixture-interactive-1"
    collector_env = tmp_path / "state" / "clashlens" / "env" / "collector.env"
    assert (
        f"CLASHLENS_REQUESTS_PER_SECOND_PER_KEY={rate if mode == 'production' else 25}"
        in collector_env.read_text().splitlines()
    )


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("CLASHLENS_REGULAR_API_KEY_NAMES=normal-1,normal-2,normal-3", "4 to 7"),
        (
            "CLASHLENS_REGULAR_API_KEY_NAMES="
            + ",".join([*REGULAR_KEYS, "extra-3", "extra-4"]),
            "4 to 7",
        ),
        (
            "CLASHLENS_REGULAR_API_KEY_NAMES=normal-1,normal-2,normal-3,interactive-1",
            "4 to 7",
        ),
        (
            "CLASHLENS_REGULAR_API_KEY_NAMES=normal-1,normal-2,normal-3,normal-1",
            "twice",
        ),
        (
            "CLASHLENS_REGULAR_API_KEY_NAMES=normal-1,normal-2,normal-3,extra-3",
            "extra-3",
        ),
        ("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY=30", "1 to 29"),
        ("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY=0", "1 to 29"),
    ],
)
def test_production_refuses_unsafe_key_settings(mode_config, setting, message):
    with Path(mode_config["OPS_ENV_FILE"]).open("a") as config:
        config.write(setting + "\n")
    result = subprocess.run(
        ["bash", "-c", MODE_CONFIG, "key-setting-test", str(OPS)],
        env=dict(mode_config, TEST_MODE="production"),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize("mode", ["production", "fixture"])
def test_production_pod_address_is_pinned_inside_its_network(
    tmp_path, mode_config, mode
):
    result = subprocess.run(
        [
            "bash",
            "-c",
            MODE_CONFIG
            + r"""
RELEASE=([POSTGRES_IMAGE]=postgres [COLLECTOR_IMAGE]=collector [PYTHON_IMAGE]=python [WEBSITE_IMAGE]=website)
render_units
""",
            "unit-rendering-test",
            str(OPS),
        ],
        env=dict(mode_config, TEST_MODE=mode),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    units = tmp_path / "config" / "containers" / "systemd"
    network = configparser.ConfigParser(interpolation=None, strict=False)
    pod = configparser.ConfigParser(interpolation=None, strict=False)
    network.optionxform = pod.optionxform = str
    network.read(units / "clashlens.network")
    pod.read(units / "clashlens.pod")
    assert pod["Pod"]["Network"] == "clashlens.network"
    if mode == "fixture":
        assert not network.has_option("Network", "Subnet")
        assert not pod.has_option("Pod", "IP")
        return
    subnet = ipaddress.ip_network(network["Network"]["Subnet"])
    address = ipaddress.ip_address(pod["Pod"]["IP"])
    assert subnet == ipaddress.ip_network("10.89.14.0/24")
    assert address == ipaddress.ip_address("10.89.14.2")
    assert address in subnet
    assert network["Network"]["NetworkName"] == "clashlens-private"


@pytest.mark.parametrize(
    ("subnets", "accepted"),
    [("10.89.14.0/24 ", True), ("10.89.15.0/24 ", False), ("", False)],
)
def test_existing_network_with_another_subnet_is_refused(
    tmp_path, mode_config, subnets, accepted
):
    podman = tmp_path / "podman"
    podman.write_text(
        '#!/usr/bin/env bash\n[[ "$2" == exists ]] || printf "%s\\n" "$TEST_SUBNETS"\n'
    )
    podman.chmod(0o700)
    result = subprocess.run(
        [
            "bash",
            "-c",
            MODE_CONFIG + "guard_network_subnet\n",
            "network-guard-test",
            str(OPS),
        ],
        env=dict(
            mode_config, PODMAN_BIN=str(podman), TEST_SUBNETS=subnets, TEST_MODE="production"
        ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "podman network rm clashlens-private" in result.stderr


@pytest.mark.parametrize("mode", ["production", "fixture"])
@pytest.mark.parametrize(
    ("proxy_ip", "accepted"),
    [
        (None, True),
        ("", True),
        ("10.89.14.2", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("192.0.2.1", True),
        ("10.88.14.5", True),
        ("10.90.14.5", True),
        ("10.89.14.5", False),
        ("10.89.14.7", False),
        ("10.89.14.8", False),
        ("10.89.0.1", False),
        ("10.89.255.254", False),
        ("::ffff:10.89.14.8", False),
        ("::ffff:a59:e08", False),
        ("0:0:0:0:0:ffff:0a59:0e08", False),
        ("::ffff:10.89.14.2", True),
        ("::ffff:a59:e02", True),
        ("0:0:0:0:0:ffff:0a59:0e02", True),
    ],
)
def test_up_refuses_stale_proxy_before_changing_services(
    tmp_path, mode_config, mode, proxy_ip, accepted
):
    if proxy_ip is not None:
        with Path(mode_config["OPS_ENV_FILE"]).open("a") as config:
            config.write(f"CLASHLENS_TRUSTED_PROXY_IP={proxy_ip}\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            r"""
source "$1" help >/dev/null
MODE=$TEST_MODE
require_host() { :; }
load_release() { :; }
cleanup_stale_admin_state() { printf 'startup guards accepted\n'; exit 0; }
up_stack
""",
            "startup-guard-test",
            str(OPS),
        ],
        env=dict(mode_config, TEST_MODE=mode),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    accepted = accepted or mode == "fixture"
    assert (result.returncode == 0) is accepted, result.stderr
    if accepted:
        assert result.stdout == "startup guards accepted\n"
    else:
        assert result.stdout == ""
        assert "set CLASHLENS_TRUSTED_PROXY_IP=10.89.14.2" in result.stderr
        assert "or remove the line" in result.stderr
    assert not (tmp_path / "state" / "clashlens" / "mode").exists()
    assert not (tmp_path / "state" / "clashlens" / "env").exists()


def test_extra_manual_backups_do_not_shorten_recovery_window(runtime):
    rows = [backup_row(i, days) for i, days in enumerate((21, 14, 6, 1, 0.1), 1)]
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(runtime[1].read_text()) == [r["backup_name"] for r in rows[1:]]


def test_retention_preview_does_not_delete(runtime):
    result = run_ops(runtime, [backup_row(1, 20), backup_row(2, 10)], "backup-prune")
    assert result.returncode == 0, result.stderr
    assert not runtime[1].exists()


def test_backup_too_new_or_not_finished_before_boundary_is_kept(runtime):
    rows = [backup_row(1, 8, duration_hours=48), backup_row(2, 1)]
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode == 0, result.stderr
    assert not runtime[1].exists()


@pytest.mark.parametrize("rows", [[], [backup_row(1, 2)]])
def test_initial_window_never_deletes(runtime, rows):
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode == 0, result.stderr
    assert not runtime[1].exists()


def test_failed_upload_does_not_prune(runtime):
    result = run_ops(
        runtime, [backup_row(1, 21), backup_row(2, 14)], "backup", upload_exit=1
    )
    assert result.returncode != 0
    assert not runtime[1].exists()


def test_invalid_catalogue_refuses_deletion(runtime):
    rows = [backup_row(1, 21), backup_row(2, 14)]
    rows[1]["finish_time"] = "0001-01-01T00:00:00Z"
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode != 0
    assert not runtime[1].exists()


@pytest.mark.parametrize("rows", [[], [backup_row(1, 9)]])
def test_status_reports_missing_or_stale_remote_backup(runtime, rows):
    result = run_ops(runtime, rows, "backup-status")
    assert result.returncode != 0


def test_scheduled_backup_waits_for_operation_lock(runtime):
    env, _ = runtime
    lock_path = Path(env["XDG_STATE_HOME"]) / "clashlens" / "ops.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", str(OPS), "backup", "--wait-for-lock"],
            env=dict(env, BACKUPS=json.dumps([backup_row(1, 1)]), UPLOAD_EXIT="0"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.2)
            assert process.poll() is None
            fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=60)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    assert process.returncode == 0, stderr
    assert "Base backup uploaded" in stdout


def test_changed_checkout_is_rejected_before_remote_activity(runtime, tmp_path):
    env, _ = runtime
    checkout = tmp_path / "changed-checkout"
    checkout.mkdir()
    copied_ops = checkout / "ops"
    shutil.copy2(OPS, copied_ops)
    copied_ops.write_bytes(copied_ops.read_bytes() + b"\n")

    result = subprocess.run(
        ["bash", str(copied_ops), "backup-status"],
        env=dict(env, BACKUPS=json.dumps([backup_row(1, 1)])),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "release inputs changed after deployment" in result.stderr
    assert not Path(env["REMOTE_ACTIVITY"]).exists()


def test_backup_accepts_unchanged_release_across_locales(runtime, tmp_path):
    locales = subprocess.check_output(["locale", "-a"], text=True).splitlines()
    english = next((name for name in locales if name.lower() == "en_us.utf8"), None)
    if english is None:
        pytest.skip("cross-locale regression requires the en_US.utf8 host locale")

    env, _ = runtime
    env = dict(env, PATH=os.environ["PATH"], BACKUPS=json.dumps([backup_row(1, 1)]))
    checkout = tmp_path / "locale-checkout"
    checkout.mkdir()
    shutil.copy2(OPS, checkout / "ops")
    (checkout / "website").mkdir()
    for name in ("A", "a", "a-b"):
        (checkout / "website" / name).write_text(name)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=env)

    # The persisted release contract hashes relative paths and their file bytes
    # in byte order, independently of the service manager's language settings.
    paths = ["ops", "website/A", "website/a", "website/a-b"]
    records = b"".join(
        path.encode()
        + b"\0"
        + hashlib.sha256((checkout / path).read_bytes()).hexdigest().encode()
        + b"\n"
        for path in paths
    )
    manifest = Path(env["XDG_STATE_HOME"]) / "clashlens" / "active-release.env"
    lines = manifest.read_text().splitlines()
    manifest.write_text(
        "\n".join(
            f"SOURCE_FINGERPRINT={hashlib.sha256(records).hexdigest()}"
            if line.startswith("SOURCE_FINGERPRINT=")
            else line
            for line in lines
        )
        + "\n"
    )

    for language in ("C", english):
        activity = Path(env["REMOTE_ACTIVITY"])
        activity.unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", str(checkout / "ops"), "backup", "--wait-for-lock"],
            env=dict(env, LC_ALL=language),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Base backup uploaded" in result.stdout
        assert activity.exists()

    (checkout / "website" / "A").write_text("changed after deployment")
    activity.unlink()
    result = subprocess.run(
        ["bash", str(checkout / "ops"), "backup", "--wait-for-lock"],
        env=dict(env, LC_ALL=english),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "release inputs changed after deployment" in result.stderr
    assert not activity.exists()
