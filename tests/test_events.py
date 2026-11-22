import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from StepDaddyLiveHD.events import (
    slots_needed,
    Event, EventChannel, EventSettings, ScheduleParseError, SlotPlanner, SlotStream,
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


SAT_21 = "Saturday 21st Nov 2026 - Schedule Time UK GMT"


def _schedule_page(*days):
    """Homepage markup for (day title, {category: [(time, title, channel id)]}) blocks."""
    blocks = ""
    for day_title, categories in days:
        cats = ""
        for name, items in categories.items():
            evs = "".join(
                f'<div class="schedule__event"><div class="schedule__eventHeader">'
                f'<span class="schedule__time" data-time="{t}">{t}</span>'
                f'<span class="schedule__eventTitle">{title}</span></div>'
                f'<div class="schedule__channels"><a href="/watch.php?id={cid}">NBA League Pass</a></div></div>'
                for t, title, cid in items)
            cats += (f'<div class="schedule__category"><div class="schedule__catHeader">'
                     f'<div class="card__meta">{name}</div></div>{evs}</div>')
        blocks += (f'<div class="schedule__day"><div class="schedule__dayTitle">{day_title}</div>'
                   f'{cats}</div>')
    return f'<section><div class="schedule" id="schedule">{blocks}</div></section>'


def _one_day_page(categories):
    """Homepage markup with one day title, like DLHD's live page on 2026-11-21."""
    return _schedule_page((SAT_21, categories))


def test_parse_rolls_after_midnight_events_to_next_day():
    page = _one_day_page({
        "NBA Basketball 🏀": [("18:00", "Early game", "4"), ("00:30", "Knicks", "5"),
                              ("01:00", "Spurs vs Pelicans", "6"), ("03:00", "Kings", "7")],
        "Handball 🤾": [("19:15", "A", "8"), ("19:00", "B", "9")],  # out of order, same day
    })
    starts = {e.title: e.start for e in parse_schedule(page)}
    assert starts["Early game"] == at(21, 18)
    assert starts["Knicks"] == at(22, 0, 30)
    assert starts["Spurs vs Pelicans"] == at(22, 1)
    assert starts["Kings"] == at(22, 3)
    assert starts["A"] == at(21, 19, 15)
    assert starts["B"] == at(21, 19)

    days = legacy_schedule(parse_schedule(page))
    assert [e["event"] for e in days["Sunday 22 Nov 2026 - Schedule Time UK GMT"]["NBA Basketball 🏀"]] == [
        "Knicks", "Spurs vs Pelicans", "Kings"]
    assert [e["event"] for e in days[SAT_21]["NBA Basketball 🏀"]] == ["Early game"]


def test_rollover_edges():
    page = _one_day_page({
        "Exactly six hours": [("20:00", "a", "1"), ("14:00", "b", "2")],  # not more than 6h: same day
        "Late start": [("23:30", "c", "3"), ("00:00", "d", "4")],
        "Two rollovers": [("22:00", "e", "5"), ("02:00", "f", "6"), ("23:00", "g", "7"), ("01:00", "h", "8")],
    })
    starts = {e.title: e.start for e in parse_schedule(page)}
    assert (starts["a"], starts["b"]) == (at(21, 20), at(21, 14))
    assert (starts["c"], starts["d"]) == (at(21, 23, 30), at(22, 0))
    assert [starts[t] for t in "efgh"] == [at(21, 22), at(22, 2), at(22, 23), at(23, 1)]


def test_legacy_schedule_reuses_dlhd_title_for_rolled_events():
    sun_22 = "Sunday 22nd Nov 2026 - Schedule Time UK GMT"
    page = _schedule_page(
        (SAT_21, {"NBA Basketball 🏀": [("18:00", "Early game", "4"), ("01:00", "Spurs", "6")]}),
        (sun_22, {"NBA Basketball 🏀": [("20:00", "Sunday game", "7")]}),
    )
    days = legacy_schedule(parse_schedule(page))
    assert list(days) == [SAT_21, sun_22]
    assert [e["event"] for e in days[sun_22]["NBA Basketball 🏀"]] == ["Spurs", "Sunday game"]


def test_legacy_schedule_title_ignores_locale():
    import locale
    previous = locale.setlocale(locale.LC_TIME)
    for name in ("de_DE.UTF-8", "de_DE.utf8", "fr_FR.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no non-English locale installed")
    try:
        page = _one_day_page({"NBA Basketball 🏀": [("18:00", "Early game", "4"), ("01:00", "Spurs", "6")]})
        assert "Sunday 22 Nov 2026 - Schedule Time UK GMT" in legacy_schedule(parse_schedule(page))
    finally:
        locale.setlocale(locale.LC_TIME, previous)


def test_after_midnight_event_gets_a_slot_while_live():
    page = _one_day_page({"NBA Basketball 🏀": [("18:00", "Early game", "4"),
                                                ("01:00", "Spurs vs Pelicans", "6")]})
    settings = EventSettings(slots=2)
    p = SlotPlanner(settings)
    p.set_events(parse_schedule(page), at(22, 2, 37))
    live = {a.slot: a.event.title for a in p.assignments(at(22, 2, 37))}
    assert "Spurs vs Pelicans" in live.values()


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



def test_slots_needed_counts_peak_overlap():
    events = [ev("A", at(8, 18)), ev("B", at(8, 19)), ev("C", at(8, 20)), ev("D", at(8, 22))]
    s = EventSettings(slots=1)
    p = SlotPlanner(s)
    p.set_events(events, at(8, 17))
    assert len(p.overflow) == 2  # B and C overlap A
    assert slots_needed(events, s, at(8, 17)) == 3  # A, B, C run together; D reuses A's slot
    assert slots_needed(events, s, at(8, 23)) == 1  # only D is left
    assert slots_needed([], s, at(8, 17)) == 0



def test_restart_keeps_live_events_in_their_slots():
    # A planner running all day vs. one started fresh at 21:15 (a restart).
    # W and X have ended by then; Y and Z are live and must keep their slots,
    # or viewers are switched mid-game and the IPTV app's guide is wrong.
    events = [ev("W", at(8, 16)), ev("X", at(8, 18)), ev("Y", at(8, 20, 30)), ev("Z", at(8, 21, 5)),
              ev("F", at(8, 23))]
    running = planner(slots=4)
    running.set_events(events, at(8, 15))
    t = at(8, 15)
    while t <= at(8, 21, 15):  # replans as the day goes on, pinning events as they go live
        running.replan(t)
        t += timedelta(minutes=5)
    restarted = planner(slots=4)
    restarted.set_events(events, at(8, 21, 15))
    slots = lambda p: {a.event.title: a.slot for a in p.assignments(at(8, 21, 15))}
    assert slots(restarted) == slots(running)
    assert {"Y", "Z", "F"} <= set(slots(restarted))
    # Requests (e.g. /epg.xml) can replan before the schedule has loaded.
    early_request = planner(slots=4)
    early_request.assignments(at(8, 21, 14))
    early_request.set_events(events, at(8, 21, 15))
    assert slots(early_request) == slots(running)


def test_overflowed_event_gets_the_slot_once_it_frees_up():
    # B's window (14:55) overlaps A's (to 15:00) by five minutes, and C's
    # (17:55) overlaps B's. B must get slot 1 once A is over, not miss its
    # whole run, including after a restart; C follows once B is over.
    events = [ev("A", at(8, 12)), ev("B", at(8, 15, 10)), ev("C", at(8, 18, 10))]
    p = planner(slots=1)
    p.set_events(events, at(8, 14, 50))
    assert [e.title for e in p.overflow] == ["B"]  # C still fits after A
    p.replan(at(8, 15, 1))
    assert p.current(1, at(8, 15, 1)).event.title == "B"
    assert [e.title for e in p.overflow] == ["C"]  # now B holds the slot until 18:10
    p.replan(at(8, 17, 0))
    assert p.current(1, at(8, 17, 0)).event.title == "B"
    p.replan(at(8, 18, 11))
    assert p.current(1, at(8, 18, 11)).event.title == "C"
    restarted = planner(slots=1)
    restarted.set_events(events, at(8, 15, 30))
    assert restarted.current(1, at(8, 15, 30)).event.title == "B"


def test_default_slots_fit_a_busy_day():
    assert EventSettings().slots >= 160


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
    # Jellyfin files programmes under Sports only on an exact category match.
    assert all("Sports" in [c.text for c in x.iter("category")] for x in root.iter("programme"))


# --- slot playlist continuity ----------------------------------------------

def hls(seq, n, tag="a"):
    body = "".join(f"#EXTINF:6.0,\nhttp://x/{tag}/{seq + i}.ts\n" for i in range(n))
    return f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:{seq}\n{body}"


def segs(playlist):
    seq = int(re.search(r"MEDIA-SEQUENCE:(\d+)", playlist).group(1))
    disc = re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", playlist)
    out, marked = [], False
    for line in playlist.split("\n"):
        if line == "#EXT-X-DISCONTINUITY":
            marked = True
        elif line and not line.startswith("#"):
            out.append((seq + len(out), line.rsplit("/", 2)[1], marked))
            marked = False
    return out, int(disc.group(1)) if disc else 0


def test_slot_stream_passthrough_for_first_source():
    st = SlotStream()
    assert st.rewrite(hls(500, 3), ("A", "10")) == hls(500, 3)
    assert st.rewrite(hls(501, 3), ("A", "10")) == hls(501, 3)


def test_slot_stream_switch_keeps_counting_and_marks_discontinuity():
    st = SlotStream()
    st.rewrite(hls(500, 3), ("A", "10"))  # emitted 500..502
    out, disc = segs(st.rewrite(hls(20, 3, "b"), ("A", "454")))  # fallback channel, lower sequence
    assert out == [(503, "b", True), (504, "b", False), (505, "b", False)] and disc == 0
    out, disc = segs(st.rewrite(hls(21, 3, "b"), ("A", "454")))
    assert out == [(504, "b", False), (505, "b", False), (506, "b", False)] and disc == 1
    # a second switch (new event) keeps going from the last segment handed out
    out, disc = segs(st.rewrite(hls(900, 2, "c"), ("B", "12")))
    assert out == [(507, "c", True), (508, "c", False)] and disc == 1


def test_slot_stream_without_media_sequence_is_unchanged():
    assert SlotStream().rewrite("#EXTM3U\n", ("A", "1")) == "#EXTM3U\n"


def test_slot_stream_restart_on_same_source_keeps_counting():
    st = SlotStream()
    st.rewrite(hls(500, 3), ("A", "10"))  # emitted 500..502
    out, disc = segs(st.rewrite(hls(7, 3), ("A", "10")))  # upstream restarted
    assert out == [(503, "a", True), (504, "a", False), (505, "a", False)] and disc == 0


def test_slot_stream_channel_order():
    st = SlotStream()
    assert st.order("A", ["10", "11", "12"], now=0) == ["10", "11", "12"]
    st.rewrite(hls(1, 1), ("A", "12"))
    assert st.order("A", ["10", "11", "12"], now=0) == ["12", "10", "11"]  # last working first
    assert st.order("B", ["10", "11", "12"], now=0) == ["10", "11", "12"]  # other event: no preference
    st.record_failure("12", now=1)
    st.record_failure("10", now=2)
    assert st.order("A", ["10", "11", "12"], now=3) == ["11", "12", "10"]  # failed last, oldest first
    assert st.order("A", ["10", "11", "12"], now=400) == ["12", "10", "11"]  # failures expire
