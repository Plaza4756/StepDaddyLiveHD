from datetime import datetime, timezone
from pathlib import Path

import pytest

from StepDaddyLiveHD.events import ScheduleParseError, legacy_schedule, parse_schedule

FIXTURE = (Path(__file__).parent / "fixtures" / "schedule.html").read_text(encoding="utf-8")
UTC = timezone.utc


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


# --- parsing ---------------------------------------------------------------

def test_parse_main_schedule_only():
    events = parse_schedule(FIXTURE)
    assert [e.title for e in events] == [
        "GNCC Buckwheat 100",
        "Duke & Friends vs UNC",
        "🥊 Boxing : Fight live on Friday, December 11, 2026",
        "🏀 NBA : San Antonio Spurs 🇺🇸 vs Portland Trail Blazers 🇺🇸",
    ]  # the stale "Extra Schedule Backup" event is not included


def test_parse_fields():
    events = {e.title: e for e in parse_schedule(FIXTURE)}
    nba = events["🏀 NBA : San Antonio Spurs 🇺🇸 vs Portland Trail Blazers 🇺🇸"]
    assert nba.start == at(8, 3)
    assert nba.category == "NBA 🏀"
    assert nba.day == "Sunday 08th Nov 2026 - Schedule Time UK GMT"
    # stream-10 duplicate of id=10 is dropped
    assert [(c.id, c.name) for c in nba.channels] == [("10", "NBA League Pass"), ("454", "Sport TV3 Portugal")]

    duke = events["Duke & Friends vs UNC"]
    assert duke.category == "Men's College Basketball 🏀"
    assert duke.start == at(7, 23, 30)
    assert duke.channels == ()  # id=00 placeholder and watchextra.php are skipped


def test_parse_missing_container():
    with pytest.raises(ScheduleParseError):
        parse_schedule("<html><div class='schedule'></div></html>")


def test_legacy_schedule_shape():
    days = legacy_schedule(parse_schedule(FIXTURE))
    assert list(days) == ["Saturday 07th Nov 2026 - Schedule Time UK GMT",
                          "Sunday 08th Nov 2026 - Schedule Time UK GMT"]
    nba = days["Sunday 08th Nov 2026 - Schedule Time UK GMT"]["NBA 🏀"][0]
    assert nba["time"] == "03:00"
    assert nba["channels"][0] == {"channel_name": "NBA League Pass", "channel_id": "10"}
