from __future__ import annotations

from pathlib import Path

import yaml

CI_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
LOCKED_ENVIRONMENT = "/tmp/clashlens-ci-venv"
TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/clashlens"
EXPECTED_JOBS = {"go", "python", "postgres", "deploy-shell", "website"}


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


def test_ci_workflow_yaml_parses_and_lists_expected_jobs() -> None:
    workflow = _workflow()
    assert set(workflow["jobs"]) == EXPECTED_JOBS


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
            "--health-cmd \"pg_isready -U postgres -d clashlens\" "
            "--health-interval 10s --health-timeout 5s --health-retries 5"
        ),
    }
    test_step = next(step for step in job["steps"] if step.get("name") == "Tests")
    assert test_step["working-directory"] == "python"
    assert test_step["run"] == "uv run pytest -q"


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

    commands = [
        step["run"] for step in job["steps"] if isinstance(step, dict) and "run" in step
    ]
    assert commands == [
        "npm ci",
        "npm test",
        "npm run build:verify",
        "npx playwright install --with-deps chromium",
        "npm run test:e2e",
    ]
    assert all(
        step.get("working-directory") == "website"
        for step in job["steps"]
        if isinstance(step, dict) and "run" in step
    )
