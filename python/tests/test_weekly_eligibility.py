from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_collector import _Client, _collector, _Spool, _Store

from clashlens import cli, weekly_eligibility
from clashlens.collector_db import CollectorIntent


@pytest.mark.parametrize("enabled", [False, True])
def test_weekly_collection_runs_only_when_explicitly_enabled(monkeypatch, enabled):
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    assert not collector.weekly_eligibility_enabled
    collector.weekly_eligibility_enabled = enabled
    weekly_calls = []

    async def idle(stop, *_args):
        await stop.wait()

    async def finish(stop, *_args):
        await asyncio.sleep(0.02)
        stop.set()

    async def weekly(_collector, stop):
        weekly_calls.append(True)
        await stop.wait()

    monkeypatch.setattr(collector, "_regular_loop", idle)
    monkeypatch.setattr(collector, "_intent_loop", idle)
    monkeypatch.setattr(collector, "_upload_loop", finish)
    monkeypatch.setattr(weekly_eligibility, "run", weekly)
    asyncio.run(collector.run(asyncio.Event(), health_host="127.0.0.1", health_port=0))
    assert weekly_calls == ([True] if enabled else [])


def test_cli_weekly_switch_is_opt_in(monkeypatch):
    monkeypatch.delenv("CLASHLENS_ENABLE_WEEKLY_ELIGIBILITY", raising=False)
    assert not cli.build_parser().parse_args(["collector"]).enable_weekly_eligibility
    assert cli.build_parser().parse_args(
        ["collector", "--enable-weekly-eligibility"]
    ).enable_weekly_eligibility
    monkeypatch.setenv("CLASHLENS_ENABLE_WEEKLY_ELIGIBILITY", "true")
    assert cli.build_parser().parse_args(["collector"]).enable_weekly_eligibility


def test_weekly_check_uses_regular_keys_and_only_the_needed_endpoints():
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    intent = CollectorIntent(
        "discovery_profile", datetime.now(UTC), 1, "#2PP", work_id=9,
        eligibility_recheck=True,
    )
    assert asyncio.run(collector.collect_intent(intent)) == "complete"
    assert [item.endpoint for item in store.handoffs] == ["profile"]
    assert client.seen_pools == [collector.regular_keys]


def test_resuming_a_durable_weekly_profile_does_not_fetch_it_again():
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    intent = CollectorIntent(
        "discovery_profile", datetime.now(UTC), 1, "#2PP", work_id=9,
        eligibility_recheck=True, profile_required=False,
    )
    assert asyncio.run(collector.collect_intent(intent)) == "complete"
    assert client.fetch_count == 0


def test_slow_weekly_collection_has_no_catchup_burst(monkeypatch):
    # A virtual clock advances through a slow request, without a long test sleep.
    clock = [0.0]
    starts = []
    stop = asyncio.Event()
    intent = CollectorIntent("discovery_profile", datetime.now(UTC), 1, "#2PP", work_id=1)

    async def database_call(*_args, **_kwargs):
        return intent

    async def collect(_intent):
        starts.append(clock[0])
        if len(starts) == 1:
            clock[0] += 10
        if len(starts) == 3:
            stop.set()

    async def wait(awaitable, *, timeout):
        awaitable.close()
        clock[0] += timeout
        raise TimeoutError

    monkeypatch.setattr(weekly_eligibility, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(weekly_eligibility.asyncio, "wait_for", wait)
    collector = SimpleNamespace(
        database=None, _database_call=database_call, collect_intent=collect,
        held_work=list,
    )
    asyncio.run(weekly_eligibility.run(collector, stop))
    assert starts == [0.0, 10.0, 12.0]
