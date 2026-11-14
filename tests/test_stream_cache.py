"""stream() per-channel cache lifecycle, against a fake DLHD session (no network)."""
import asyncio
import base64
import sys
from urllib.parse import parse_qs, urlparse
import types

import pytest

try:
    import rxconfig  # noqa: F401  (real config when reflex is installed)
except ImportError:
    sys.modules["rxconfig"] = types.SimpleNamespace(config=types.SimpleNamespace(
        api_url="http://localhost:3535", proxy_content=True, socks5=""))

from StepDaddyLiveHD.step_daddy import StepDaddy  # noqa: E402
from StepDaddyLiveHD import step_daddy as step_daddy_module  # noqa: E402

KEY = base64.urlsafe_b64encode(b"k" * 16).decode().rstrip("=")
PLAYLIST = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"


class Resp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data, self.url = status, text, data, ""

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeSession:
    def __init__(self):
        self.calls = []
        self.token_fail = set()  # channel ids whose token endpoint fails
        self.m3u8_status = {}  # channel id -> status code
        self.m3u8_raise = set()  # channel ids whose m3u8 request raises
        self.ttl = 600
        self.tickets = set()  # tickets DLHD currently accepts
        self.posts = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if "/stream/stream-" in url:
            cid = url.rsplit("-", 1)[1].split(".")[0]
            return Resp(text=f'<iframe src="https://src.test/{cid}" width="100%">')
        if url.startswith("https://src.test/"):
            cid = url.rsplit("/", 1)[1]
            return Resp(text=f'const SRC = "https://cdn.test/{cid}/index.m3u8";\n'
                             f'const CHANNEL = "slug{cid}";\nconst EDGE = "https://edge.test";')
        if "/api/playlist-token" in url:
            q = parse_qs(urlparse(url).query)
            cid = q["slug"][0].removeprefix("slug")
            if cid in self.token_fail:
                return Resp(status=403, text="forbidden")
            if q.get("tk", [None])[0] not in self.tickets:  # tickets are per channel
                return Resp(status=403, text='{"error":"ticket","reason":"bad"}')
            return Resp(data={"token": "t", "ttl": self.ttl, "k": KEY})
        if url.startswith("https://cdn.test/"):
            cid = url.split("/")[3]
            if cid in self.m3u8_raise:
                raise ConnectionError("boom")
            status = self.m3u8_status.get(cid, 200)
            return Resp(status=status, text=PLAYLIST if status == 200 else "nope")
        raise AssertionError(f"unexpected url {url}")


    async def post(self, url, timeout=None):
        self.posts.append(url)
        return Resp()


class FakeBrowser:
    """Stands in for loading the stream page in cloakbrowser."""

    def __init__(self, session):
        self.session = session
        self.passes = []  # channel ids, in order
        self.delay = 0.0
        self.active = self.max_active = 0
        self.during = None  # called while a pass is loading

    async def load(self, page_url, edge, slug):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            cid = slug.removeprefix("slug")
            self.passes.append(cid)
            if self.during:
                self.during(cid)
            await asyncio.sleep(self.delay)
            ticket = f"ticket-{cid}-{len(self.passes)}"
            self.session.tickets.add(ticket)
            return f"{edge}/api/playlist-token?slug={slug}&tk={ticket}"
        finally:
            self.active -= 1


@pytest.fixture
def sd():
    s = StepDaddy()
    s._session = FakeSession()
    s.browser = FakeBrowser(s._session)
    s._load_in_browser = s.browser.load
    return s


def scrapes(sd, cid):
    return sum(1 for u in sd._session.calls if u.endswith(f"/stream/stream-{cid}.php"))


def test_cache_hit_reuses_entry(sd):
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert scrapes(sd, "1") == 1


def test_lru_bound(sd):
    sd._cache_size = 2
    for cid in ("1", "2", "1", "3"):  # 1 is refreshed before 3 arrives, so 2 is evicted
        asyncio.run(sd.stream(cid))
    assert list(sd._cache) == ["1", "3"]


