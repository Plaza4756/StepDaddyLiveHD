"""HIDE_NSFW: #NSFW channels are left out of the channel list and their streams are refused."""
import asyncio
import sys
import types

import pytest

try:
    import rxconfig  # noqa: F401  (real config when reflex is installed)
except ImportError:
    sys.modules["rxconfig"] = types.SimpleNamespace(config=types.SimpleNamespace(
        api_url="http://localhost:3535", proxy_content=True, socks5="", hide_nsfw=True))

from fastapi.testclient import TestClient  # noqa: E402

from StepDaddyLiveHD import backend, step_daddy as step_daddy_module  # noqa: E402
from StepDaddyLiveHD.step_daddy import StepDaddy  # noqa: E402

CHANNELS = {"51": "ABC USA", "52": "Adult Swim", "501": "18+ (Player-01)", "502": "18+ (Player-02)"}
NSFW_IDS = {"501", "502"}


class Resp:
    def __init__(self, text):
        self.status_code, self.text, self.url = 200, text, "https://thedaddy.li/24-7-channels.php"


class FakeSession:
    def __init__(self, page="cards"):
        self.page = page  # "cards", "empty" (no channel cards, e.g. a block page) or "error"

    async def get(self, url, headers=None, timeout=None):
        assert url.endswith("/24-7-channels.php"), url
        if self.page == "error":
            raise ConnectionError("listing unreachable")
        if self.page == "empty":
            return Resp("<html>blocked</html>")
        return Resp("".join(
            f'<a class="card" href="/watch.php?id={cid}">\n<div class="card__title">{name}</div></a>'
            for cid, name in CHANNELS.items()))


def make(monkeypatch, hide, page="cards"):
    monkeypatch.setattr(step_daddy_module.config, "hide_nsfw", hide, raising=False)
    sd = StepDaddy()
    sd._session = FakeSession(page)
    return sd


def loaded(monkeypatch, hide):
    sd = make(monkeypatch, hide)
    asyncio.run(sd.load_channels())
    return sd


def route_client(monkeypatch, sd):
    """Serve the app against sd, with stream() faked; returns (client, ids streamed)."""
    streamed = []

    async def fake_stream(channel_id):
        streamed.append(channel_id)
        return "#EXTM3U\n"

    sd.stream = fake_stream
    monkeypatch.setattr(backend, "step_daddy", sd)
    return TestClient(backend.fastapi_app), streamed


def test_hidden_by_default_keeps_adult_swim(monkeypatch):
    sd = loaded(monkeypatch, hide=True)
    assert {c.id for c in sd.channels} == {"51", "52"}
    assert "Adult Swim" in [c.name for c in sd.channels]
    assert "18+" not in sd.playlist()
    assert all(sd.is_hidden(cid) for cid in NSFW_IDS)
    assert not sd.is_hidden("52")


def test_shown_when_switched_off(monkeypatch):
    sd = loaded(monkeypatch, hide=False)
    assert {c.id for c in sd.channels} == set(CHANNELS)
    assert sd.playlist().count("18+ (Player-") == 2
    assert not any(sd.is_hidden(cid) for cid in NSFW_IDS)


@pytest.mark.parametrize("hide", [True, False])
def test_stream_route(monkeypatch, hide):
    client, streamed = route_client(monkeypatch, loaded(monkeypatch, hide=hide))

    resp = client.get("/stream/501.m3u8")
    assert resp.status_code == (404 if hide else 200)
    assert client.get("/stream/52.m3u8").status_code == 200
    assert streamed == (["52"] if hide else ["501", "52"])


