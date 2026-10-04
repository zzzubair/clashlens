from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

CI_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
LOCKED_ENVIRONMENT = "/tmp/clashlens-ci-venv"
TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/clashlens"


def _workflow() -> dict:
    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _expand(text: str, **github: str) -> str:
    """Expand GitHub ${{ }} expressions using only the operators ci.yml uses."""

    def value(match: re.Match) -> str:
        python = match[1].replace("&&", " and ").replace("||", " or ").strip()
        names = {"always": lambda: True, "github": SimpleNamespace(**github)}
        result = eval(python, {"__builtins__": {}, **names})
        return str(result).lower() if isinstance(result, bool) else str(result)

    return re.sub(r"\$\{\{(.*?)\}\}", value, text)


def _runs(condition: str, event: str) -> bool:
    return _expand(f"${{{{ {condition} }}}}", event_name=event) == "true"


def _python_job() -> dict:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    python_job = jobs.get("python-tests")
    assert isinstance(python_job, dict)
    return python_job


@pytest.fixture
def command_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    for directory in ("python", "website", "development", "bin"):
        (workspace / directory).mkdir(parents=True, exist_ok=True)
    (workspace / "development" / "test_fixtures.py").touch()
    (workspace / "website" / "package.json").write_bytes(
        (CI_WORKFLOW.parents[2] / "website" / "package.json").read_bytes()
    )
    calls = workspace / "calls.jsonl"
    substitute = (
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "command = Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'command': command, 'args': args, "
        "'cwd': os.getcwd(), 'environment': {key: os.environ.get(key) for key in "
        "('UV_PROJECT_ENVIRONMENT', 'CLASHLENS_TEST_DATABASE_URL', 'PYTHONPATH', "
        "'CLASHLENS_TEST_GROUP')}}) + '\\n')\n"
        "if [command, *args] == json.loads(os.environ['FAIL_COMMAND']):\n"
        "    sys.exit(23)\n"
        "if command == 'npm' and (args[:1] == ['run'] or args == ['test']):\n"
        "    name = args[1] if args[0] == 'run' else 'test'\n"
        "    script = json.loads(Path('package.json').read_text())['scripts'][name]\n"
        "    sys.exit(subprocess.call(['bash', '-e', '-o', 'pipefail', '-c', script]))\n"
        "if command == 'podman' and args[:1] == ['run']:\n"
        "    os.chdir(Path(os.environ['GITHUB_WORKSPACE']) / 'python')\n"
        "    image = args.index('clashlens-python-check:ci')\n"
        "    sys.exit(subprocess.call(args[image + 1:]))\n"
    )
    for command in (
        "uv",
        "npm",
        "npx",
        "sudo",
        "podman",
        "react-router",
        "tsc",
        "eslint",
        "prettier",
        "vitest",
        "node",
        "playwright",
    ):
        executable = workspace / "bin" / command
        executable.write_text(substitute)
        executable.chmod(0o700)
    dev = workspace / "dev"
    dev.write_text(substitute)
    dev.chmod(0o700)
    environment = dict(
        os.environ,
        PATH=f"{workspace / 'bin'}:{os.environ['PATH']}",
        COMMAND_LOG=str(calls),
        FAIL_COMMAND="[]",
        GITHUB_WORKSPACE=str(workspace),
    )
    return workspace, environment


