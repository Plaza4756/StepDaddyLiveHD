"""DLHD live-event support.

Parses the schedule from the DLHD homepage HTML, builds an XMLTV guide, and
assigns events to a fixed set of "event slot" channels so an IPTV app such as
Dispatcharr gets a stable channel list whose guide shows the game each slot
is carrying.

Kept free of reflex/rxconfig imports so it can be unit tested on its own.
"""
from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping
from xml.sax.saxutils import escape, quoteattr

logger = logging.getLogger(__name__)

# DLHD labels its times "Schedule Time UK GMT". They are treated as UTC.
# Unverified: whether DLHD shifts to BST (UTC+1) in summer. If it does, this
# should become ZoneInfo("Europe/London").
SCHEDULE_TZ = timezone.utc

EVENT_GROUP = "DLHD Events"


class ScheduleParseError(Exception):
    pass


@dataclass(frozen=True)
class EventChannel:
    id: str
    name: str


@dataclass(frozen=True)
class Event:
    day: str  # DLHD day title, e.g. "Saturday 07th Nov 2026 - Schedule Time UK GMT"
    category: str
    time: str  # "HH:MM" as listed
    title: str
    start: datetime  # aware, UTC
    channels: tuple[EventChannel, ...]

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.category}|{self.title}"

    @property
    def label(self) -> str:
        return f"{self.category}: {self.title}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_DAY_SPLIT = re.compile(r'<div class="schedule__dayTitle"[^>]*>')
_CAT_SPLIT = re.compile(r'<div class="schedule__catHeader"')
_EVENT_SPLIT = re.compile(r'<div class="schedule__event"')
_CAT_NAME = re.compile(r'<div class="card__meta"[^>]*>(.*?)</div>', re.S)
_TIME = re.compile(r'class="schedule__time"[^>]*data-time="(\d{1,2}):(\d{2})"')
_TITLE = re.compile(r'class="schedule__eventTitle"[^>]*>(.*?)</span>', re.S)
_LINK = re.compile(r'<a\b([^>]*)>(.*?)</a>', re.S)
_HREF = re.compile(r'\bhref="([^"]*)"')
# /watch.php?id=10, /stream/stream-10.php, ... but not /watchextra.php (other player).
_CHANNEL_ID = re.compile(r'(?:/watch\.php\?(?:[^"]*&)?id=|stream-)(\d+)')
_DAY_DATE = re.compile(r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})')
_TAGS = re.compile(r'<[^>]+>')
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAGS.sub("", fragment)).split())


def _parse_day(title: str) -> datetime | None:
    m = _DAY_DATE.search(title)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    try:
        return datetime(int(m.group(3)), month, int(m.group(1)), tzinfo=SCHEDULE_TZ)
    except ValueError:
        return None


def _parse_channels(fragment: str) -> tuple[EventChannel, ...]:
    channels: list[EventChannel] = []
    seen: set[str] = set()
    for attrs, body in _LINK.findall(fragment):
        href = _HREF.search(attrs)
        if not href:
            continue
        m = _CHANNEL_ID.search(href.group(1))
        if not m:
            continue
        channel_id = str(int(m.group(1)))
        # id=00 is DLHD's "Channel Not Listed #JustAsk" placeholder.
        if channel_id == "0" or channel_id in seen:
            continue
        seen.add(channel_id)
        channels.append(EventChannel(id=channel_id, name=_text(body) or f"CH-{channel_id}"))
    return tuple(channels)


def parse_schedule(page: str) -> list[Event]:
    """Parse the main schedule (``<div id="schedule">``) from the homepage HTML.

    The "Extra ..." sections further down the page are skipped: one of them
    carries a months-old backup schedule.
    """
    start = page.find('id="schedule"')
    if start < 0:
        raise ScheduleParseError('schedule container (id="schedule") not found')
    end = page.find("</section>", start)
    main = page[start:end if end >= 0 else len(page)]

    events: list[Event] = []
    for day_chunk in _DAY_SPLIT.split(main)[1:]:
        day_title = _text(day_chunk.split("</div>", 1)[0])
        day_date = _parse_day(day_title)
        if day_date is None:
            logger.warning("Skipping schedule day with unparseable title: %r", day_title)
            continue
        for cat_chunk in _CAT_SPLIT.split(day_chunk)[1:]:
            cat = _CAT_NAME.search(cat_chunk)
            category = _text(cat.group(1)) if cat else ""
            for ev_chunk in _EVENT_SPLIT.split(cat_chunk)[1:]:
                tm = _TIME.search(ev_chunk)
                title = _TITLE.search(ev_chunk)
                if not tm or not title:
                    continue
                hour, minute = int(tm.group(1)), int(tm.group(2))
                if hour > 23 or minute > 59:
                    continue
                events.append(Event(
                    day=day_title,
                    category=category,
                    time=f"{hour:02d}:{minute:02d}",
                    title=_text(title.group(1)),
                    start=day_date.replace(hour=hour, minute=minute),
                    channels=_parse_channels(ev_chunk),
                ))
    if not events:
        raise ScheduleParseError("no events found in schedule")
    return events


