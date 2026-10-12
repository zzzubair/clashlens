"""Alerts tied to the 05:30 board target, upload age, failed work, stalled
work and failed deploys, delivered to a local HTTP server, never Discord.

On 8 Oct 2026 every Reset reading was in by 05:10, but the first frozen board
published at 06:26 and raw responses waited up to 76.5 minutes to reach the
archive, while the old alerts could not fire before about 06:05."""

import os
from datetime import UTC, datetime

import pytest
import test_alerts
from test_alerts import reset_at

from clashlens import alerts

runtime = test_alerts.runtime

PREFIX = "clashlens_collector_"


@pytest.fixture
def rt(runtime, monkeypatch):
    for name, value in runtime.holds.items():
        monkeypatch.setattr(alerts, name, value)
    return runtime


def at(rt, hour, minute=0) -> int:
    rt.now = datetime(2026, 10, 8, hour, minute, tzinfo=UTC).timestamp()
    return reset_at(rt.now)


def posts(rt, text: str) -> list[str]:
    return [p["content"] for p in rt.posts if text in p["content"]]


def reset_warnings(rt) -> list[str]:
    return posts(rt, "behind its 05:30")


def test_reset_stage_warnings_from_0512_until_the_board_is_readable(rt) -> None:
    reset = at(rt, 5, 11)
    rt.served = reset - 86400  # Only yesterday's board is out.
    rt.reset = f"{reset} 13251 13000 0 0"
    assert rt.run() == 0
    assert not rt.posts  # Collection has until 05:12.
    at(rt, 5, 12)
    assert rt.run() == 0
    assert "Reset collection has ended for 13,000 of 13,251 players" in (
        reset_warnings(rt)[0]
    )
    # Collection done; the inputs are still not frozen at 05:25.
    rt.reset = f"{reset} 13251 13251 0 0"
    at(rt, 5, 25)
    assert rt.run() == 0
    # One incident: the stage changes, the message does not repeat.
    assert len(reset_warnings(rt)) == 1
    # Frozen at 05:26 and readable from 05:29: the warning clears and
    # recovers once it has stayed clear for 15 minutes.
    rt.reset = f"{reset} 13251 13251 {reset + 1560} 0"
    at(rt, 5, 29)
    rt.served = reset
    assert rt.run() == 0
    at(rt, 5, 44)
    assert rt.run() == 0
    assert "recovered at 2026-10-08T05:29:00+00:00" in reset_warnings(rt)[-1]
    assert not posts(rt, "not readable by 05:30")


def test_a_reset_that_never_starts_warns_at_0512(rt) -> None:
    reset = at(rt, 5, 12)
    rt.served = reset - 86400
    rt.reset = f"{reset - 86400} 13251 13251 {reset - 85800} {reset - 85500}"
    assert rt.run() == 0
    assert "Reset collection has not started" in reset_warnings(rt)[0]


@pytest.mark.parametrize(
    ("left", "warned"),
    [
        # 3,000 finished a minute: 6,000 more need two minutes, done by 05:18.
        ((9000, 6000), False),
        # 300 a minute: 20,700 more need 69 minutes, finishing about 06:24.
        ((21000, 20700), True),
        # None finished: they never finish at this pace.
        ((21000, 21000), True),
    ],
)
def test_reset_work_projected_past_0525_warns_at_0515(rt, left, warned) -> None:
    reset = at(rt, 5, 15)
    rt.served = reset - 86400
    rt.reset = f"{reset} 13251 13251 0 0"
    rt.now -= 60
    rt.metrics[f"{PREFIX}reset_work_remaining"] = left[0]
    assert rt.run() == 0
    rt.now += 60
    rt.metrics[f"{PREFIX}reset_work_remaining"] = left[1]
    assert rt.run() == 0
    assert len(reset_warnings(rt)) == int(warned)
    if left == (21000, 20700):
        assert "20,700 Reset jobs are left and would finish about 06:24" in (
            reset_warnings(rt)[0]
        )


