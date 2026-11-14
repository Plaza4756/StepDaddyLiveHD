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


@pytest.fixture
def client():
    return TestClient(backend.fastapi_app)


def test_stream_route_maps_block_to_503(client, monkeypatch):
    async def blocked_stream(channel_id):
        raise UpstreamBlocked(120.5)

    monkeypatch.setattr(backend.step_daddy, "stream", blocked_stream)
    r = client.get("/stream/10.m3u8")
    assert r.status_code == 503 and r.headers["retry-after"] == "120"