def legacy_schedule(events: Iterable[Event]) -> dict:
    """Shape events like the old schedule-generated.php JSON the /schedule page reads."""
    days: dict = {}
    for ev in events:
        days.setdefault(ev.day, {}).setdefault(ev.category, []).append({
            "event": ev.title,
            "time": ev.time,
            "channels": [{"channel_name": c.name, "channel_id": c.id} for c in ev.channels],
        })
    return days


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _regex(value: str | None, name: str) -> re.Pattern | None:
    if not value:
        return None
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"{name}: invalid regex {value!r}: {e}") from e


@dataclass
class EventSettings:
    slots: int = 30
    # Searched (case-insensitive) in "<category>: <title>".
    include: re.Pattern | None = None
    # "Upcoming Events" lists far-future events under today's date.
    exclude: re.Pattern | None = field(default_factory=lambda: re.compile(r"^Upcoming Events:", re.I))
    duration: timedelta = timedelta(hours=3)  # DLHD gives no end times
    lead: timedelta = timedelta(minutes=15)  # slot goes live this long before kickoff
    hold: timedelta = timedelta(minutes=2)  # a slot fetched this recently counts as watched
    max_overtime: timedelta = timedelta(hours=2)  # how long a watched event may outrun its end

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "EventSettings":
        s = cls()
        if env.get("EVENT_SLOTS"):
            s.slots = int(env["EVENT_SLOTS"])
            if not 1 <= s.slots <= 999:
                raise ValueError("EVENT_SLOTS must be between 1 and 999")
        s.include = _regex(env.get("EVENT_INCLUDE"), "EVENT_INCLUDE")
        if "EVENT_EXCLUDE" in env:
            s.exclude = _regex(env["EVENT_EXCLUDE"], "EVENT_EXCLUDE")
        if env.get("EVENT_DURATION_MINUTES"):
            s.duration = timedelta(minutes=int(env["EVENT_DURATION_MINUTES"]))
        if env.get("EVENT_LEAD_MINUTES"):
            s.lead = timedelta(minutes=int(env["EVENT_LEAD_MINUTES"]))
        return s

    def in_guide(self, ev: Event) -> bool:
        return bool(ev.channels) and not (self.exclude and self.exclude.search(ev.label))

    def in_slots(self, ev: Event) -> bool:
        return self.in_guide(ev) and (self.include is None or bool(self.include.search(ev.label)))


# ---------------------------------------------------------------------------
# Event slots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Assignment:
    slot: int
    event: Event
    start: datetime  # slot starts serving the event (kickoff - lead)
    end: datetime  # slot stops serving it (kickoff + duration, or later while watched)


def slot_tvg_id(slot: int) -> str:
    return f"dlhd-event-{slot:02d}"


def slot_name(slot: int) -> str:
    return f"DLHD Event {slot:02d}"


