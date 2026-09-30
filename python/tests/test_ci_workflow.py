from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CI_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
LOCKED_ENVIRONMENT = "/tmp/clashlens-ci-venv"
TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/clashlens"


def _workflow() -> dict:
    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _python_job() -> dict:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    python_job = jobs.get("python")
    assert isinstance(python_job, dict)
    return python_job


def test_python_job_uses_one_locked_environment_for_every_uv_step() -> None:
    job = _python_job()
    # The job-level environment applies to sync, Ruff, compile, and the
    # tests, so every uv step in the job resolves one locked environment.
    assert job.get("env", {}).get("UV_PROJECT_ENVIRONMENT") == LOCKED_ENVIRONMENT
    uv_steps = [
        step
        for step in job["steps"]
        if isinstance(step, dict) and "uv" in step.get("run", "")
    ]
    assert [step["name"] for step in uv_steps] == [
        "Sync locked dependencies",
        "Ruff",
        "Compile",
        "Tests",
    ]
    for step in uv_steps:
        # A step-level override would fragment the locked environment.
        assert "UV_PROJECT_ENVIRONMENT" not in step.get("env", {})


def test_python_job_runs_the_complete_suite_against_postgresql() -> None:
    job = _python_job()
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
    test_step = next(step for step in job["steps"] if step.get("name") == "Tests")
    assert test_step["working-directory"] == "python"
    assert test_step["run"] == (
        "uv run pytest -q\n"
        "PYTHONPATH=.. uv run pytest -q ../development/test_fixtures.py\n"
    )


def test_website_job_uses_node_24_lockfile_and_browser_acceptance_gate() -> None:
    job = _workflow()["jobs"]["website"]
    setup_node = next(
        step for step in job["steps"] if step.get("uses") == "actions/setup-node@v4"
    )
    assert setup_node["with"] == {
        "node-version": "24",
        "cache": "npm",
        "cache-dependency-path": "website/package-lock.json",
    }

    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert {"npm ci", "npm audit --omit=dev", "npm test"} <= set(commands)
    assert commands.index(
        "npx playwright install --with-deps chromium"
    ) < commands.index("npm run test:e2e")
    package = json.loads((CI_WORKFLOW.parents[2] / "website/package.json").read_text())
    assert package["scripts"]["test:e2e"] == "npm run build:verify && playwright test"
    # Normal browser coverage must not enqueue another full Python suite.
    assert "../dev check" not in commands
    full_audit = next(step for step in job["steps"] if step.get("run") == "npm audit")
    assert full_audit["continue-on-error"] is True
    cleanup = next(step for step in job["steps"] if "Clean up" in step.get("name", ""))
    assert cleanup["if"] == "always()"


def test_container_packaging_is_always_run_with_conditional_full_coverage() -> None:
    job = _workflow()["jobs"]["containers"]
    assert "if" not in job
    packaging = next(
        step for step in job["steps"] if "Verify packaged" in step.get("name", "")
    )
    assert "if" not in packaging
    assert "podman run --rm" in packaging["run"]
    assert (
        "uv run --locked pytest -q tests/test_ops_backup.py tests/test_support_wrapper.py"
        in packaging["run"]
    )
    full = next(step for step in job["steps"] if step.get("run") == "../dev check")
    assert full["if"] == "steps.coverage.outputs.full_check == 'true'"
    assert full["working-directory"] == "website"
    # Preserve existing required result names; the container result is new.
    assert _python_job()["name"] == "Python lint, compile, and PostgreSQL tests"
    assert (
        _workflow()["jobs"]["website"]["name"]
        == "Website Node 24 checks and Chromium E2E"
    )


@pytest.mark.parametrize(
    ("event", "path", "operation", "expected"),
    [
        ("push", "docs/architecture.md", "modify", True),
        ("pull_request", "python/src/clashlens/profile.py", "modify", False),
        ("pull_request", "website/app/routes/home.tsx", "modify", False),
        ("pull_request", "docs/architecture.md", "modify", False),
        *[
            ("pull_request", path, "modify", True)
            for path in (
                "Containerfile",
                "python/Containerfile",
                "development/PythonCheck.Containerfile",
                "website/Containerfile",
                "python/uv.lock",
                "website/package-lock.json",
                "python/pyproject.toml",
                "website/package.json",
                ".containerignore",
                "website/.dockerignore",
                "dev",
                "ops",
                "deploy/postgres/Containerfile",
                "deploy/migrations/0042_example.sql",
                "development/fixtures.py",
                ".github/workflows/ci.yml",
                "website/playwright.config.ts",
                "python/src/clashlens/cli.py",
                "python/src/clashlens/operating.py",
                "python/src/clashlens/db.py",
                "website/app/server/config.server.ts",
            )
        ],
        ("pull_request", "website/Containerfile", "rename", True),
        ("pull_request", "website/Containerfile", "delete", True),
    ],
)
def test_full_container_coverage_selection(
    tmp_path, event, path, operation, expected
) -> None:
    # The test container needs no Git installation. Simulate its NUL-separated
    # diff, including both sides of a rename as --no-renames requests.
    changed = [path, "renamed-file"] if operation == "rename" else [path]
    body = b"\0".join(value.encode() for value in changed) + b"\0"
    git = tmp_path / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert sys.argv[1:] == ['diff', '--no-renames', '--name-only', '-z', 'base...head']\n"
        f"sys.stdout.buffer.write({body!r})\n"
    )
    git.chmod(0o700)
    step = next(
        step
        for step in _workflow()["jobs"]["containers"]["steps"]
        if step.get("id") == "coverage"
    )
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        check=True,
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            BASE_SHA="base",
            HEAD_SHA="head",
            GITHUB_EVENT_NAME=event,
            GITHUB_OUTPUT=str(output),
        ),
    )
    assert output.read_text().strip() == f"full_check={str(expected).lower()}"


def test_full_coverage_selection_fails_when_the_diff_cannot_be_read(tmp_path) -> None:
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nexit 1\n")
    git.chmod(0o700)
    step = next(
        step
        for step in _workflow()["jobs"]["containers"]["steps"]
        if step.get("id") == "coverage"
    )
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        check=False,
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            BASE_SHA="bad",
            HEAD_SHA="head",
            GITHUB_EVENT_NAME="pull_request",
            GITHUB_OUTPUT=str(output),
        ),
    )
    assert result.returncode != 0
    assert not output.exists()


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