def test_a_board_not_readable_at_0530_alerts_then_recovers(rt) -> None:
    # The board check reads the board through the website's public page, so a
    # board saved as published but not shown there still alerts.
    reset = at(rt, 5, 29)
    rt.served = reset - 86400
    rt.reset = f"{reset} 13251 13251 {reset + 1500} 0"
    assert rt.run() == 0
    assert not rt.posts
    at(rt, 5, 30)
    assert rt.run() == 0
    late = posts(rt, "not readable by 05:30")
    assert len(late) == 1 and "the 2026-10-08 05:00 UTC board was not readable" in late[0]
    assert "First observed 2026-10-08T05:30:00+00:00" in late[0]
    rt.served = reset
    at(rt, 6, 26)
    assert rt.run() == 0
    at(rt, 6, 41)
    assert rt.run() == 0
    assert "recovered at 2026-10-08T06:26:00+00:00" in posts(rt, "not readable")[-1]


@pytest.mark.parametrize(
    ("website", "board_page"),
    [
        # The website, or the API behind it, could not answer.
        (503, None),
        # The page rendered without a board: the website rejected it.
        (200, None),
        # The board's own page answered with an error message.
        (302, "<h2>This standings page is unavailable</h2>"),
    ],
)
def test_a_board_the_website_cannot_show_by_0530_alerts(rt, website, board_page) -> None:
    reset = at(rt, 5, 30)
    rt.reset = f"{reset} 13251 13251 {reset + 1500} 0"
    rt.website_status = website
    shown, rt.board_page = rt.board_page, board_page or rt.board_page
    assert rt.run() == 1
    late = posts(rt, "not readable by 05:30")
    assert len(late) == 1
    assert "board was not readable at 05:30; the website check could not read it" in late[0]
    # The page shows the board again.
    rt.website_status, rt.board_page = 302, shown
    at(rt, 5, 40)
    assert rt.run() == 0
    at(rt, 5, 55)
    assert rt.run() == 0
    assert "recovered at 2026-10-08T05:40:00+00:00" in posts(rt, "not readable")[-1]


def test_a_board_without_a_public_website_address_misses_0530(rt) -> None:
    # Only the address visitors use proves the board readable.
    reset = at(rt, 5, 30)
    rt.reset = f"{reset} 13251 13251 {reset + 1500} 0"
    rt.config["public_origin"] = ""
    assert rt.run() == 1
    assert len(posts(rt, "not readable by 05:30")) == 1
    probe = next(args for args in rt.calls if "--reset" in args)
    assert probe[-2:] == ["0", "0"]


def test_a_failed_website_read_after_the_board_was_readable_stays_quiet(rt) -> None:
    reset = at(rt, 14)
    rt.reset = f"{reset} 13251 13251 {reset + 1200} {reset + 1500}"
    rt.website_status = 503
    assert rt.run() == 1
    assert not posts(rt, "not readable")


def test_the_record_keeps_when_the_website_showed_the_board(rt) -> None:
    reset = at(rt, 5, 27)
    assert rt.run() == 0
    # The Reset probe gets the board the website showed and when it read it,
    # not when the probe itself runs after the slower checks.
    probe = next(args for args in rt.calls if "--reset" in args)
    assert probe[-2:] == [str(reset), str(int(rt.now))]
    publication = next(args for args in rt.calls if "--publication" in args)
    assert publication[-2:] == ["2026-10", "7"]


def test_unreadable_reset_progress_warns_after_ten_minutes(rt) -> None:
    rt.reset = "not a number"
    start = rt.now
    for minute in range(11):
        rt.now = start + 60 * minute
        assert rt.run() == 1
    assert "Reset progress" in posts(rt, "unreadable")[0]


def test_an_upload_waiting_five_minutes_warns_and_fifteen_alerts(
    rt, monkeypatch
) -> None:
    monkeypatch.setattr(alerts, "WARNING_UPLOAD_WAIT", rt.warning_limits["WARNING_UPLOAD_WAIT"])
    age = f"{PREFIX}oldest_pending_upload_age_seconds"
    rt.metrics[age] = 299
    assert rt.run() == 0
    assert not rt.posts
    rt.metrics[age] = 300
    assert rt.run() == 0
    assert "a raw response has waited 5 minutes to be uploaded" in posts(rt, "Early warning")[0]
    rt.metrics[age] = 900
    assert rt.run() == 0
    assert len(posts(rt, "over 15 minutes to be uploaded")) == 1
    # The archive comes back and the backlog drains.
    rt.metrics[age] = 2
    assert rt.run() == 0
    rt.now += 900
    assert rt.run() == 0
    assert len(posts(rt, "recovered")) == 2