@pytest.mark.parametrize("page", [None, "error", "empty"])  # not loaded yet, load raised, no cards
def test_streams_refused_until_channel_list_loads(monkeypatch, page):
    sd = make(monkeypatch, hide=True, page=page or "cards")
    if page == "error":
        with pytest.raises(ConnectionError):
            asyncio.run(sd.load_channels())
    elif page == "empty":
        asyncio.run(sd.load_channels())
    client, streamed = route_client(monkeypatch, sd)

    for cid in ("501", "52"):
        resp = client.get(f"/stream/{cid}.m3u8")
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == str(backend.CHANNEL_RETRY_SECONDS)
    assert streamed == []

    sd._session = FakeSession()  # listing reachable again
    asyncio.run(sd.load_channels())
    assert client.get("/stream/501.m3u8").status_code == 404
    assert client.get("/stream/52.m3u8").status_code == 200


def test_streams_not_gated_when_switched_off(monkeypatch):
    client, streamed = route_client(monkeypatch, make(monkeypatch, hide=False))
    assert client.get("/stream/501.m3u8").status_code == 200
    assert streamed == ["501"]


def test_channel_reload_retries_after_failure(monkeypatch):
    class Stop(BaseException):
        pass

    loads, sleeps = [], []
    sd = make(monkeypatch, hide=True)

    async def fake_load():
        loads.append(1)
        if len(loads) == 1:
            raise ConnectionError("listing unreachable")
        sd._channels_loaded = True

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop  # end the loop after the post-success sleep

    sd.load_channels = fake_load
    monkeypatch.setattr(backend, "step_daddy", sd)
    monkeypatch.setattr(backend.asyncio, "sleep", fake_sleep)
    with pytest.raises(Stop):
        asyncio.run(backend.update_channels())
    assert len(loads) == 2
    assert sleeps[0] == backend.CHANNEL_RETRY_SECONDS
    assert 1700 <= sleeps[1] <= 2000


@pytest.mark.parametrize("page", ["error", "empty"])
def test_hidden_ids_stay_refused_after_a_bad_reload(monkeypatch, page):
    sd = loaded(monkeypatch, hide=True)
    sd._session = FakeSession(page)
    try:
        asyncio.run(sd.load_channels())
    except ConnectionError:
        pass
    client, streamed = route_client(monkeypatch, sd)
    assert client.get("/stream/501.m3u8").status_code == 404
    assert client.get("/stream/52.m3u8").status_code == 200
    assert streamed == ["52"]


@pytest.mark.parametrize("hide", [True, False])
def test_channel_reload_retries_soon_while_list_has_no_cards(monkeypatch, hide):
    class Stop(BaseException):
        pass

    sd = make(monkeypatch, hide=hide, page="empty")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        sd._session = FakeSession()  # the next load sees the real channel list
        if len(sleeps) == 2:
            raise Stop

    monkeypatch.setattr(backend, "step_daddy", sd)
    monkeypatch.setattr(backend.asyncio, "sleep", fake_sleep)
    with pytest.raises(Stop):
        asyncio.run(backend.update_channels())
    if hide:  # streams are gated until a list loads, so retry soon
        assert sleeps[0] == backend.CHANNEL_RETRY_SECONDS
    else:  # nothing is gated: keep upstream's refresh interval
        assert 1700 <= sleeps[0] <= 2000
    assert 1700 <= sleeps[1] <= 2000


@pytest.mark.parametrize("cid", ["501%3F", "501.php%3F", "501%23", "0501", "501%20", "501%C2%B2"])  # last: "501²", isdigit() but not int()-able
def test_padded_hidden_ids_are_refused(monkeypatch, cid):
    client, streamed = route_client(monkeypatch, loaded(monkeypatch, hide=True))
    assert client.get(f"/stream/{cid}.m3u8").status_code == 404
    assert streamed == []


def test_padded_ids_pass_through_when_switched_off(monkeypatch):
    client, streamed = route_client(monkeypatch, loaded(monkeypatch, hide=False))
    assert client.get("/stream/0501.m3u8").status_code == 200
    assert streamed == ["0501"]


def test_listing_without_channels_is_logged(monkeypatch, caplog):
    sd = make(monkeypatch, hide=True, page="empty")
    with caplog.at_level("WARNING", logger=step_daddy_module.logger.name):
        asyncio.run(sd.load_channels())
    assert "No channels found" in caplog.text
