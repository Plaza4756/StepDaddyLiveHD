import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from StepDaddyLiveHD.events import (
    Event, EventChannel, EventSettings, ScheduleParseError, SlotPlanner,
    guide_xmltv, legacy_schedule, parse_schedule, slots_playlist,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "schedule.html").read_text(encoding="utf-8")
UTC = timezone.utc


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def ev(title, start, channels=("10",), category="NBA"):
    return Event(day="d", category=category, time=start.strftime("%H:%M"), title=title,
                 start=start, channels=tuple(EventChannel(c, f"CH {c}") for c in channels))


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


# --- settings --------------------------------------------------------------

def test_settings_from_env():
    s = EventSettings.from_env({"EVENT_SLOTS": "5", "EVENT_INCLUDE": "nba|nfl",
                                "EVENT_DURATION_MINUTES": "200"})
    assert s.slots == 5 and s.duration == timedelta(minutes=200)
    assert s.in_slots(ev("x", at(8, 3), category="NBA 🏀"))
    assert not s.in_slots(ev("x", at(8, 3), category="Soccer"))
    assert not s.in_slots(ev("x", at(8, 3), channels=(), category="NBA"))
    assert not s.in_guide(ev("x", at(8, 3), category="Upcoming Events"))
    assert EventSettings.from_env({"EVENT_EXCLUDE": ""}).exclude is None
    with pytest.raises(ValueError):
        EventSettings.from_env({"EVENT_INCLUDE": "("})


# --- slot planning ---------------------------------------------------------

def planner(slots=2, **kw):
    return SlotPlanner(EventSettings(slots=slots, **kw))


def test_lowest_free_slot_and_overflow():
    p = planner(slots=2)
    a, b, c, d = ev("A", at(8, 18)), ev("B", at(8, 19)), ev("C", at(8, 20)), ev("D", at(8, 22))
    p.set_events([d, c, b, a], at(8, 12))
    got = {x.event.title: x.slot for x in p.assignments(at(8, 12))}
    assert got == {"A": 1, "B": 2, "D": 1}  # A ends 21:00 so D (22:00, lead 21:45) reuses slot 1
    assert [e.title for e in p.overflow] == ["C"]


def test_resolve_window_and_lead():
    p = planner(slots=1)
    p.set_events([ev("A", at(8, 18))], at(8, 12))
    assert p.resolve(1, at(8, 17, 44)) is None
    assert p.resolve(1, at(8, 17, 45)).event.title == "A"  # 15 min lead
    assert p.resolve(1, at(8, 20, 59)).event.title == "A"
    assert p.resolve(2, at(8, 18)) is None  # out of range


def test_live_event_keeps_slot_when_schedule_changes():
    p = planner(slots=2)
    a = ev("A", at(8, 18))
    p.set_events([a], at(8, 18, 30))
    assert p.current(1, at(8, 18, 30)).event.title == "A"
    # an earlier-starting event appears later; A must not move
    p.set_events([ev("Early", at(8, 17)), a], at(8, 18, 31))
    assert p.current(1, at(8, 18, 31)).event.title == "A"
    assert p.current(2, at(8, 18, 31)).event.title == "Early"


def test_watched_slot_holds_through_overtime():
    p = planner(slots=2)
    a, b = ev("A", at(8, 18)), ev("B", at(8, 21, 30))
    p.set_events([a, b], at(8, 18))
    t = at(8, 18)
    while t < at(8, 22):  # viewer polls slot 1 every few seconds past A's 21:00 end
        assert p.resolve(1, t).event.title == "A"
        t += timedelta(seconds=10)
    assert p.current(2, at(8, 22)).event.title == "B"  # B moved to slot 2
    # viewer leaves; after the hold window A is released
    assert p.current(1, at(8, 22, 5)) is None


def test_slot_hold_needs_continuous_viewing():
    p = planner(slots=1)
    p.set_events([ev("A", at(8, 18)), ev("B", at(8, 21, 15))], at(8, 18))
    t = at(8, 18)
    while t < at(8, 20, 55):  # viewer leaves before A's nominal end
        assert p.resolve(1, t).event.title == "A"
        t += timedelta(seconds=10)
    # a new viewer after the gap gets B, not the finished A
    assert p.resolve(1, at(8, 21, 5)).event.title == "B"
    assert p.overflow == []


def test_non_viewer_replan_does_not_extend_hold():
    p = planner(slots=1)
    p.set_events([ev("A", at(8, 18)), ev("B", at(8, 21, 15))], at(8, 18))
    t = at(8, 18)
    while t <= at(8, 21, 30):  # viewer polls into overtime, last poll 21:30:00
        assert p.resolve(1, t).event.title == "A"
        t += timedelta(seconds=10)
    p.assignments(at(8, 21, 31) + timedelta(seconds=30))  # /epg.xml fetch replans inside the hold window
    p.replan(at(8, 21, 31) + timedelta(seconds=30))
    # viewer returns after a gap longer than hold: A must not be re-pinned
    assert p.resolve(1, at(8, 21, 32) + timedelta(seconds=30)).event.title == "B"


def test_watched_slot_released_after_max_overtime():
    p = planner(slots=1, max_overtime=timedelta(minutes=30))
    p.set_events([ev("A", at(8, 18))], at(8, 18))
    t = at(8, 18)
    while t < at(8, 21, 29):
        assert p.resolve(1, t).event.title == "A"
        t += timedelta(seconds=30)
    assert p.resolve(1, at(8, 21, 31)) is None


# --- output ----------------------------------------------------------------

def test_slots_playlist():
    data = slots_playlist("http://localhost:3535", EventSettings(slots=2))
    assert data.splitlines() == [
        "#EXTM3U",
        '#EXTINF:-1 tvg-id="dlhd-event-01" tvg-name="DLHD Event 01" group-title="DLHD Events",DLHD Event 01',
        "http://localhost:3535/event/1.m3u8",
        '#EXTINF:-1 tvg-id="dlhd-event-02" tvg-name="DLHD Event 02" group-title="DLHD Events",DLHD Event 02',
        "http://localhost:3535/event/2.m3u8",
    ]


def test_guide_xmltv():
    events = parse_schedule(FIXTURE) + [ev("Overlap & co", at(8, 4), channels=("10",), category="NBA 🏀")]
    settings = EventSettings(slots=1)
    p = SlotPlanner(settings)
    p.set_events(events, at(8, 2))
    root = ET.fromstring(guide_xmltv(events, {"10": "NBA League Pass (24/7 name)"}, settings,
                                     p.assignments(at(8, 2))))
    channels = {c.get("id"): c.findtext("display-name") for c in root.iter("channel")}
    assert channels == {"dlhd-10": "NBA League Pass (24/7 name)", "dlhd-243": "Floracing",
                        "dlhd-454": "Sport TV3 Portugal", "dlhd-event-01": "DLHD Event 01"}
    progs = [(x.get("channel"), x.get("start"), x.get("stop"), x.findtext("title"))
             for x in root.iter("programme")]
    spurs = "🏀 NBA : San Antonio Spurs 🇺🇸 vs Portland Trail Blazers 🇺🇸"
    assert ("dlhd-10", "20261108030000 +0000", "20261108040000 +0000", spurs) in progs  # clipped
    assert ("dlhd-10", "20261108040000 +0000", "20261108070000 +0000", "Overlap & co") in progs
    assert ("dlhd-event-01", "20261108030000 +0000", "20261108060000 +0000", spurs) in progs
    assert not any("Boxing" in p[3] for p in progs)  # Upcoming Events excluded
