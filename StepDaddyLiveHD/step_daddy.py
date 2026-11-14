import json
import re
from pydantic import BaseModel
from urllib.parse import urlparse, urljoin, parse_qs
from curl_cffi.requests import AsyncSession, RetryStrategy
from typing import List
from .utils import encrypt, decrypt, urlsafe_base64, custom_aes_decrypt
from rxconfig import config
import html
import time
import logging
import base64
import asyncio
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class Channel(BaseModel):
    id: str
    name: str
    tags: List[str]
    logo: str | None


class TicketRejected(Exception):
    """DLHD refused the playlist-token request's Turnstile ticket."""


# Seconds a browser pass may take to connect, load the stream page and get a
# ticketed playlist-token request (measured 6-9 s). Cleanup has its own bound.
TICKET_TIMEOUT = 30
BROWSER_CLEANUP_TIMEOUT = 10
# cloakbrowser fingerprint seed; the same seed gives the same browser identity.
CDP_FINGERPRINT = "12345"


class StepDaddy:
    def __init__(self):
        socks5 = config.socks5
        strategy = RetryStrategy(
            count=3, delay=0.5, jitter=0.1, backoff="exponential")
        if socks5 != "":
            self._session = AsyncSession(
                proxy="socks5://" + socks5, impersonate="chrome146", retry=strategy, allow_redirects="safe")
        else:
            self._session = AsyncSession(
                impersonate="chrome146", retry=strategy, allow_redirects="safe")
        self._base_url = "https://thedaddy.li"
        self.channels = []
        with open("StepDaddyLiveHD/meta.json", "r") as f:
            self._meta = json.load(f)
        self._cache = {}  # channel_id -> stream token/url cache, LRU ordered
        self._cache_size = 16
        # cloakbrowser's free build allows one browser session at a time.
        self._browser_lock = asyncio.Lock()
        self._ticket_passes = {}  # stream page url -> browser pass in progress
        # stream page url -> last ticketed token URL, kept apart from the cache
        # so a failed playlist (e.g. an event channel off air) doesn't cost a
        # new browser pass on every retry. LRU ordered, _cache_size entries.
        self._tickets = {}
        self._ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

    def _headers(self, referer: str = None, origin: str = None):
        if referer is None:
            referer = self._base_url
        else:
            ref_com = urlparse(referer)
            referer = f"{ref_com.scheme}://{ref_com.netloc}/"
        headers = {
            "referer": referer,
            "user-agent": self._ua,
        }
        if origin:
            origin_com = urlparse(origin)
            origin = f"{origin_com.scheme}://{origin_com.netloc}"
            headers["origin"] = origin
        if ("Windows" in self._ua):
            headers["sec-ch-ua-platform"] = '"Windows"'
            headers["sec-ch-ua"] = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
        return headers

    async def load_channels(self):
        channels = []
        try:
            channels_url = f"{self._base_url}/24-7-channels.php"
            response = await self._session.get(
                url=channels_url,
                headers=self._headers(),
            )
            
            # Logic to update self._base_url if it hase moved to a new domain
            url_from_resp = urlparse(response.url)
            extracted_base_url = f"{
                url_from_resp.scheme}://{url_from_resp.netloc}"
            if extracted_base_url != self._base_url:
                logger.info(f"Updated baseUrl: {extracted_base_url}")
                self._base_url = extracted_base_url

            
            matches = re.findall(
                r'<a class="card"\s+href="/watch\.php\?id=(\d+)"[^>]*>\s*<div class="card__title">(.*?)</div>',
                response.text,
                re.DOTALL
            )
            for channel_id, channel_name in matches:
                channel_name = html.unescape(
                    channel_name.strip()).replace("#", "")
                meta = self._meta.get(
                    "18+" if channel_name.startswith("18+") else channel_name, {})
                logo = meta.get("logo", "")
                if logo:
                    logo = f"{config.api_url}/logo/{urlsafe_base64(logo)}"
                channels.append(
                    Channel(id=channel_id, name=channel_name, tags=meta.get("tags", []), logo=logo))
        finally:
            self.channels = sorted(channels, key=lambda channel: (
                channel.name.startswith("18"), channel.name))

    async def stream(self, channel_id: str):
        current_ts = int(time.time())
        # One cache entry per channel, so concurrent viewers on different
        # channels don't evict each other's tokens.
        cache = self._cache.get(channel_id)
        if cache is not None:
            self._cache[channel_id] = self._cache.pop(channel_id)  # mark most recently used
            source_url = cache["source_url"]

            if (current_ts < cache.get("expiry")):
                m3u8_playlist_url = cache["m3u8_playlist_url"]
            else:
                #refresh playlist-token
                try:
                    await self.get_m3u8_playlist_token(cache["playlist_token_url"], source_url, cache)
                    m3u8_playlist_url = cache["m3u8_playlist_url"]
                except Exception as e:
                    # A failed refresh (e.g. the ticket expired or DLHD rotated
                    # EDGE) drops the entry and rescrapes below, with a new
                    # ticket, instead of failing until restart.
                    logger.info(f"Token refresh failed for {channel_id}, rescraping: {e}")
                    if isinstance(e, TicketRejected):
                        self._forget_ticket(self._stream_page_url(channel_id), cache["playlist_token_url"])
                    self._cache.pop(channel_id, None)
                    cache = None
        if cache is None:
            logger.info("Cache miss!")
            cache = {}
            url = self._stream_page_url(channel_id)
            response = await self._session.get(
                url=url,
                headers=self._headers(),
                timeout=12
            )
            try:
                matches = re.compile(
                    "iframe src=\"(.*)\" width").findall(response.text)
                source_url = matches[0]
                logger.info("source_url: %s", source_url)
            except Exception as e:
                logger.info(f"Error: {e}")

            try:
                source_headers = self._headers()
                source_headers["Sec-Fetch-Dest"] = "iframe"
                source_resp = await self._session.get(
                    url=source_url,
                    headers=source_headers,
                    timeout=12
                )
                if source_resp.status_code != 200:
                    logger.info(f"{source_url} failed with code {source_resp.status_code}")

                m3u8_playlist_src = re.search(r'const\sSRC\s*=\s*"((?:[^"\\]|\\.)*)"', source_resp.text).group(1).replace('\\/', '/')
                channel_slug = re.search(r'const\sCHANNEL\s*=\s*"((?:[^"\\]|\\.)*)"', source_resp.text).group(1).replace('\\/', '/')
                edge = re.search(r'const\sEDGE\s*=\s*"((?:[^"\\]|\\.)*)"', source_resp.text).group(1).replace('\\/', '/')

                cache["m3u8_playlist_src"] = m3u8_playlist_src
                # DLHD only issues a token for a URL carrying a Cloudflare
                # Turnstile ticket, which a browser gets by loading the page.
                playlist_token_url = await self._token_with_ticket(url, edge, channel_slug, source_url, cache)
                
                cache["source_url"] = source_url
                m3u8_playlist_url = cache["m3u8_playlist_url"]
                cache["playlist_token_url"] = playlist_token_url
                cache["m3u8_playlist_src"] = m3u8_playlist_src
                self._cache[channel_id] = cache
                while len(self._cache) > self._cache_size:
                    self._cache.pop(next(iter(self._cache)))  # evict least recently used

            except Exception as e:
                logger.info(f"Error: {e}")

        aes_key = cache["aes_key"]
        try:
            m3u8_playlist_resp = await self._session.get(
                url=m3u8_playlist_url,
                headers=self._headers(source_url,source_url),
                timeout=12
            )
            if m3u8_playlist_resp.status_code != 200:
                logger.info(f"m3u8_playlist status code: {
                        m3u8_playlist_resp.status_code}")
        except Exception as e:
            logger.info(f"Error: {e}")
            self._cache.pop(channel_id, None)
            raise Exception(f"Upstream m3u8 playlist request failed: {e}") from e
        if m3u8_playlist_resp.status_code != 200:
            # Fail clearly and drop the cache entry so the next request starts
            # fresh.
            self._cache.pop(channel_id, None)
            raise Exception(f"Upstream m3u8 playlist status code: {m3u8_playlist_resp.status_code}")

        m3u8_data = ""
        sequence = 0
        for line in m3u8_playlist_resp.text.split("\n"):
            if line.startswith('#'):
                if line.startswith("#EXT-X-KEY:"):
                    original_url = re.search(r'URI="(.*?)"', line).group(1)
                    content_key_url = urljoin(m3u8_playlist_url, original_url)
                    line = line.replace(original_url, f"{
                        config.api_url}/key/{encrypt(content_key_url)}/{encrypt(source_url)}")
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    sequence = int(line.split(":", 1)[1].strip())
                    # logger.info(f"sequence: {sequence}")
            elif ((line != '') and (config.proxy_content)):
                line = custom_aes_decrypt(line, aes_key, sequence)
                # logger.info(f"decoded segment url: {line}")
                sequence += 1
                line = urljoin(m3u8_playlist_url, line)
                line = f"{
                    config.api_url}/content/{encrypt(line)}/{encrypt(source_url)}"
            m3u8_data += line + "\n"
        return m3u8_data

    async def key(self, url: str, host: str):
        url = decrypt(url)
        host = decrypt(host)
        logger.info(f"Content_Key_Url: {url}")
        key_headers = self._headers(referer=f"{host}")

        key_response = await self._session.get(url, headers=key_headers)
        if key_response.status_code != 200:
            raise Exception("Failed to get key")
        return key_response.content

    async def _token_with_ticket(self, page_url: str, edge: str, slug: str, source_url: str, cache: dict) -> str:
        """Get a playlist token into cache; returns the ticketed token URL used.

        A ticket kept from an earlier pass is tried first. If DLHD rejects it,
        a browser pass gets a new one in the same request; other failures (a
        slow or failing edge) are raised without spending a browser pass.
        """
        kept = self._tickets.get(page_url)
        if kept is not None and self._is_token_url(kept, edge, slug):
            try:
                await self.get_m3u8_playlist_token(kept, source_url, cache)
                return kept
            except TicketRejected as e:
                logger.info(f"Kept ticket for {page_url} was rejected, getting a new one: {e}")
                self._forget_ticket(page_url, kept)
        token_url = await self._ticketed_token_url(page_url, edge, slug)
        await self.get_m3u8_playlist_token(token_url, source_url, cache)
        return token_url

    def _stream_page_url(self, channel_id: str) -> str:
        return f"{self._base_url}/stream/stream-{channel_id}.php"

    def _forget_ticket(self, page_url: str, token_url: str):
        # Only that ticket: another request may already have stored a new one.
        if self._tickets.get(page_url) == token_url:
            del self._tickets[page_url]

    async def _ticketed_token_url(self, page_url: str, edge: str, slug: str) -> str:
        """Playlist-token URL with a Cloudflare Turnstile ticket for this channel.

        A ticket only works for its own channel, but keeps working for token
        refreshes, so it is fetched once per cache entry. Concurrent requests
        for the same channel share one browser pass.
        """
        task = self._ticket_passes.get(page_url)
        if task is None or task.done():
            task = asyncio.ensure_future(self._browser_ticket(page_url, edge, slug))
            self._ticket_passes[page_url] = task

            def done(t):
                if self._ticket_passes.get(page_url) is t:
                    del self._ticket_passes[page_url]
                if t.cancelled():
                    return
                if t.exception() is not None:
                    logger.info(f"Ticket for {page_url} failed: {t.exception()}")
                    return
                # Kept even if every caller gave up waiting, so the pass isn't wasted.
                self._tickets.pop(page_url, None)
                self._tickets[page_url] = t.result()
                while len(self._tickets) > self._cache_size:
                    self._tickets.pop(next(iter(self._tickets)))

            task.add_done_callback(done)
        # shield: a caller that gives up (e.g. its client timed out) must not
        # cancel the pass other callers are waiting on.
        return await asyncio.shield(task)

    @staticmethod
    def _is_token_url(url: str, edge: str, slug: str) -> bool:
        if not url.startswith(f"{edge}/api/playlist-token?"):
            return False
        return parse_qs(urlparse(url).query).get("slug") == [slug]

    async def _browser_ticket(self, page_url: str, edge: str, slug: str) -> str:
        async with self._browser_lock:
            return await self._load_in_browser(page_url, edge, slug)

    def _on_browser_response(self, found: asyncio.Future, response, edge: str, slug: str):
        if found.done() or not self._is_token_url(response.url, edge, slug):
            return
        if response.status == 200:
            found.set_result(response.url)
        else:
            found.set_exception(Exception(f"Ticketed token request failed with code {response.status}"))

    async def _load_in_browser(self, page_url: str, edge: str, slug: str) -> str:
        """Load page_url in cloakbrowser and return the token URL it requests."""
        found = asyncio.get_running_loop().create_future()
        logger.info(f"Cloudflare turnstile verification for {page_url}")
        pw = browser = None
        try:
            # One deadline for the whole pass: it holds the browser lock, so a
            # stalled cloakbrowser must not hold up every other channel.
            async with asyncio.timeout(TICKET_TIMEOUT):
                pw = await async_playwright().start()
                browser = await pw.chromium.connect_over_cdp(f"{config.cdp_host}?fingerprint={CDP_FINGERPRINT}")
                page = await browser.new_page()
                page.on("response", lambda r: self._on_browser_response(found, r, edge, slug))
                await page.set_extra_http_headers({"Referer": f"{self._base_url}/", "Origin": self._base_url})
                await page.goto(url=page_url, wait_until="domcontentloaded")
                token_url = await found
        except TimeoutError:
            raise Exception(f"No ticketed token request within {TICKET_TIMEOUT}s") from None
        finally:
            if found.done() and not found.cancelled():
                found.exception()  # retrieved, so asyncio doesn't warn about it
            await self._close_browser(pw, browser)
        logger.info(f"playlist_token_url: {token_url}")
        return token_url

    async def _close_browser(self, pw, browser):
        """Best-effort cleanup, bounded so a stalled browser can't hold the lock."""
        try:
            async with asyncio.timeout(BROWSER_CLEANUP_TIMEOUT):
                if browser is not None:
                    await browser.close()
                if pw is not None:
                    await pw.stop()
        except Exception as e:
            logger.info(f"Closing the browser failed: {e}")
        try:
            close_url = f"{config.cdp_host}/fingerprint/{CDP_FINGERPRINT}/close"
            await asyncio.wait_for(self._session.post(close_url, timeout=5), 6)
        except Exception as e:
            logger.info(f"Closing the browser fingerprint failed: {e}")

    async def get_m3u8_playlist_token(self, token_url: str, source_url: str, cache: dict):
        current_ts = int(time.time())
        #get playlist-token
        pt_resp = await self._session.get(
            url=token_url,
            headers=self._headers(source_url,source_url),
            timeout=12
        )
        if pt_resp.status_code != 200:
            logger.info(f"Playlist token request failed with code: {pt_resp.status_code}")
            if pt_resp.status_code == 403 and '"ticket"' in pt_resp.text:
                raise TicketRejected(f"Playlist token request failed with code: 403 {pt_resp.text[:80]}")
        token = pt_resp.json().get("token")
        ttl = int(pt_resp.json().get("ttl"))
        aes_key = pt_resp.json().get("k")
        # logger.info(f"aes_key: {aes_key}")
        m3u8_playlist_src = cache["m3u8_playlist_src"]
        m3u8_playlist_url = f"{m3u8_playlist_src}?t={token}&_={current_ts}"
        logger.info(f"m3u8 playlist url : {m3u8_playlist_url}")
        expiry = current_ts + int(ttl*0.5)

        cache["m3u8_playlist_url"] = m3u8_playlist_url
        cache["expiry"] = expiry
        cache["aes_key"] = aes_key 


    @staticmethod
    def content_url(path: str):
        return decrypt(path)

    def playlist(self):
        data = "#EXTM3U\n"
        for channel in self.channels:
            entry = f" tvg-logo=\"{channel.logo}\",{
                channel.name}" if channel.logo else f",{channel.name}"
            data += f"#EXTINF:-1{entry}\n{config.api_url}/stream/{channel.id}.m3u8\n"
        return data

    async def schedule(self):
        response = await self._session.get(f"{self._base_url}/schedule/schedule-generated.php", headers=self._headers())
        return response.json()