def test_failed_work_alerts_until_resolved_however_old(rt) -> None:
    # Failures from 1-2 Oct 2026 were still failed on 8 Oct; the 24-hour
    # failure alert had long recovered.
    rt.metrics |= {
        f"{PREFIX}failed_processing": 9,
        f"{PREFIX}oldest_failed_processing_age_seconds": 6 * 86400,
        f"{PREFIX}newest_failed_processing_age_seconds": 5 * 86400,
    }
    assert rt.run() == 0
    assert len(rt.posts) == 1
    assert "Outstanding: 9 processing jobs and 0 raw-response uploads" in rt.posts[0]["content"]
    assert "the oldest failed 144 hours ago" in rt.posts[0]["content"]
    rt.now += 86400 * 3
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.metrics[f"{PREFIX}failed_processing"] = 0
    assert rt.run() == 0
    rt.now += 900
    assert rt.run() == 0
    assert "recovered" in rt.posts[-1]["content"]


@pytest.mark.parametrize(
    "work", ["process_observation", "reconcile_ranked_day", "build_snapshot"]
)
def test_waiting_work_none_of_which_finishes_in_two_minutes_warns(rt, work) -> None:
    rt.metrics[f"{PREFIX}completed_jobs_2m"] = 40
    rt.metrics[f"{PREFIX}completed_job_{work}_2m"] = 40
    rt.metrics[f"{PREFIX}waiting_job_{work}_age_seconds"] = 600
    assert rt.run() == 0
    assert not rt.posts  # Old work, but some of it still finishes.
    del rt.metrics[f"{PREFIX}completed_job_{work}_2m"]
    rt.metrics[f"{PREFIX}waiting_job_{work}_age_seconds"] = 119
    assert rt.run() == 0
    assert not rt.posts
    # Two minutes waiting and none finished, however busy the threads.
    rt.metrics[f"{PREFIX}waiting_job_{work}_age_seconds"] = 120
    assert rt.run() == 0
    name = alerts.WORK_NAMES.get(work, work)
    assert f"{name} have waited 2 minutes and none finished" in (
        posts(rt, "Early warning")[0]
    )
    rt.metrics[f"{PREFIX}completed_job_{work}_2m"] = 3
    assert rt.run() == 0
    rt.now += 900
    assert rt.run() == 0
    assert "recovered" in rt.posts[-1]["content"]


def test_a_database_starting_for_five_minutes_warns(rt) -> None:
    rt.database = f"starting {int(rt.now) - 299}"
    assert rt.run() == 0
    assert not rt.posts
    rt.database = f"starting {int(rt.now) - 300}"
    assert rt.run() == 0
    assert "clashlens-postgres has been starting for 5 minutes" in rt.posts[0]["content"]
    # Crash recovery finished: the database is healthy again.
    rt.database = f"healthy {int(rt.now) - 600}"
    assert rt.run() == 0
    rt.now += 900
    assert rt.run() == 0
    assert "recovered" in rt.posts[-1]["content"]


def test_a_reset_window_player_over_twenty_minutes_still_alerts(rt) -> None:
    at(rt, 4, 55)
    for _minute in range(6):
        rt.leaderboard = "1 13000 1201"
        assert rt.run() == 0
        rt.now += 60
    assert len(posts(rt, "Live Leaderboard")) == 1


def test_a_failed_deploy_alerts_until_the_next_up_succeeds(rt) -> None:
    # ./ops up restarts the alert schedule after it fails.
    rt.state_dir.mkdir()
    intent = rt.state_dir / "alert-intent"
    intent.write_text("failed\n")
    os.utime(intent, (rt.now, rt.now))
    rt.post_status = 500
    assert rt.run() == 1
    assert not rt.posts
    # Discord failed once; the next scheduled check delivers it.
    rt.post_status = 204
    rt.now += 60
    assert rt.run() == 0
    assert len(posts(rt, "A deploy failed and left Clash Lens stopped")) == 1
    # Later checks while the stack is still down keep it open.
    rt.now += 60
    assert rt.run() == 0
    assert len(rt.posts) == 1
    # The next successful up records running.
    intent.write_text("running\n")
    os.utime(intent, (rt.now, rt.now))
    rt.now += 60
    assert rt.run() == 0
    rt.now += 900
    assert rt.run() == 0
    assert "recovered" in posts(rt, "A deploy failed")[-1]
