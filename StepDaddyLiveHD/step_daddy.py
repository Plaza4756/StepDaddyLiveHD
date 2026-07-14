import json
import re
from pydantic import BaseModel
from urllib.parse import urlparse, urljoin, parse_qs
from curl_cffi.requests import AsyncSession, RetryStrategy
from typing import List
from .utils import encrypt, decrypt, urlsafe_base64
from rxconfig import config
import html

# import asyncio
import time
import logging
import base64
import random

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
                proxy="socks5://" + socks5, impersonate="chrome", retry=strategy, allow_redirects="safe")
        else:
            self._session = AsyncSession(
                impersonate="chrome146", retry=strategy, allow_redirects="safe")
        self._base_url = "https://dlhd.st"
        self.channels = []
        self.channel_auth_done = False
        with open("StepDaddyLiveHD/meta.json", "r") as f:
            self._meta = json.load(f)
        self._cache = {}  # To cache server url
        # Cookies to be set by Flaresolverr first and used by curl_cffi subsequently
        self._cookies = {}
        self._ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        

    def _headers(self, referer: str = None, origin: str = None):
        if referer is None:
            referer = self._base_url
        headers = {
            "Referer": referer,
            "user-agent": self._ua,
        }
        if origin:
            headers["Origin"] = origin
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
                # response,
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
        if (self._cache.get("channel") == channel_id) and (current_ts < int(self._cache.get("expiry"))):
            source_url = self._cache["source_url"]
            m3u8_playlist_url = self._cache["m3u8_playlist_url"]
            m3u8_stream_info = self._cache["m3u8_stream_info"]
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
                    headers=self._headers()
                )
                m3u8_redirect_url_encoded = re.search(
                    r'window\.atob\(["\']([A-Za-z0-9+/=]+)["\']\)', source_resp.text).group(1)
                m3u8_redirect_url = base64.b64decode(
                    m3u8_redirect_url_encoded).decode('utf-8')
                logger.info(f"m3u8_redirect_url: {m3u8_redirect_url}")

                # Extract expiry query parameter from m3u8_redirect_url
                try:
                    parsed_m3u8_redirect_url = urlparse(m3u8_redirect_url)
                    expiry_param_value = parse_qs(
                        parsed_m3u8_redirect_url.query)['expires'][0]
                    logger.info(f"Expiry timestamp: {expiry_param_value}")
                except Exception as e:
                    logger.info(f"Error in expiry param extraction: {e}")
                    # Set aritificial ~1hr of cache expiry time in case of a failure
                    expiry_param_value = int(time.time()) + 3500
            except Exception as e:
                logger.info(f"Error: {e}")
            try:
                m3u8_redirect_resp = await self._session.get(
                    url=m3u8_redirect_url,
                    headers=self._headers(source_url)
                )
                logger.info(f"m3u8_redirect_resp: {m3u8_redirect_resp.text}")
            except Exception as e:
                logger.info(f"Error: {e}")

            try:
                for line in m3u8_redirect_resp.text.split("\n"):
                    if line.startswith('#'):
                        if line.startswith('#EXT-X-STREAM-INF'):
                            m3u8_stream_info = line
                    elif line != '':
                        m3u8_playlist_url = urljoin(
                            m3u8_redirect_url, line)
                logger.info(f"m3u8_playlist_url: {m3u8_playlist_url}")
                self._cache["channel"] = channel_id
                self._cache["source_url"] = source_url
                self._cache["m3u8_playlist_url"] = m3u8_playlist_url
                self._cache["m3u8_stream_info"] = m3u8_stream_info
                self._cache["expiry"] = int(expiry_param_value)
            except Exception as e:
                logger.info(f"Error: {e}")

        try:
            m3u8_playlist_resp = await self._session.get(
                url=m3u8_playlist_url,
                headers=self._headers(source_url)
            )
            # logger.info(m3u8_playlist_resp.text)
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
        m3u8_data += m3u8_stream_info
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
