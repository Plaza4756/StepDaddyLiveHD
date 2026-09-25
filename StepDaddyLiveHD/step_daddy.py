import json
import re
from pydantic import BaseModel
from urllib.parse import urlparse, urljoin, parse_qs
from curl_cffi.requests import AsyncSession, RetryStrategy
from typing import List
from .utils import encrypt, decrypt, urlsafe_base64
from rxconfig import config
import html
import time
import logging
import base64

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


class StepDaddy:
    def __init__(self):
        socks5 = config.socks5
        strategy = RetryStrategy(
            count=3, delay=0.5, jitter=0.1, backoff="exponential")
        if socks5 != "":
            self._session = AsyncSession(
                proxy="socks5://" + socks5, impersonate="chrome150", retry=strategy, allow_redirects="safe")
        else:
            self._session = AsyncSession(
                impersonate="chrome150", retry=strategy, allow_redirects="safe")
        self._base_url = "https://dlive.sx"
        self.channels = []
        with open("StepDaddyLiveHD/meta.json", "r") as f:
            self._meta = json.load(f)
        self._cache = {}  # To cache server url
        # Cookies to be set by Flaresolverr first and used by curl_cffi subsequently
        self._cookies = {}
        self._ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

    def _headers(self, referer: str = None, origin: str = None):
        if referer is None:
            referer = self._base_url
        else:
            ref_com = urlparse(referer)
            referer = f"{ref_com.scheme}://{ref_com.netloc}/"
        headers = {
            "referer": referer,
        }
        if origin:
            headers["origin"] = origin
        if ("Windows" in self._ua):
            headers["sec-ch-ua-platform"] = '"Windows"'
            headers["sec-ch-ua"] = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
        return headers

    async def load_channels(self):
        channels = []
        try:
            channels_url = f"{self._base_url}/24-7-channels.php"
            response = await self._session.post(
                url=channels_url,
                headers=self._headers(),
                cookies=self._cookies
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
        if (self._cache.get("channel") == channel_id) and (current_ts < self._cache.get("expiry")):
            source_url = self._cache["source_url"]
            m3u8_playlist_url = self._cache["m3u8_playlist_url"]
        else:
            logger.info("Cache miss!")
            self._cache.clear()
            url = f"{self._base_url}/stream/stream-{channel_id}.php"
            response = await self._session.post(
                url=url,
                headers=self._headers(),
                cookies=self._cookies,
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
                source_resp = await self._session.get(
                    url=source_url,
                    headers=self._headers(),
                )
                if source_resp.status_code != 200:
                    logger.info(f"{source_url} failed with code {source_resp.status_code}")
                    
                # De-obfucated js logic to extract m3u8_playlist_url
                econfig = re.search(r"window\._econfig\s*=\s*['\"]([^'\"]+)['\"]", source_resp.text).group(1)
                outer = base64.b64decode(econfig).decode("utf-8")
                q = len(outer) // 4
                chunks = [outer[i:i+q] for i in range(0, q*4, q)]
                reordered = [None] * 4
                for source_index, destination_index in enumerate([2, 0, 3, 1]):
                    c = chunks[source_index]
                    c = c[:3] + c[4:]
                    reordered[destination_index] = base64.b64decode(c).decode("utf-8")
                decoded_config = json.loads(base64.b64decode("".join(reordered)).decode("utf-8"))
                m3u8_playlist_url = decoded_config.get("stream_url_nop2p") or decoded_config.get("stream_url")
                logger.info(f"m3u8_playlist_url: {m3u8_playlist_url}")

                # Extract expiry query parameter from m3u8_playlist_url
                try:
                    parsed_m3u8_playlist_url = urlparse(m3u8_playlist_url)
                    expiry = int(parse_qs(parsed_m3u8_playlist_url.query)['e'][0])
                    logger.info(f"Cache m3u8_playlist_url till timestamp: {expiry}")
                except Exception as e:
                    logger.info(f"Error in expiry parameter extraction: {e}")
                    # Set artificial approx 1hr cache expiry time
                    expiry = current_ts + 3500
                
                self._cache["source_url"] = source_url
                self._cache["m3u8_playlist_url"] = m3u8_playlist_url
                self._cache["channel"] = channel_id
                self._cache["expiry"] = expiry

            except Exception as e:
                logger.info(f"Error: {e}")

        try:
            m3u8_playlist_resp = await self._session.get(
                url=m3u8_playlist_url,
                headers=self._headers(source_url)
            )
            if m3u8_playlist_resp.status_code != 200:
                logger.info(f"m3u8_playlist status code: {
                        m3u8_playlist_resp.status_code}")
        except Exception as e:
            logger.info(f"Error: {e}")

        m3u8_data = ""
        for line in m3u8_playlist_resp.text.split("\n"):
            if line.startswith('#'):
                if line.startswith("#EXT-X-KEY:"):
                    original_url = re.search(r'URI="(.*?)"', line).group(1)
                    content_key_url = urljoin(m3u8_playlist_url, original_url)
                    line = line.replace(original_url, f"{
                        config.api_url}/key/{encrypt(content_key_url)}/{encrypt(source_url)}")
            elif ((line != '') and (config.proxy_content)):
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
