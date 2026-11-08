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
    async def get(self, url, headers=None, timeout=None):
        assert url.endswith("/24-7-channels.php"), url
        return Resp("".join(
            f'<a class="card" href="/watch.php?id={cid}">\n<div class="card__title">{name}</div></a>'
            for cid, name in CHANNELS.items()))


def loaded(monkeypatch, hide):
    monkeypatch.setattr(step_daddy_module.config, "hide_nsfw", hide, raising=False)
    sd = StepDaddy()
    sd._session = FakeSession()
    asyncio.run(sd.load_channels())
    return sd


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
    sd = loaded(monkeypatch, hide=hide)
    streamed = []

    async def fake_stream(channel_id):
        streamed.append(channel_id)
        return "#EXTM3U\n"

    sd.stream = fake_stream
    monkeypatch.setattr(backend, "step_daddy", sd)
    client = TestClient(backend.fastapi_app)

    resp = client.get("/stream/501.m3u8")
    assert resp.status_code == (404 if hide else 200)
    assert client.get("/stream/52.m3u8").status_code == 200
    assert streamed == (["52"] if hide else ["501", "52"])
