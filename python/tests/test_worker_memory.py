from types import SimpleNamespace
from weakref import ref

import pytest

from clashlens import worker
from clashlens.db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    LeaseLost,
)


@pytest.mark.parametrize("entry", ["process_once", "process_job"])
@pytest.mark.parametrize("lose_lease", [False, True])
@pytest.mark.parametrize("libc", ["glibc", "missing_library", "missing_symbol"])
@pytest.mark.parametrize(
    ("work_type", "module", "completion"),
    [
        ("build_snapshot", worker.snapshots, "complete_snapshot"),
        ("build_analytics", worker.boundary_publication, "complete_analytics"),
        ("build_army_analytics", worker.army_ingestion, "complete_army_analytics"),
        ("reconcile_ranked_day", worker.reconciliation_db, "complete_reconciliation"),
    ],
)
def test_builds_release_freed_memory_without_changing_job_outcomes(
    monkeypatch, entry, lose_lease, libc, work_type, module, completion
) -> None:
    claim = SimpleNamespace(
        job_id=17,
        work_type=work_type,
        processing_version=PROCESSING_VERSION,
        domain_rule_version=DOMAIN_RULE_VERSION,
        analytics_rule_version=(
            ARMY_ANALYTICS_RULE_VERSION
            if work_type == "build_army_analytics"
            else ANALYTICS_RULE_VERSION
        ),
    )
    database = SimpleNamespace(
        claim_job=lambda **_kwargs: claim,
        renew_claim=lambda *_args, **_kwargs: None,
    )
    temporary_buffer = None
    trimmed = []
    library_loads = []

    class BuildBuffer:
        pass

    def complete(_database, _claim):
        nonlocal temporary_buffer
        buffer = BuildBuffer()
        temporary_buffer = ref(buffer)
        if lose_lease:
            raise LeaseLost("build lease expired")

    def trim(pad):
        # The build's objects must be freed before asking the allocator to trim.
        assert temporary_buffer is not None and temporary_buffer() is None
        trimmed.append(pad.value)
        return 0  # Nothing releasable is a normal result, not a job failure.

    def load_library(name):
        library_loads.append(name)
        if libc == "missing_library":
            raise OSError("glibc is unavailable")
        if libc == "missing_symbol":
            return SimpleNamespace()
        return SimpleNamespace(malloc_trim=trim)

    monkeypatch.setattr(module, completion, complete)
    monkeypatch.setattr(worker.ctypes, "CDLL", load_library)
    processor = worker.ObservationProcessor(database, archive=object())
    kwargs = {"job_id": claim.job_id} if entry == "process_job" else {}

    result = getattr(processor, entry)(owner="memory-test", **kwargs)

    assert result == worker.ProcessResult(
        claim.job_id, "lease_lost" if lose_lease else "processed"
    )
    is_build = work_type != "reconcile_ranked_day"
    assert library_loads == (["libc.so.6"] if is_build else [])
    assert trimmed == ([0] if is_build and libc == "glibc" else [])