def test_failed_token_refresh_rescrapes(sd):
    sd._session.ttl = 0  # token expires immediately, so the next hit refreshes
    asyncio.run(sd.stream("1"))
    sd._session.token_fail.add("1")
    with pytest.raises(Exception):
        asyncio.run(sd.stream("1"))  # refresh fails, rescrape's token also fails
    assert "1" not in sd._cache and scrapes(sd, "1") == 2
    sd._session.token_fail.clear()
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")  # recovers without restart
    assert scrapes(sd, "1") == 3


def test_refresh_failure_recovers_in_same_request(sd):
    sd._session.ttl = 0
    asyncio.run(sd.stream("1"))
    calls = {"n": 0}
    real_get = sd._session.get

    async def flaky(url, **kw):  # only the first token refresh fails
        if "/api/playlist-token" in url and calls["n"] == 0:
            calls["n"] += 1
            return Resp(status=500, text="err")
        return await real_get(url, **kw)

    sd._session.get = flaky
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert scrapes(sd, "1") == 2


@pytest.mark.parametrize("failure", ["status", "raise"])
def test_failed_m3u8_evicts(sd, failure):
    asyncio.run(sd.stream("1"))
    if failure == "status":
        sd._session.m3u8_status["1"] = 404
        match = "status code: 404"
    else:
        sd._session.m3u8_raise.add("1")
        match = "request failed: boom"
    with pytest.raises(Exception, match=match):
        asyncio.run(sd.stream("1"))
    assert "1" not in sd._cache


# --- Cloudflare Turnstile tickets (one browser pass per channel)


def test_ticket_is_reused_for_token_refresh(sd):
    sd._session.ttl = 0  # token expires immediately, so every hit refreshes
    for _ in range(3):
        assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert sd.browser.passes == ["1"] and scrapes(sd, "1") == 1


def test_rejected_ticket_gets_a_new_one(sd):
    sd._session.ttl = 0
    asyncio.run(sd.stream("1"))
    sd._session.tickets.clear()  # DLHD stops accepting the ticket
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert sd.browser.passes == ["1", "1"] and scrapes(sd, "1") == 2
    # initial token, the rejected refresh, the new ticket: the rejected ticket isn't tried twice
    assert sum("/api/playlist-token" in u for u in sd._session.calls) == 3


def test_concurrent_misses_share_one_browser_pass(sd):
    sd.browser.delay = 0.05

    async def two_viewers():
        return await asyncio.gather(sd.stream("1"), sd.stream("1"))

    assert all(p.startswith("#EXTM3U") for p in asyncio.run(two_viewers()))
    assert sd.browser.passes == ["1"]


def test_browser_passes_run_one_at_a_time(sd):
    sd.browser.delay = 0.05

    async def two_channels():
        return await asyncio.gather(sd.stream("1"), sd.stream("2"))

    asyncio.run(two_channels())
    assert sorted(sd.browser.passes) == ["1", "2"] and sd.browser.max_active == 1


def test_kept_ticket_survives_a_failed_playlist(sd):
    # An event channel that is off air answers 404 for its playlist. Retries
    # must reuse the channel's ticket, not start a browser pass each time.
    asyncio.run(sd.stream("1"))
    sd._session.m3u8_status["1"] = 404
    for _ in range(3):
        with pytest.raises(Exception, match="status code: 404"):
            asyncio.run(sd.stream("1"))
    del sd._session.m3u8_status["1"]
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert sd.browser.passes == ["1"]


def test_ticket_from_an_abandoned_request_is_kept(sd):
    sd.browser.delay = 0.1

    async def give_up_then_retry():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sd.stream("1"), 0.05)  # e.g. an event slot's deadline
        await asyncio.sleep(0.2)  # the browser pass finishes anyway
        return await sd.stream("1")

    assert asyncio.run(give_up_then_retry()).startswith("#EXTM3U")
    assert sd.browser.passes == ["1"]