class SlotPlanner:
    """Assigns events to slots 1..N.

    Events are taken in start order and each gets the lowest slot that is free
    from (kickoff - lead) to (kickoff + duration). On every replan, events that
    are already live keep their slot, so schedule refreshes don't move them.
    A slot whose stream was fetched within ``hold`` keeps serving its event
    past the nominal end (up to ``max_overtime``), so a game in overtime is not
    switched away mid-watch; the next event planned there moves to another slot.
    """

    REPLAN_INTERVAL = timedelta(seconds=30)

    def __init__(self, settings: EventSettings):
        self.settings = settings
        self._events: list[Event] = []
        self._plan: dict[int, list[Assignment]] = {}
        self._serving: dict[int, Assignment] = {}
        self._last_access: dict[int, datetime] = {}
        self._planned_at: datetime | None = None
        self.overflow: list[Event] = []

    def set_events(self, events: Iterable[Event], now: datetime) -> None:
        self._events = sorted(
            (e for e in events if self.settings.in_slots(e)),
            key=lambda e: (e.start, e.category, e.title),
        )
        self.replan(now)

    def _watched(self, slot: int, now: datetime) -> bool:
        last = self._last_access.get(slot)
        return last is not None and now - last < self.settings.hold

    def _pins(self, now: datetime) -> dict[int, Assignment]:
        s = self.settings
        pins: dict[int, Assignment] = {}
        for slot in range(1, s.slots + 1):
            serving = self._serving.get(slot)
            # Cap against the nominal end; serving.end itself grows while held.
            if (serving and self._watched(slot, now)
                    and now < serving.event.start + s.duration + s.max_overtime):
                # Hold runs from the last viewer request, not from this replan
                # (replans also come from /epg.xml and schedule refreshes).
                pins[slot] = Assignment(slot, serving.event, serving.start,
                                        max(serving.end, self._last_access[slot] + s.hold))
                continue
            for a in self._plan.get(slot, []):
                if a.start <= now < a.end:
                    pins[slot] = a
                    break
        return pins

    def replan(self, now: datetime) -> None:
        s = self.settings
        pins = self._pins(now)
        pinned = {a.event.key for a in pins.values()}
        plan: dict[int, list[Assignment]] = {
            slot: [pins[slot]] if slot in pins else [] for slot in range(1, s.slots + 1)}
        busy_until = {slot: (pins[slot].end if slot in pins else None) for slot in plan}
        overflow: list[Event] = []
        for ev in self._events:
            if ev.key in pinned:
                continue
            start, end = ev.start - s.lead, ev.start + s.duration
            if end <= now:
                continue
            slot = next((n for n in plan if busy_until[n] is None or busy_until[n] <= start), None)
            if slot is None:
                overflow.append(ev)
                continue
            plan[slot].append(Assignment(slot, ev, start, end))
            busy_until[slot] = end
        self._plan = plan
        self.overflow = overflow
        self._planned_at = now

    def _maybe_replan(self, now: datetime) -> None:
        if self._planned_at is None or now - self._planned_at >= self.REPLAN_INTERVAL:
            self.replan(now)

    def current(self, slot: int, now: datetime) -> Assignment | None:
        self._maybe_replan(now)
        return next((a for a in self._plan.get(slot, []) if a.start <= now < a.end), None)

    def resolve(self, slot: int, now: datetime) -> Assignment | None:
        """Assignment a viewer of ``slot`` should get now; records the slot as watched."""
        if not 1 <= slot <= self.settings.slots:
            return None
        # Only an unbroken run of requests continues a viewing. After a gap the
        # slot forgets what it last served, so a finished event can't be re-pinned.
        if not self._watched(slot, now):
            self._serving.pop(slot, None)
        self._last_access[slot] = now
        a = self.current(slot, now)
        if a is None:
            self._serving.pop(slot, None)
        else:
            self._serving[slot] = a
        return a

    def assignments(self, now: datetime) -> list[Assignment]:
        self._maybe_replan(now)
        return [a for slot in sorted(self._plan) for a in self._plan[slot]]


_MEDIA_SEQUENCE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:\s*(\d+)\s*$", re.M)


@dataclass
class SlotStream:
    """Keeps one event slot's HLS playlist continuous across source switches.

    When a slot moves to another DLHD channel or event, the upstream media
    sequence jumps (often backwards). Players track live segments by sequence
    number, so a backwards jump can stall them. Upstream sequence numbers are
    shifted so they keep counting up, and the first segment from the new
    source gets an EXT-X-DISCONTINUITY tag. A backwards jump on the same
    source (upstream restart) is handled the same way.

    It also orders an event's channels for the next request: the channel that
    last worked first, channels that failed recently last.
    """
    FAILURE_TTL = 300.0  # seconds a failed channel stays at the back

    source: tuple[str, str] | None = None  # (event key, DLHD channel id)
    offset: int = 0  # emitted sequence = upstream sequence + offset
    next_seq: int = 0  # emitted sequence after the last segment handed out
    discontinuities: int = 0  # switches so far
    switch_seq: int | None = None  # upstream sequence where the last switch starts
    last_upstream: int | None = None  # upstream sequence of the previous rewrite
    failed: dict[str, float] = field(default_factory=dict)  # channel id -> failure time

    def order(self, event_key: str, channel_ids: list[str], now: float) -> list[str]:
        self.failed = {c: t for c, t in self.failed.items() if now - t < self.FAILURE_TTL}
        preferred = self.source[1] if self.source and self.source[0] == event_key else None
        return sorted(channel_ids, key=lambda c: (
            c in self.failed, c != preferred, self.failed.get(c, 0.0)))

    def record_failure(self, channel_id: str, now: float) -> None:
        self.failed[channel_id] = now

    def rewrite(self, playlist: str, source: tuple[str, str]) -> str:
        m = _MEDIA_SEQUENCE.search(playlist)
        if m is None:
            return playlist
        upstream = int(m.group(1))
        lines = playlist.split("\n")
        count = sum(1 for line in lines if line and not line.startswith("#"))
        restarted = (source == self.source and self.last_upstream is not None
                     and upstream < self.last_upstream)
        self.last_upstream = upstream
        self.failed.pop(source[1], None)
        if source != self.source or restarted:
            if self.source is not None:
                self.discontinuities += 1
                self.switch_seq = upstream
                self.offset = self.next_seq - upstream
            self.source = source
        self.next_seq = max(self.next_seq, upstream + self.offset + count)

        disc_seq, mark = self.discontinuities, None
        if self.switch_seq is not None:
            if self.switch_seq < upstream:
                self.switch_seq = None  # the switch segment has left the window
            else:
                disc_seq -= 1
                if self.switch_seq < upstream + count:
                    mark = self.switch_seq - upstream  # index of the first new segment

        out, extinf = [], 0
        for line in lines:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                out.append(f"#EXT-X-MEDIA-SEQUENCE:{upstream + self.offset}")
                if disc_seq:
                    out.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_seq}")
                continue
            if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                continue
            if line.startswith("#EXTINF"):
                if extinf == mark:
                    out.append("#EXT-X-DISCONTINUITY")
                extinf += 1
            out.append(line)
        return "\n".join(out)


