from __future__ import annotations

from threading import Event

from clashlens_prototype.worker import ObservationProcessor


class NoClaimDatabase:
    def __init__(self) -> None:
        self.claim_calls = 0

    def claim_job(self, **_kwargs: object) -> None:
        self.claim_calls += 1
        raise AssertionError("shutdown must stop before claiming another job")


def test_worker_does_not_claim_after_shutdown_is_requested() -> None:
    database = NoClaimDatabase()
    stop_requested = Event()
    stop_requested.set()
    processor = ObservationProcessor(database, archive=object())

    results = processor.process_until_idle(
        owner="shutdown-worker",
        stop_requested=stop_requested,
    )

    assert results == []
    assert database.claim_calls == 0


def test_worker_does_not_claim_when_archive_readiness_is_false() -> None:
    database = NoClaimDatabase()
    processor = ObservationProcessor(database, archive=object())

    results = processor.process_until_idle(
        owner="archive-down-worker",
        readiness_check=lambda: False,
    )

    assert results == []
    assert database.claim_calls == 0
