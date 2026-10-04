"""Check when ./ops up leaves the collector, database, pod and network running."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2] / "ops"
OLD_COLLECTOR = "sha256:" + "1" * 64
NEW_COLLECTOR = "sha256:" + "2" * 64
POSTGRES = "sha256:" + "3" * 64
SAME_CONTENTS = '["sha256:aa"] ["python"] null ["PATH=/opt/venv/bin"] "10001:10001"'
KEPT = {"clashlens-collector", "clashlens-postgres", "clashlens-pod", "clashlens-network"}
FAKE_MANAGER = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as calls:
    calls.write(" ".join(args) + "\\n")
fmt = args[args.index("--format") + 1] if "--format" in args else ""
if args[:2] == ["image", "inspect"]:
    images = json.loads(os.environ["IMAGES"])
    if args[-1] not in images:
        sys.exit(125)
    print(images[args[-1]])
elif args[:2] == ["secret", "inspect"]:
    print((Path(os.environ["SECRET_STORE"]) / args[-1]).read_text())
elif args[:1] == ["inspect"] and fmt == "{{{{.Image}}}}":
    print(json.loads(os.environ["RUNNING"])[args[-1]].removeprefix("sha256:"))
elif args[:1] == ["inspect"]:
    print("healthy")
elif args[:2] == ["--user", "is-active"]:
    stopped = "--user stop " + args[2] + "\\n" in Path(os.environ["CALLS"]).read_text()
    print("inactive" if stopped or args[2] in os.environ.get("INACTIVE", "") else "active")
elif args[:2] == ["--user", "is-enabled"]:
    print("disabled")
"""
DEPLOY = r"""
source "$1" help >/dev/null
MODE=production PREFIX=clashlens
RELEASE=([COLLECTOR_IMAGE]=$NEW_COLLECTOR [POSTGRES_IMAGE]=$POSTGRES)
RESTART_COLLECTOR=$RESTART
keep_running_plan
stop_units
keep_running_check
keep_running_record
printf 'keep=%s collector=%s\n' "$KEEP_RUNNING" "${RELEASE[COLLECTOR_IMAGE]}"
"""


@pytest.fixture
def stack(tmp_path):
    units = tmp_path / "config" / "containers" / "systemd"
    units.mkdir(parents=True)
    env = tmp_path / "state" / "clashlens" / "env"
    env.mkdir(parents=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (units / "clashlens-collector.container").write_text(
        f"Image={OLD_COLLECTOR}\nMemory=2g\n"
        "Secret=clashlens-collector-database-url,type=mount,target=/run/secrets/database-url\n"
        "Secret=clashlens-normal-api-keys,type=env,target=CLASHLENS_NORMAL_API_KEYS\n"
    )
    (units / "clashlens-postgres.container").write_text(
        f"Image={POSTGRES}\nSecret=clashlens-postgres-password,type=mount,target=/run/secrets/p\n"
    )
    for name in ("clashlens.pod", "clashlens.network", "clashlens-postgres.volume"):
        (units / name).write_text(f"# {name}\n")
    (env / "collector.env").write_text("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY=40\n")
    (env / "postgres.env").write_text("POSTGRES_DB=clashlens\n")
    for name, value in (
        ("clashlens-collector-database-url", "postgresql://collector:old-password@db"),
        ("clashlens-normal-api-keys", "normal-1=old-key-value"),
        ("clashlens-postgres-password", "database-password"),
    ):
        (secrets / name).write_text(value)
    manager = tmp_path / "manager"
    manager.write_text(FAKE_MANAGER)
    manager.chmod(0o700)
    stack = {
        "tmp_path": tmp_path,
        "env": {
            "PATH": "/usr/bin:/bin",
            "PODMAN_BIN": str(manager),
            "SYSTEMCTL_BIN": str(manager),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "CALLS": str(tmp_path / "calls"),
            "SECRET_STORE": str(secrets),
            "NEW_COLLECTOR": NEW_COLLECTOR,
            "POSTGRES": POSTGRES,
            "RESTART": "false",
            "IMAGES": json.dumps(
                {OLD_COLLECTOR: SAME_CONTENTS, NEW_COLLECTOR: SAME_CONTENTS, POSTGRES: "pg"}
            ),
            "RUNNING": json.dumps(
                {"clashlens-collector": OLD_COLLECTOR, "clashlens-postgres": POSTGRES}
            ),
        },
    }
    # The first up has no record, so it restarts everything and writes one.
    first = deploy(stack)
    assert "Restarting the collector with the database, pod and network: no record" in first.stdout
    assert "keep=false" in first.stdout
    return stack


def deploy(stack, **overrides):
    calls = Path(stack["env"]["CALLS"])
    calls.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", "-c", DEPLOY, "keep-running-test", str(OPS)],
        env=dict(stack["env"], **overrides),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    result.stopped = {
        line.split()[2].removesuffix(".service")
        for line in calls.read_text().splitlines()
        if line.startswith("--user stop ") and line.endswith(".service")
    }
    return result


def assert_restarted(result, reason):
    assert f"Restarting the collector with the database, pod and network: {reason}" in result.stdout
    assert "keep=false" in result.stdout
    assert KEPT <= result.stopped


def test_unchanged_up_leaves_the_collector_running(stack):
    result = deploy(stack)
    assert "Leaving the collector, database, pod and network running" in result.stdout
    # The same contents under a new image ID stay pinned to the running image.
    assert f"keep=true collector={OLD_COLLECTOR}" in result.stdout
    assert not KEPT & result.stopped
    assert {"clashlens-api", "clashlens-worker", "clashlens-website"} <= result.stopped
    assert "--user disable --now clashlens.target" not in Path(stack["env"]["CALLS"]).read_text()


def test_changed_collector_image_restarts(stack):
    images = json.loads(stack["env"]["IMAGES"])
    images[NEW_COLLECTOR] = SAME_CONTENTS.replace("aa", "bb")
    assert_restarted(deploy(stack, IMAGES=json.dumps(images)), "the collector image changed")


def test_changed_collector_setting_restarts(stack):
    collector_env = stack["tmp_path"] / "state" / "clashlens" / "env" / "collector.env"
    collector_env.write_text("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY=35\n")
    assert_restarted(deploy(stack), "changed collector settings")
    # The restart records the new settings, so the next unchanged up keeps it.
    assert "keep=true" in deploy(stack).stdout


def test_changed_secret_restarts_without_printing_it(stack):
    secret = stack["tmp_path"] / "secrets" / "clashlens-normal-api-keys"
    secret.write_text("normal-1=new-key-value")
    result = deploy(stack)
    assert_restarted(result, "changed collector secrets")
    record = (stack["tmp_path"] / "state" / "clashlens" / "kept-services.env").read_text()
    for text in (result.stdout, result.stderr, record):
        assert "key-value" not in text
        assert "password" not in text


def test_restart_collector_flag_restarts(stack):
    assert_restarted(deploy(stack, RESTART="true"), "--restart-collector was given")


def test_stopped_database_restarts(stack):
    result = deploy(stack, INACTIVE="clashlens-postgres.service")
    assert_restarted(result, "clashlens-postgres is not running")
