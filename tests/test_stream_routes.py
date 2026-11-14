"""/stream, /key and /content routes, with StepDaddy's network calls faked."""
import sys
import types

import pytest

try:
    import rxconfig  # noqa: F401  (real config when reflex is installed)
except ImportError:
    sys.modules["rxconfig"] = types.SimpleNamespace(config=types.SimpleNamespace(
        api_url="http://localhost:3535", proxy_content=True, socks5=""))

from fastapi.testclient import TestClient  # noqa: E402

from StepDaddyLiveHD import backend  # noqa: E402
from StepDaddyLiveHD.step_daddy import UpstreamBlocked  # noqa: E402
from StepDaddyLiveHD.utils import encrypt  # noqa: E402


@pytest.fixture
def client():
    return TestClient(backend.fastapi_app)


def test_stream_route_maps_block_to_503(client, monkeypatch):
    async def blocked_stream(channel_id):
        raise UpstreamBlocked(120.5)

    monkeypatch.setattr(backend.step_daddy, "stream", blocked_stream)
    r = client.get("/stream/10.m3u8")
    assert r.status_code == 503 and r.headers["retry-after"] == "120"


class FakeSegment:
    def __init__(self, status, headers=None):
        self.status_code, self.headers, self.url, self.text = status, headers or {}, "https://cdn.test/s", "x"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def content_path():
    # Match the registered /content route, with or without a .ts suffix.
    route = next(r.path for r in backend.fastapi_app.routes if getattr(r, "path", "").startswith("/content/"))
    suffix = ".ts" if route.endswith(".ts") else ""
    return f"/content/{encrypt('https://cdn.test/s.png')}/{encrypt('https://src.test/1')}{suffix}"


def test_segment_cdn_429_does_not_block_dlhd(client, monkeypatch):
    # Segments come from a third-party CDN; its rate limit must not stop every
    # DLHD channel for up to an hour.
    monkeypatch.setattr(backend.step_daddy, "_blocked_until", 0.0)
    monkeypatch.setattr(backend.step_daddy._session, "stream",
                        lambda method, url, headers=None: FakeSegment(429, {"retry-after": "120"}))
    assert client.get(content_path()).status_code == 429
    assert backend.step_daddy._blocked_until == 0.0


def test_segments_still_proxied_during_dlhd_block(client, monkeypatch):
    monkeypatch.setattr(backend.step_daddy, "_blocked_until", 0.0)
    backend.step_daddy._blocked_until = float("inf")  # DLHD block active
    requested = []

    def stream(method, url, headers=None):
        requested.append(url)
        return FakeSegment(404)

    monkeypatch.setattr(backend.step_daddy._session, "stream", stream)
    assert client.get(content_path()).status_code == 404 and requested


def test_content_403_without_retry_after_passes_through(client, monkeypatch):
    monkeypatch.setattr(backend.step_daddy, "_blocked_until", 0.0)
    monkeypatch.setattr(backend.step_daddy._session, "stream",
                        lambda method, url, headers=None: FakeSegment(403))
    assert client.get(content_path()).status_code == 403


def test_key_route_maps_block_to_503(client, monkeypatch):
    async def blocked_key(url, host):
        raise UpstreamBlocked(60.2)

    monkeypatch.setattr(backend.step_daddy, "key", blocked_key)
    r = client.get("/key/a/b")
    assert r.status_code == 503 and r.headers["retry-after"] == "60"
