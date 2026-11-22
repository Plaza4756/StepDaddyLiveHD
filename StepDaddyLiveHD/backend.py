import os
import asyncio
from StepDaddyLiveHD.step_daddy import StepDaddy, Channel
from fastapi import Response, status, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from .utils import urlsafe_base64_decode
from urllib.parse import quote, urlparse
import random
import curl_cffi.requests
import logging
import zlib
from typing import Optional

logger = logging.getLogger(__name__)

fastapi_app = FastAPI()
step_daddy = StepDaddy()

CHANNEL_RETRY_SECONDS = 60  # wait before retrying a failed channel list load


@fastapi_app.get("/stream/{channel_id}.m3u8")
async def stream(channel_id: str):
    if step_daddy.nsfw_ids_unknown():
        return JSONResponse(content={"error": "Channel list not loaded yet"},
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            headers={"Retry-After": str(CHANNEL_RETRY_SECONDS)})
    if step_daddy.is_hidden(channel_id):
        return JSONResponse(content={"error": "Stream not found"}, status_code=status.HTTP_404_NOT_FOUND)
    try:
        return Response(
            content=await step_daddy.stream(channel_id),
            media_type="application/vnd.apple.mpegurl",
            headers={
                f"Content-Disposition": f"attachment; filename={channel_id}.m3u8"}
        )
    except IndexError:
        return JSONResponse(content={"error": "Stream not found"}, status_code=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@fastapi_app.get("/key/{url}/{host}")
async def key(url: str, host: str):
    try:
        return Response(
            content=await step_daddy.key(url, host),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=key"}
        )
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# TIKTIKTSGZ - gzip-compressed MPEG-TS follows the marker
MAGIC_TS_GZ = b"TIKTIKTSGZ"
# TIKTIKRAW  - raw MPEG-TS follows the marker
MAGIC_TS_RAW = b"TIKTIKRAW"
# TIKTIKPX   - marker embedded in decoded PNG pixels
MAGIC_PIX = b"TIKTIKPX"

TS_SYNC = 0x47          # MPEG-TS sync byte
TS_PACKET_SIZE = 0xBC  # 188 bytes


# --------------------------------------------------------------------------
# Decompression helpers
# --------------------------------------------------------------------------

def gunzip(data: bytes) -> bytes:
    return zlib.decompress(bytes(data), 16 + zlib.MAX_WBITS)


def inflate_zlib(data: bytes) -> bytes:
    return zlib.decompress(bytes(data))


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


# --------------------------------------------------------------------------
# Container probes
# --------------------------------------------------------------------------

def webp_exif_ts(data: bytes) -> Optional[bytes]:
    if len(data) < 0x10:
        return None
    if data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None

    pos = 0x0C
    while pos + 8 <= len(data):
        chunk_type = data[pos:pos + 4]
        chunk_size = int.from_bytes(data[pos + 4:pos + 8], "little")
        pos += 8
        if chunk_size < 0 or pos + chunk_size > len(data):
            return None
        if chunk_type == b"EXIF":
            payload = data[pos:pos + chunk_size]
            # A valid payload starts with the TS sync byte and repeats it 188
            # bytes later (i.e. the next packet).
            if len(payload) > TS_PACKET_SIZE \
                    and payload[0] == TS_SYNC \
                    and payload[TS_PACKET_SIZE] == TS_SYNC:
                return payload
            return None
        # RIFF chunks are word aligned: pad odd-sized chunks with one byte.
        pos += chunk_size + (chunk_size & 1)

    return None


def png_iend_ts(data: bytes) -> Optional[bytes]:
    if len(data) < 0x10 or data[0] != 0x89 or data[1] != 0x50:  # '\\x89P'
        return None

    pos = 0x08
    while pos + 8 <= len(data):
        chunk_len = int.from_bytes(data[pos:pos + 4], "big")
        if chunk_len > len(data) - pos - 12:
            return None
        chunk_type = data[pos + 4:pos + 8]
        pos += 8 + chunk_len + 4
        if chunk_type == b"IEND":
            if pos < len(data) \
                    and data[pos] == TS_SYNC \
                    and pos + TS_PACKET_SIZE < len(data) \
                    and data[pos + TS_PACKET_SIZE] == TS_SYNC:
                return data[pos:]
            return None

    return None


# --------------------------------------------------------------------------
# PNG decoding (pixel steganography path)
# --------------------------------------------------------------------------

def png_rgb(data: bytes) -> Optional[bytes]:
    if len(data) < 8 or data[0] != 0x89 or data[1] != 0x50:
        return None

    width = height = 0
    bit_depth = colour_type = interlace = 0
    idat = bytearray()

    pos = 0x08
    while pos + 8 <= len(data):
        chunk_len = int.from_bytes(data[pos:pos + 4], "big")
        if chunk_len > len(data) - pos - 12:
            return None
        chunk_type = data[pos + 4:pos + 8]
        if chunk_type == b"IHDR":
            chunk = data[pos + 8:pos + 8 + chunk_len]
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            bit_depth = chunk[8]
            colour_type = chunk[9]
            interlace = chunk[12]
        elif chunk_type == b"IDAT":
            idat += data[pos + 8:pos + 8 + chunk_len]
        elif chunk_type == b"IEND":
            break
        pos += 12 + chunk_len

    if not width or not height or bit_depth != 8 or interlace \
            or (colour_type != 2 and colour_type != 6):
        return None

    raw = inflate_zlib(bytes(idat))

    bpp = 4 if colour_type == 6 else 3
    stride = width * bpp
    out = bytearray(width * height * 3)

    prev = bytes(stride)      # previously reconstructed scanline
    off = 0                   # read cursor into `raw`
    dst = 0                   # write cursor into `out`

    for _ in range(height):
        if off + 1 + stride > len(raw):
            return None
        ftype = raw[off]
        line = raw[off + 1:off + 1 + stride]
        off += 1 + stride

        if ftype == 0:                                    # filter: None
            if colour_type == 2:
                out[dst:dst + stride] = line
            else:
                j = dst
                for i in range(0, stride, 4):
                    out[j] = line[i]
                    out[j + 1] = line[i + 1]
                    out[j + 2] = line[i + 2]
                    j += 3
            dst += width * 3
            prev = line
            continue

        cur = bytearray(stride)
        for i in range(stride):
            left = cur[i - bpp] if i >= bpp else 0
            up = prev[i]
            upleft = prev[i - bpp] if i >= bpp else 0
            x = line[i]
            if ftype == 1:        # Sub
                x += left
            elif ftype == 2:      # Up
                x += up
            elif ftype == 3:      # Average
                x += (left + up) >> 1
            elif ftype == 4:      # Paeth
                x += paeth(left, up, upleft)
            else:
                return None
            cur[i] = x & 0xFF

        if colour_type == 2:
            out[dst:dst + stride] = cur
            dst += stride
        else:
            for i in range(0, stride, 4):
                out[dst] = cur[i]
                out[dst + 1] = cur[i + 1]
                out[dst + 2] = cur[i + 2]
                dst += 3
        prev = bytes(cur)

    return bytes(out)


def find_pixel(rgb: Optional[bytes]) -> int:
    if not rgb or len(rgb) < 0x0C:
        return -1
    for i in range(len(rgb) - 7):
        if rgb[i] != MAGIC_PIX[0]:
            continue
        if rgb[i:i + len(MAGIC_PIX)] == MAGIC_PIX:
            return i
    return -1


def unwrap_pixels(data: bytes) -> Optional[bytes]:
    rgb = png_rgb(data)
    idx = find_pixel(rgb)
    if rgb is None or idx < 0 or idx + 12 > len(rgb):
        return None

    size = int.from_bytes(rgb[idx + 8:idx + 12], "big")
    if size <= 0 or idx + 12 + size > len(rgb):
        return None

    blob = rgb[idx + 12:idx + 12 + size]
    if len(blob) < 2 or blob[0] != 0x1F or blob[1] != 0x8B:
        return None

    try:
        out = gunzip(blob)
    except zlib.error:
        return None

    if not out or out[0] != TS_SYNC:
        return None
    return out


# --------------------------------------------------------------------------
# Top level segment unwrapping
# --------------------------------------------------------------------------

def unwrap_segment(data: bytes) -> bytes:
    data = bytes(data)

    # 1. WebP with an EXIF chunk carrying the TS.
    hit = webp_exif_ts(data)
    if hit:
        return hit

    # 2. PNG with the TS appended after IEND.
    hit = png_iend_ts(data)
    if hit:
        return hit

    # 3. PNG with the TS hidden in the decoded pixels.
    is_png = len(data) >= 8 and data[0] == 0x89 and data[1] == 0x50
    if is_png:
        try:
            hit = unwrap_pixels(data)
        except zlib.error:
            # We catch every failure of the pixel path and report the
            # generic "TS payload not found".
            hit = None
        if hit:
            return hit
        raise ValueError("TS payload not found")

    # 4. "TIKTIKRAW" marker followed by the raw TS.
    for i in range(len(data) - len(MAGIC_TS_RAW)):
        if data[i:i + len(MAGIC_TS_RAW)] == MAGIC_TS_RAW:
            payload = data[i + len(MAGIC_TS_RAW):]
            if payload and payload[0] == TS_SYNC:
                return payload

    # 5. "TIKTIKTSGZ" marker followed by a gzip-compressed TS.
    for i in range(len(data) - len(MAGIC_TS_GZ)):
        if data[i:i + len(MAGIC_TS_GZ)] == MAGIC_TS_GZ:
            return gunzip(data[i + len(MAGIC_TS_GZ):])

    # 6. Fall back to scanning for the MPEG-TS sync pattern: 0x47 at offset
    #    i and again at i+188 (the start of the next 188-byte packet).
    for i in range(len(data) - TS_PACKET_SIZE):
        if data[i] == TS_SYNC and data[i + TS_PACKET_SIZE] == TS_SYNC:
            return data[i:]
    raise ValueError("TS payload not found")


def unwrap_now(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return data
    data = bytes(data)

    try:
        out = unwrap_segment(data)
    except zlib.error:
        return data

    if not out or out[0] != TS_SYNC:
        return data
    return out


@fastapi_app.get("/content/{path}/{host}")
async def content(path: str, host: str):
    try:
        host = step_daddy.content_url(host)
        headers = step_daddy._headers(referer=host)
        async with step_daddy._session.stream(
                "GET", step_daddy.content_url(path), headers=headers) as response:
            if response.status_code != 200:
                return JSONResponse(
                    content={
                        "error": f"Upstream status code: {response.status_code}",
                        "url": f"{response.url}",
                        "details": f"{response.text[:200]}"
                    },
                    status_code=response.status_code
                )

            # ----------------------------------------------------------
            # Encoded media segment
            #
            # We need the complete segment because the PNG/WebP
            # structure cannot necessarily be decoded incrementally.
            # ----------------------------------------------------------

            encoded = bytearray()

            async for chunk in response.aiter_content(
                chunk_size= 16384 * 1024
            ):
                encoded.extend(chunk)

            try:
                ts = unwrap_now(bytes(encoded))

            except Exception as exc:
                return JSONResponse(
                    content={
                        "error": "Unable to decode media segment",
                        "details": str(exc),
                    },
                    status_code=502,
                )

            # ----------------------------------------------------------
            # Send MPEG-TS downstream
            # ----------------------------------------------------------

            async def ts_stream():
                chunk_size = 16384 * 1024

                for offset in range(
                    0,
                    len(ts),
                    chunk_size,
                ):
                    yield ts[offset:offset + chunk_size]

            return StreamingResponse(
                ts_stream(),
                media_type="video/mp2t",
                headers={
                    "Content-Length": str(len(ts)),
                    "Cache-Control": "no-cache",
                },
            )
    except Exception as e:
        return JSONResponse(
            content={
                "error": "An internal server error occurred during content proxying.",
                "details": str(e)
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


async def update_channels():
    while True:
        try:
            await step_daddy.load_channels()
            # A page with no channel cards (e.g. a challenge page) leaves streams gated; retry soon.
            delay = CHANNEL_RETRY_SECONDS if step_daddy.nsfw_ids_unknown() else random.randint(1700, 2000)
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            continue
        except Exception:
            logger.exception("Channel list load failed; retrying in %ss", CHANNEL_RETRY_SECONDS)
            await asyncio.sleep(CHANNEL_RETRY_SECONDS)


def get_channels():
    return step_daddy.channels


def get_channel(channel_id) -> Channel | None:
    if not channel_id or channel_id == "":
        return None
    return next((channel for channel in step_daddy.channels if channel.id == channel_id), None)


@fastapi_app.get("/playlist.m3u8")
def playlist():
    return Response(content=step_daddy.playlist(), media_type="application/vnd.apple.mpegurl", headers={"Content-Disposition": "attachment; filename=playlist.m3u8"})


async def get_schedule():
    return await step_daddy.schedule()


@fastapi_app.get("/logo/{logo}")
async def logo(logo: str):
    url = urlsafe_base64_decode(logo)
    file = url.split("/")[-1]
    if not os.path.exists("./logo-cache"):
        os.makedirs("./logo-cache")
    if os.path.exists(f"./logo-cache/{file}"):
        return FileResponse(f"./logo-cache/{file}")
    try:
        response = await step_daddy._session.get(url, headers=step_daddy._headers())
        if response.status_code == 200:
            with open(f"./logo-cache/{file}", "wb") as f:
                f.write(response.content)
            return FileResponse(f"./logo-cache/{file}")
        else:
            return JSONResponse(content={"error": "Logo not found"}, status_code=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
