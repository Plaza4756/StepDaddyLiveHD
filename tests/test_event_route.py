"""/event/<slot>.m3u8 route: slot resolution, channel fallback and preference, deadline."""
import asyncio
import re
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
from StepDaddyLiveHD.step_daddy import UpstreamBlocked  # noqa: E402


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
    calls, failing, slow, blocked = [], set(), set(), set()

    async def fake_stream(channel_id):
        calls.append(channel_id)
        if channel_id in slow:
            await asyncio.sleep(5)
        if channel_id in failing:
            raise Exception("down")
        if channel_id in blocked:
            raise UpstreamBlocked(120.5)
        return playlist(channel_id)

    monkeypatch.setattr(backend, "slot_planner", planner)
    monkeypatch.setattr(backend, "_slot_streams", {})
    monkeypatch.setattr(backend, "_slot_fetches", {})
    monkeypatch.setattr(backend.step_daddy, "stream", fake_stream)
    return types.SimpleNamespace(client=TestClient(backend.fastapi_app), calls=calls,
                                 failing=failing, slow=slow, blocked=blocked)


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


def test_deadline_stops_trying_channels(env, monkeypatch):
    monkeypatch.setattr(backend, "EVENT_STREAM_DEADLINE", 0.2)
    monkeypatch.setattr(backend, "EVENT_STREAM_FAIR_SHARE", 0.1)
    env.slow.add("10")
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 502
    details = r.json()["details"]
    assert details[0] == "10: timed out"
    assert all(re.match(r"1[12]: skipped", d) for d in details[1:]) and len(details) == 2
    assert env.calls == ["10"]
    # the hung channel goes to the back, so the next refresh reaches 11
    env.calls.clear()
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 200 and "http://x/11/100.ts" in r.text
    assert env.calls == ["11"]


def test_stream_cache_fits_all_slots():
    assert backend.step_daddy._cache_size >= backend.event_settings.slots


def test_epg_lists_event_slots(env):
    r = env.client.get("/epg.xml")
    assert r.status_code == 200 and '<channel id="dlhd-event-02">' in r.text
    assert 'channel="dlhd-event-01"' in r.text  # the live event's programme


def test_failed_channel_goes_to_the_back(env):
    env.failing.add("10")
    env.client.get("/event/1.m3u8")  # 10 fails, 11 works
    env.failing.update({"11"})
    env.failing.discard("10")
    env.calls.clear()
    r = env.client.get("/event/1.m3u8")  # 11 (last working) fails; 12 before recently failed 10
    assert env.calls == ["11", "12"] and "http://x/12/100.ts" in r.text


def test_dlhd_block_stops_fallback_with_503(env):
    env.blocked.add("10")
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 503 and r.headers["retry-after"] == "120"
    assert env.calls == ["10"]  # the other channels share the block, so they aren't tried
    env.blocked.clear()
    env.calls.clear()
    env.client.get("/event/1.m3u8")
    assert env.calls == ["10"]  # not marked as failed: still tried first

def test_deadline_cut_short_channel_is_not_blamed(env, monkeypatch):
    # 10 fails slowly, using most of the deadline; 11 is healthy but gets the
    # rest and times out. Only 10 may go to the back, or 11 would be starved
    # behind 10 on every refresh.
    monkeypatch.setattr(backend, "EVENT_STREAM_DEADLINE", 0.4)
    monkeypatch.setattr(backend, "EVENT_STREAM_FAIR_SHARE", 0.2)
    real = backend.step_daddy.stream
    first_11 = [True]

    async def stream(channel_id):
        if channel_id == "10":
            env.calls.append(channel_id)
            await asyncio.sleep(0.3)
            raise Exception("down")
        if channel_id == "11" and first_11[0]:
            first_11[0] = False
            await asyncio.sleep(5)  # longer than what's left of the deadline
        return await real(channel_id)

    monkeypatch.setattr(backend.step_daddy, "stream", stream)
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 502
    assert r.json()["details"] == ["10: down", "11: cut short, 0.4s deadline reached"]
    env.calls.clear()
    r = env.client.get("/event/1.m3u8")
    assert r.status_code == 200 and "http://x/11/100.ts" in r.text
    assert env.calls == ["11"]  # 11 first now; 10, which really failed, is at the back
