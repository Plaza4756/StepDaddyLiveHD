"""/event/<slot>.m3u8 route: slot resolution, channel fallback and preference."""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

try:
    import rxconfig  # noqa: F401  (real config when reflex is installed)
except ImportError:
    sys.modules["rxconfig"] = types.SimpleNamespace(config=types.SimpleNamespace(
        api_url="http://localhost:3535", proxy_content=True, socks5=""))

from fastapi.testclient import TestClient  # noqa: E402

from StepDaddyLiveHD import backend  # noqa: E402
from StepDaddyLiveHD.events import Event, EventChannel, EventSettings, SlotPlanner  # noqa: E402


def playlist(channel_id, seq=100):
    return (f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{seq}\n"
            f"#EXTINF:6.0,\nhttp://x/{channel_id}/{seq}.ts\n")


@pytest.fixture
def env(monkeypatch):
    now = datetime.now(timezone.utc)
    ev = Event(day="d", category="NBA", time="00:00", title="Game", start=now - timedelta(minutes=10),
               channels=(EventChannel("10", "A"), EventChannel("11", "B"), EventChannel("12", "C")))
    planner = SlotPlanner(EventSettings(slots=2))
    planner.set_events([ev], now)
    calls, failing, slow = [], set(), set()

    async def fake_stream(channel_id):
        calls.append(channel_id)
        if channel_id in slow:
            await asyncio.sleep(5)
        if channel_id in failing:
            raise Exception("down")
        return playlist(channel_id)

    monkeypatch.setattr(backend, "slot_planner", planner)
    monkeypatch.setattr(backend, "_slot_streams", {})
    monkeypatch.setattr(backend.step_daddy, "stream", fake_stream)
    return types.SimpleNamespace(client=TestClient(backend.fastapi_app), calls=calls,
                                 failing=failing, slow=slow)


def test_empty_and_out_of_range_slots_404(env):
    assert env.client.get("/event/2.m3u8").status_code == 404  # no event planned there
    assert env.client.get("/event/99.m3u8").status_code == 404
    assert env.calls == []


def test_falls_back_and_prefers_last_working_channel(env):
    env.failing.add("10")
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 200 and "http://x/11/100.ts" in r.text
    assert env.calls == ["10", "11"]
    env.failing.clear()  # 10 recovers, but the slot sticks with 11
    env.calls.clear()
    r = env.client.get("/event/1.m3u8")
    assert env.calls == ["11"] and "http://x/11/100.ts" in r.text


def test_switch_renumbers_playlist(env):
    assert "MEDIA-SEQUENCE:100" in env.client.get("/event/1.m3u8").text  # channel 10
    env.failing.add("10")
    text = env.client.get("/event/1.m3u8").text  # falls back to 11, same upstream sequence
    assert "MEDIA-SEQUENCE:101" in text and "#EXT-X-DISCONTINUITY\n" in text


def test_all_channels_fail_502(env):
    env.failing.update({"10", "11", "12"})
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 502
    assert r.json()["details"] == ["10: down", "11: down", "12: down"]


def test_epg_lists_event_slots(env):
    r = env.client.get("/epg.xml")
    assert r.status_code == 200 and '<channel id="dlhd-event-02">' in r.text
    assert 'channel="dlhd-event-01"' in r.text  # the live event's programme