def slots_playlist(api_url: str, settings: EventSettings) -> str:
    data = "#EXTM3U\n"
    for slot in range(1, settings.slots + 1):
        data += (f'#EXTINF:-1 tvg-id="{slot_tvg_id(slot)}" tvg-name="{slot_name(slot)}" '
                 f'group-title="{EVENT_GROUP}",{slot_name(slot)}\n'
                 f"{api_url}/event/{slot}.m3u8\n")
    return data


# ---------------------------------------------------------------------------
# XMLTV
# ---------------------------------------------------------------------------

def channel_tvg_id(channel_id: str) -> str:
    return f"dlhd-{channel_id}"


def _xmltv_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _programme(channel: str, start: datetime, stop: datetime, ev: Event) -> str:
    desc = f"{ev.category}. DLHD: " + ", ".join(f"{c.name} ({c.id})" for c in ev.channels)
    return (f"  <programme start={quoteattr(_xmltv_time(start))} stop={quoteattr(_xmltv_time(stop))} "
            f"channel={quoteattr(channel)}>\n"
            f"    <title>{escape(ev.title)}</title>\n"
            f"    <desc>{escape(desc)}</desc>\n"
            f"    <category>{escape(ev.category)}</category>\n"
            f"  </programme>\n")


def guide_xmltv(events: Iterable[Event], channel_names: Mapping[str, str],
                settings: EventSettings, assignments: Iterable[Assignment]) -> str:
    """XMLTV for DLHD channel ids ("dlhd-<id>") plus the event slots ("dlhd-event-NN").

    DLHD often lists one channel for several overlapping events, so each
    channel's programmes are clipped to end where the next one starts.
    """
    per_channel: dict[str, list[Event]] = {}
    names: dict[str, str] = {}
    for ev in events:
        if not settings.in_guide(ev):
            continue
        for c in ev.channels:
            per_channel.setdefault(c.id, []).append(ev)
            names.setdefault(c.id, channel_names.get(c.id) or c.name)

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n',
           '<tv generator-info-name="StepDaddyLiveHD">\n']
    for channel_id in sorted(per_channel, key=int):
        out.append(f"  <channel id={quoteattr(channel_tvg_id(channel_id))}>"
                   f"<display-name>{escape(names[channel_id])}</display-name></channel>\n")
    for slot in range(1, settings.slots + 1):
        out.append(f"  <channel id={quoteattr(slot_tvg_id(slot))}>"
                   f"<display-name>{escape(slot_name(slot))}</display-name></channel>\n")

    for channel_id in sorted(per_channel, key=int):
        evs = sorted(per_channel[channel_id], key=lambda e: (e.start, e.title))
        for i, ev in enumerate(evs):
            stop = ev.start + settings.duration
            nxt = next((e.start for e in evs[i + 1:] if e.start > ev.start), None)
            if nxt is not None and nxt < stop:
                stop = nxt
            out.append(_programme(channel_tvg_id(channel_id), ev.start, stop, ev))
    for a in assignments:
        out.append(_programme(slot_tvg_id(a.slot), max(a.event.start, a.start), a.end, a.event))
    out.append("</tv>\n")
    return "".join(out)