def _run_step(step, command_workspace, job_environment=None):
    workspace, environment = command_workspace
    step_environment = {
        key: value.replace(
            "${{ matrix.group }}",
            (job_environment or {}).get("CLASHLENS_TEST_GROUP", "1"),
        )
        for key, value in step.get("env", {}).items()
    }
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=workspace / step.get("working-directory", "."),
        env={**environment, **(job_environment or {}), **step_environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _calls(command_workspace):
    log = command_workspace[0] / "calls.jsonl"
    return (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )


@pytest.mark.parametrize("group", ["1", "2", "3", "4"])
def test_python_groups_have_independent_postgresql_and_run_development_tests_once(
    command_workspace, group
) -> None:
    job = _python_job()
    assert "if" not in job
    assert job["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}
    assert job["env"]["CLASHLENS_TEST_DATABASE_URL"] == TEST_DATABASE_URL
    assert job["services"]["postgres"] == {
        "image": "postgres:18",
        "env": {
            "POSTGRES_DB": "clashlens",
            "POSTGRES_PASSWORD": "postgres",
            "POSTGRES_USER": "postgres",
        },
        "ports": ["5432:5432"],
        "options": (
            '--health-cmd "pg_isready -U postgres -d clashlens" '
            "--health-interval 10s --health-timeout 5s --health-retries 5"
        ),
    }
    for step in job["steps"]:
        if "run" in step:
            if "if" in step:
                assert step["name"] == "Development service tests"
                assert step["if"] == "matrix.group == 1"
                if group != "1":
                    continue
            result = _run_step(
                step, command_workspace, {**job["env"], "CLASHLENS_TEST_GROUP": group}
            )
            assert result.returncode == 0, result.stderr
    calls = _calls(command_workspace)
    assert [(call["command"], call["args"]) for call in calls] == [
        ("uv", ["sync", "--locked"]),
        ("uv", ["run", "ruff", "check", ".", "../development/test_fixtures.py"]),
        ("uv", ["run", "python", "-m", "compileall", "-q", "src"]),
        ("uv", ["run", "pytest", "-q", "--durations=30"]),
        *(
            [("uv", ["run", "pytest", "-q", "../development/test_fixtures.py"])]
            if group == "1"
            else []
        ),
    ]
    for call in calls:
        assert call["cwd"] == str(command_workspace[0] / "python")
        assert call["environment"]["UV_PROJECT_ENVIRONMENT"] == LOCKED_ENVIRONMENT
        assert call["environment"]["CLASHLENS_TEST_DATABASE_URL"] == TEST_DATABASE_URL
    assert calls[3]["environment"]["CLASHLENS_TEST_GROUP"] == group
    if group == "1":
        assert calls[-1]["environment"]["PYTHONPATH"] == ".."


def test_native_python_failure_stops_before_development_tests(
    command_workspace,
) -> None:
    step = next(step for step in _python_job()["steps"] if step.get("name") == "Tests")
    command_workspace[1]["FAIL_COMMAND"] = json.dumps(
        ["uv", "run", "pytest", "-q", "--durations=30"]
    )
    result = _run_step(step, command_workspace, _python_job()["env"])
    assert result.returncode == 23
    assert [(call["command"], call["args"]) for call in _calls(command_workspace)] == [
        ("uv", ["run", "pytest", "-q", "--durations=30"]),
    ]


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize(
    ("required", "groups", "name", "on_pull_requests"),
    [
        (
            "python",
            "python-tests",
            "Python lint, compile, and PostgreSQL tests",
            True,
        ),
        ("packaged-python", "packaged-python-tests", "Packaged Python tests", False),
    ],
)
def test_required_python_result_rejects_failed_cancelled_or_skipped_groups(
    command_workspace, result, required, groups, name, on_pull_requests
) -> None:
    job = _workflow()["jobs"][required]
    assert job["name"] == name
    assert job["needs"] == groups
    assert _runs(job["if"], "pull_request") == on_pull_requests
    assert _runs(job["if"], "push") and _runs(job["if"], "workflow_dispatch")
    step = job["steps"][0]
    assert step["env"]["RESULT"] == f"${{{{ needs.{groups}.result }}}}"
    step = {**step, "env": {"RESULT": result}}
    completed = _run_step(step, command_workspace)
    assert (completed.returncode == 0) == (result == "success")


def test_python_groups_collect_every_test_once() -> None:
    directory = CI_WORKFLOW.parents[2] / "python"
    environment = dict(os.environ)
    environment.pop("CLASHLENS_TEST_GROUP", None)

    def collect(group=None):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=directory,
            env={**environment, **({"CLASHLENS_TEST_GROUP": group} if group else {})},
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return [
            line
            for line in result.stdout.splitlines()
            if line.startswith("tests/") and "::" in line
        ]

    full = collect()
    groups = [collect(group) for group in ("1", "2", "3", "4")]
    assert full and all(groups)
    assert len(full) == len(set(full))
    combined = [node for nodes in groups for node in nodes]
    assert len(combined) == len(set(combined))
    assert sorted(combined) == sorted(full)
    durations = json.loads((directory / "tests" / "ci_test_durations.json").read_text())
    totals = [
        sum(
            durations.get(file.removeprefix("tests/"), 1.0)
            for file in {node.split("::", 1)[0] for node in nodes}
        )
        for nodes in groups
    ]
    assert max(totals) / min(totals) < 1.10


def test_invalid_python_group_fails_instead_of_silently_omitting_tests() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=CI_WORKFLOW.parents[2] / "python",
        env={**os.environ, "CLASHLENS_TEST_GROUP": "5"},
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR


@pytest.mark.parametrize(
    "failure",
    [[], ["npm", "audit"], ["react-router", "build"], ["playwright", "test"]],
)
def test_website_job_uses_node_24_lockfile_and_browser_acceptance_gate(
    command_workspace, failure
) -> None:
    job = _workflow()["jobs"]["website"]
    assert "if" not in job
    setup_node = next(
        step for step in job["steps"] if step.get("uses") == "actions/setup-node@v4"
    )
    assert setup_node["with"] == {
        "node-version": "24",
        "cache": "npm",
        "cache-dependency-path": "website/package-lock.json",
    }

    cleanup = next(step for step in job["steps"] if "Clean up" in step.get("name", ""))
    assert cleanup["if"] == "always()"
    command_workspace[1]["FAIL_COMMAND"] = json.dumps(failure)
    failed = False
    for step in job["steps"]:
        if "run" not in step or (failed and step.get("if") != "always()"):
            continue
        if step is not cleanup:
            assert "if" not in step
        result = _run_step(step, command_workspace)
        if result.returncode and not step.get("continue-on-error", False):
            failed = True
    assert failed == (failure in (["react-router", "build"], ["playwright", "test"]))
    calls = _calls(command_workspace)
    operations = [(call["command"], call["args"]) for call in calls]
    assert operations[:4] == [
        ("npm", ["ci"]),
        ("npm", ["audit", "--omit=dev"]),
        ("npm", ["audit"]),
        ("npm", ["test"]),
    ]
    browser_operations = [
        operation
        for operation in operations
        if operation[0] in ("npx", "react-router", "node", "playwright", "dev")
    ]
    expected = [
        ("react-router", ["typegen"]),
        ("npx", ["playwright", "install", "--with-deps", "chromium"]),
        ("react-router", ["build"]),
    ]
    if failure != ["react-router", "build"]:
        expected.extend(
            [
                ("node", ["./scripts/check-browser-assets.mjs"]),
                ("playwright", ["test"]),
            ]
        )
    assert browser_operations == expected
    for call in calls:
        if call["command"] != "podman":
            assert call["cwd"] == str(command_workspace[0] / "website")
    project = f"clashlens-dev-{hashlib.sha256(str(command_workspace[0]).encode()).hexdigest()[:10]}-e2e"
    assert [call["args"] for call in calls if call["command"] == "podman"] == [
        ["pod", "exists", project],
        ["pod", "rm", "--force", project],
        *[
            command
            for suffix in ("postgres-data", "archive-data", "spool")
            for command in (
                ["volume", "exists", f"{project}-{suffix}"],
                ["volume", "rm", f"{project}-{suffix}"],
            )
        ],
    ]


def test_pr_packaging_builds_only_python_and_runs_packaged_tests(
    command_workspace,
) -> None:
    job = _workflow()["jobs"]["containers"]
    assert job["name"] == "Container packaging and runtime checks"
    assert "if" not in job
    for step in job["steps"]:
        if "run" not in step:
            continue
        assert "if" not in step
        completed = _run_step(step, command_workspace)
        assert completed.returncode == 0, completed.stderr
    operations = [(call["command"], call["args"]) for call in _calls(command_workspace)]
    assert not any(command == "dev" for command, _ in operations)
    builds = [
        args
        for command, args in operations
        if command == "podman" and args[0] == "build"
    ]
    assert builds == [
        [
            "build",
            "--file",
            "development/PythonCheck.Containerfile",
            "--tag",
            "clashlens-python-check:ci",
            ".",
        ]
    ]
    assert operations[-2:] == [
        (
            "podman",
            [
                "run",
                "--rm",
                "--tmpfs",
                "/tmp:rw,mode=1777",
                "clashlens-python-check:ci",
                "uv",
                "run",
                "--locked",
                "pytest",
                "-q",
                "tests/test_ops_backup.py",
                "tests/test_ops_keep_running.py",
                "tests/test_support_wrapper.py",
            ],
        ),
        (
            "uv",
            [
                "run",
                "--locked",
                "pytest",
                "-q",
                "tests/test_ops_backup.py",
                "tests/test_ops_keep_running.py",
                "tests/test_support_wrapper.py",
            ],
        ),
    ]
    assert _calls(command_workspace)[-1]["cwd"] == str(command_workspace[0] / "python")
    assert (
        _workflow()["jobs"]["website"]["name"]
        == "Website Node 24 checks and Chromium E2E"
    )


def test_packaging_test_failure_fails_the_container_step(command_workspace) -> None:
    step = next(
        step
        for step in _workflow()["jobs"]["containers"]["steps"]
        if "Verify packaged" in step.get("name", "")
    )
    command_workspace[1]["FAIL_COMMAND"] = json.dumps(
        [
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/test_ops_backup.py",
            "tests/test_ops_keep_running.py",
            "tests/test_support_wrapper.py",
        ]
    )
    result = _run_step(step, command_workspace)
    assert result.returncode == 23
    assert [call["command"] for call in _calls(command_workspace)] == ["podman", "uv"]


PACKAGED_DATABASE_URL = (
    "postgresql://clashlens:clashlens-dev@127.0.0.1:5432/clashlens_test?sslmode=disable"
)


@pytest.mark.parametrize("group", ["1", "2", "3", "4"])
def test_packaged_python_groups_run_the_full_packaged_suite_once(
    command_workspace, group
) -> None:
    job = _workflow()["jobs"]["packaged-python-tests"]
    assert job["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}
    # The development stack's database image, user and test database.
    assert job["services"]["postgres"] == {
        "image": "postgres:18-alpine",
        "env": {
            "POSTGRES_DB": "clashlens_test",
            "POSTGRES_PASSWORD": "clashlens-dev",
            "POSTGRES_USER": "clashlens",
        },
        "ports": ["5432:5432"],
        "options": (
            '--health-cmd "pg_isready -U clashlens -d clashlens_test" '
            "--health-interval 10s --health-timeout 5s --health-retries 5"
        ),
    }
    for step in job["steps"]:
        if "run" not in step:
            continue
        if "if" in step:
            assert step["if"] == "matrix.group == 1"
            if group != "1":
                continue
        step = {**step, "run": step["run"].replace("${{ matrix.group }}", group)}
        completed = _run_step(step, command_workspace)
        assert completed.returncode == 0, completed.stderr
    operations = [(call["command"], call["args"]) for call in _calls(command_workspace)]
    image = "clashlens-python-check:ci"
    assert [args for command, args in operations if command == "podman"] == [
        [
            "build",
            "--file",
            "development/PythonCheck.Containerfile",
            "--tag",
            image,
            ".",
        ],
        *(
            [
                [
                    "run",
                    "--rm",
                    "--tmpfs",
                    "/tmp:rw,mode=1777",
                    image,
                    "sh",
                    "-c",
                    (
                        "uv run ruff check . ../development/*.py && uv run python -m "
                        "compileall -q src ../development && uv run pytest -q "
                        "../development/test_fixtures.py"
                    ),
                ]
            ]
            if group == "1"
            else []
        ),
        [
            "run",
            "--rm",
            "--network",
            "host",
            "--tmpfs",
            "/tmp:rw,mode=1777",
            "--env",
            f"CLASHLENS_TEST_DATABASE_URL={PACKAGED_DATABASE_URL}",
            "--env",
            f"CLASHLENS_TEST_GROUP={group}",
            image,
            "uv",
            "run",
            "pytest",
            "-q",
            "--durations=30",
        ],
    ]
    assert [args for command, args in operations if command == "uv"] == [
        *(
            [
                ["run", "ruff", "check", ".", "../development/test_fixtures.py"],
                ["run", "python", "-m", "compileall", "-q", "src", "../development"],
                ["run", "pytest", "-q", "../development/test_fixtures.py"],
            ]
            if group == "1"
            else []
        ),
        ["run", "pytest", "-q", "--durations=30"],
    ]
    assert ("sudo", ["apt-get", "install", "-y", "podman"]) in operations


def test_packaged_python_test_failure_fails_the_group(command_workspace) -> None:
    step = next(
        step
        for step in _workflow()["jobs"]["packaged-python-tests"]["steps"]
        if step.get("name") == "Packaged tests"
    )
    command_workspace[1]["FAIL_COMMAND"] = json.dumps(
        ["uv", "run", "pytest", "-q", "--durations=30"]
    )
    assert _run_step(step, command_workspace).returncode == 23


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_full_container_runtime_runs_only_on_main_pushes_and_manual_runs(
    command_workspace, event
) -> None:
    workflow = _workflow()
    # PyYAML's YAML 1.1 parser treats the GitHub Actions key "on" as True.
    assert workflow[True] == {
        "push": {"branches": ["main"]},
        "pull_request": None,
        "workflow_dispatch": None,
    }
    job = workflow["jobs"]["container-runtime"]
    assert "needs" not in job
    selected = _runs(job["if"], event)
    assert selected == (event != "pull_request")
    assert _runs(workflow["jobs"]["packaged-python-tests"]["if"], event) == selected
    if selected:
        for step in job["steps"]:
            if "run" in step:
                assert _run_step(step, command_workspace).returncode == 0
        assert _calls(command_workspace)[-1]["command"] == "dev"
        assert _calls(command_workspace)[-1]["args"] == ["check"]
    else:
        assert _calls(command_workspace) == []


def test_only_newer_pull_request_runs_cancel_older_ones() -> None:
    settings = _workflow()["concurrency"]

    def run(event, ref, sha):
        github = {"event_name": event, "ref": ref, "sha": sha}
        return (
            _expand(settings["group"], **github),
            _expand(settings["cancel-in-progress"], **github),
        )

    main_runs = [
        run("push", "refs/heads/main", "a" * 40),
        run("push", "refs/heads/main", "b" * 40),
        run("workflow_dispatch", "refs/heads/main", "c" * 40),
    ]
    pull_request_runs = [
        run("pull_request", "refs/pull/7/merge", "d" * 40),
        run("pull_request", "refs/pull/7/merge", "e" * 40),
    ]
    assert len({group for group, _ in main_runs + pull_request_runs[:1]}) == 4
    assert {cancel for _, cancel in main_runs} == {"false"}
    assert pull_request_runs[0] == pull_request_runs[1]
    assert pull_request_runs[0][1] == "true"


@pytest.mark.parametrize("exists", [True, False])
def test_browser_cleanup_only_removes_its_own_stack(tmp_path, exists) -> None:
    calls = tmp_path / "calls"
    podman = tmp_path / "podman"
    podman.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"with open({str(calls)!r}, 'a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"sys.exit({int(not exists)} if sys.argv[2] == 'exists' else 0)\n"
    )
    podman.chmod(0o700)
    step = next(
        step
        for step in _workflow()["jobs"]["website"]["steps"]
        if "Clean up" in step.get("name", "")
    )
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        check=True,
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            GITHUB_WORKSPACE=str(tmp_path),
        ),
    )
    project = (
        f"clashlens-dev-{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:10]}-e2e"
    )
    expected = [["pod", "exists", project]]
    if exists:
        expected.append(["pod", "rm", "--force", project])
    for suffix in ("postgres-data", "archive-data", "spool"):
        expected.append(["volume", "exists", f"{project}-{suffix}"])
        if exists:
            expected.append(["volume", "rm", f"{project}-{suffix}"])
    assert [json.loads(line) for line in calls.read_text().splitlines()] == expected
