"""DLHD schedule parsing.

Parses the live-event schedule from the DLHD homepage HTML.

Kept free of reflex/rxconfig imports so it can be unit tested on its own.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

# DLHD labels its times "Schedule Time UK GMT". They are treated as UTC.
# Unverified: whether DLHD shifts to BST (UTC+1) in summer. If it does, this
# should become ZoneInfo("Europe/London").
SCHEDULE_TZ = timezone.utc

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
