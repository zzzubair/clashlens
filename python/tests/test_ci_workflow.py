from __future__ import annotations

from pathlib import Path

import yaml

CI_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
LOCKED_ENVIRONMENT = "/tmp/clashlens-ci-venv"
EXPECTED_JOBS = {"go", "python", "postgres", "deploy-shell"}


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
    # The job-level environment applies to sync, Ruff, compile, and the unit
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
        "Unit tests",
    ]
    for step in uv_steps:
        # A step-level override would fragment the locked environment.
        assert "UV_PROJECT_ENVIRONMENT" not in step.get("env", {})