def test_kept_tickets_are_bounded(sd):
    sd._cache_size = 2
    for cid in ("1", "2", "3"):
        asyncio.run(sd.stream(cid))
    assert len(sd._tickets) == 2 and not any(u.endswith("stream-1.php") for u in sd._tickets)


class BrowserResponse:
    def __init__(self, url, status, headers=None):
        self.url, self.status, self.headers = url, status, headers or {}


def test_kept_ticket_survives_other_token_failures(sd):
    # Only a ticket rejection costs a browser pass; a failing edge doesn't.
    asyncio.run(sd.stream("1"))
    sd._session.m3u8_status["1"] = 404
    with pytest.raises(Exception):
        asyncio.run(sd.stream("1"))  # drops the cache entry, keeps the ticket
    del sd._session.m3u8_status["1"]
    sd._session.token_fail.add("1")  # 403 without a ticket error
    with pytest.raises(Exception):
        asyncio.run(sd.stream("1"))
    sd._session.token_fail.clear()
    assert asyncio.run(sd.stream("1")).startswith("#EXTM3U")
    assert sd.browser.passes == ["1"]


class FakePlaywright:
    """async_playwright() stand-in: a page whose load emits the given responses."""

    def __init__(self, responses=(), hang_connect=False):
        self.responses, self.hang_connect = list(responses), hang_connect
        self.stopped = self.closed = False

    def __call__(self):
        return self

    async def start(self):
        return self

    @property
    def chromium(self):
        return self

    async def connect_over_cdp(self, url):
        if self.hang_connect:
            await asyncio.sleep(3600)
        return self

    async def new_page(self):
        return self

    def on(self, event, handler):
        self.handler = handler

    async def set_extra_http_headers(self, headers):
        pass

    async def goto(self, url, wait_until=None):
        for r in self.responses:
            self.handler(r)

    async def close(self):
        self.closed = True

    async def stop(self):
        self.stopped = True


@pytest.fixture
def real_browser_pass(sd, monkeypatch):
    """Run the real _load_in_browser against a FakePlaywright."""
    monkeypatch.setattr(step_daddy_module.config, "cdp_host", "http://cdp.test", raising=False)
    monkeypatch.setattr(step_daddy_module, "TICKET_TIMEOUT", 0.2)

    def run(fake):
        monkeypatch.setattr(step_daddy_module, "async_playwright", fake)
        return asyncio.run(StepDaddy._load_in_browser(sd, "https://dlhd.test/stream/stream-1.php",
                                                      "https://edge.test", "slug1"))
    return run


def test_browser_pass_returns_the_ticketed_url(sd, real_browser_pass):
    url = "https://edge.test/api/playlist-token?slug=slug1&tk=abc"
    fake = FakePlaywright([BrowserResponse("https://edge.test/api/playlist-token?slug=slug10&tk=x", 200),
                           BrowserResponse(url, 200)])
    assert real_browser_pass(fake) == url
    assert fake.closed and fake.stopped and sd._session.posts == ["http://cdp.test/fingerprint/12345/close"]


def test_browser_pass_is_bounded_and_cleaned_up(sd, real_browser_pass):
    fake = FakePlaywright(hang_connect=True)  # cloakbrowser accepts but never answers
    with pytest.raises(Exception, match="No ticketed token request within"):
        real_browser_pass(fake)
    assert fake.stopped and sd._session.posts  # cleanup still ran


def test_token_url_must_be_for_this_channel():
    edge = "https://edge.test"
    ok = f"{edge}/api/playlist-token?slug=premium31&tk=x"
    assert StepDaddy._is_token_url(ok, edge, "premium31")
    assert not StepDaddy._is_token_url(ok, edge, "premium3")  # not a prefix match
    assert not StepDaddy._is_token_url(ok, "https://other.test", "premium31")