def test_concurrent_viewers_share_one_fetch(env, monkeypatch):
    # Overlapping requests for a slot share the fetch in progress: one upstream
    # request, one state update, the same playlist for everyone (no stale
    # result can be applied late, record a late failure, or return a late 502).
    fetches = []

    async def stream(channel_id):
        fetches.append(channel_id)
        await asyncio.sleep(0.2)
        return playlist(channel_id, 100 + len(fetches))

    monkeypatch.setattr(backend.step_daddy, "stream", stream)

    async def viewers():
        first = asyncio.create_task(backend.event_stream(1))
        await asyncio.sleep(0.05)  # the others arrive while the fetch is running
        rest = [asyncio.create_task(backend.event_stream(1)) for _ in range(2)]
        return await asyncio.gather(first, *rest)

    responses = asyncio.run(viewers())
    assert fetches == ["10"]
    assert {r.status_code for r in responses} == {200}
    assert len({r.body for r in responses}) == 1
    assert "DISCONTINUITY" not in responses[0].body.decode()


def test_next_request_after_a_fetch_starts_a_new_one(env, monkeypatch):
    fetches = []

    async def stream(channel_id):
        fetches.append(channel_id)
        return playlist(channel_id, 100 + len(fetches))

    monkeypatch.setattr(backend.step_daddy, "stream", stream)

    async def two_refreshes():
        first = await backend.event_stream(1)
        return first, await backend.event_stream(1)

    first, second = asyncio.run(two_refreshes())
    assert fetches == ["10", "10"]
    assert "MEDIA-SEQUENCE:101" in first.body.decode() and "MEDIA-SEQUENCE:102" in second.body.decode()


def test_viewer_disconnect_does_not_cancel_shared_fetch(env, monkeypatch):
    async def stream(channel_id):
        await asyncio.sleep(0.2)
        return playlist(channel_id)

    monkeypatch.setattr(backend.step_daddy, "stream", stream)

    async def one_leaves():
        leaver = asyncio.create_task(backend.event_stream(1))
        await asyncio.sleep(0.05)
        stayer = asyncio.create_task(backend.event_stream(1))
        await asyncio.sleep(0.05)
        leaver.cancel()  # client went away
        return await stayer

    assert asyncio.run(one_leaves()).status_code == 200


def test_event_switch_during_fetch_starts_a_new_one(env, monkeypatch):
    # Slot 1 moves from E1 to E2 while an E1 fetch is running. Viewers of E2
    # must not wait for (and get) E1; the late E1 fetch must not move the slot
    # back, and its viewers get E2's playlist.
    from StepDaddyLiveHD.events import Assignment
    now = datetime.now(timezone.utc)
    e1 = backend.slot_planner.current(1, now).event
    e2 = Event(day="d", category="NFL", time="00:00", title="Next", start=now,
               channels=(EventChannel("20", "Z"),))
    current = {"ev": e1}
    monkeypatch.setattr(backend.slot_planner, "resolve",
                        lambda slot, when: Assignment(slot, current["ev"], now, now + timedelta(hours=3)))
    fetches = []

    async def stream(channel_id):
        fetches.append(channel_id)
        await asyncio.sleep(0.2 if channel_id == "10" else 0.0)
        return playlist(channel_id)

    monkeypatch.setattr(backend.step_daddy, "stream", stream)

    async def switch():
        old = asyncio.create_task(backend.event_stream(1))
        await asyncio.sleep(0.05)
        current["ev"] = e2
        new = await backend.event_stream(1)
        return await old, new

    old, new = asyncio.run(switch())
    assert fetches == ["10", "20"]
    assert "http://x/20/" in new.body.decode() and old.body == new.body
    assert backend._slot_streams[1].source == (e2.key, "20")
    assert "DISCONTINUITY" not in new.body.decode()


def test_fetch_error_is_logged_and_next_request_retries(env, monkeypatch, caplog):
    calls = []
    real_rewrite = backend.SlotStream.rewrite

    def flaky_rewrite(self, playlist_text, source):
        calls.append(source)
        if len(calls) == 1:
            raise RuntimeError("rewrite broke")
        return real_rewrite(self, playlist_text, source)

    monkeypatch.setattr(backend.SlotStream, "rewrite", flaky_rewrite)

    async def run():
        with pytest.raises(RuntimeError):
            await backend.event_stream(1)
        await asyncio.sleep(0)  # let the done callback run
        return await backend.event_stream(1)

    with caplog.at_level("ERROR"):
        r = asyncio.run(run())
    assert r.status_code == 200 and len(calls) == 2
    assert any("Event slot fetch failed" in rec.message for rec in caplog.records)
