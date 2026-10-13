import os
import asyncio
from StepDaddyLiveHD.step_daddy import StepDaddy, Channel
from fastapi import Response, status, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from .utils import urlsafe_base64_decode
from urllib.parse import quote, urlparse
import random
import curl_cffi.requests
import gzip
import struct

fastapi_app = FastAPI()
step_daddy = StepDaddy()


@fastapi_app.get("/stream/{channel_id}.m3u8")
async def stream(channel_id: str):
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


# ---------------------------------------------------------------------------
# PNG helpers
# ---------------------------------------------------------------------------

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


def png_rgb(data: bytes):
    """
    Decode the subset of PNG used by the original JavaScript:

        - 8-bit
        - RGB or RGBA
        - non-interlaced

    Returns RGB bytes or None.
    """
    if len(data) < 8 or data[:2] != b"\x89P":
        return None

    offset = 8
    width = height = 0
    depth = color_type = interlace = 0
    idats = []

    while offset + 8 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]

        if length > len(data) - offset - 12:
            return None

        chunk_type = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]

        if chunk_type == b"IHDR":
            if len(chunk) < 13:
                return None

            width, height = struct.unpack_from(">II", chunk, 0)
            depth = chunk[8]
            color_type = chunk[9]
            interlace = chunk[12]

        elif chunk_type == b"IDAT":
            idats.append(chunk)

        elif chunk_type == b"IEND":
            break

        offset += 12 + length

    # Same restrictions as the original JS.
    if (
        not width
        or not height
        or depth != 8
        or interlace
        or color_type not in (2, 6)
    ):
        return None

    try:
        raw = gzip_or_zlib_decompress(b"".join(idats))
    except Exception:
        return None

    bytes_per_pixel = 4 if color_type == 6 else 3
    stride = width * bytes_per_pixel

    rgb = bytearray(width * height * 3)

    src = 0
    dst = 0
    previous = bytearray(stride)

    for _ in range(height):
        if src + 1 + stride > len(raw):
            return None

        filter_type = raw[src]
        src += 1

        row = raw[src:src + stride]
        src += stride

        reconstructed = bytearray(stride)

        for i in range(stride):
            left = (
                reconstructed[i - bytes_per_pixel]
                if i >= bytes_per_pixel
                else 0
            )

            above = previous[i]

            upper_left = (
                previous[i - bytes_per_pixel]
                if i >= bytes_per_pixel
                else 0
            )

            value = row[i]

            if filter_type == 1:
                value += left

            elif filter_type == 2:
                value += above

            elif filter_type == 3:
                value += (left + above) >> 1

            elif filter_type == 4:
                value += paeth(left, above, upper_left)

            elif filter_type != 0:
                return None

            reconstructed[i] = value & 0xFF

        if color_type == 2:
            # RGB
            rgb[dst:dst + stride] = reconstructed
            dst += stride

        else:
            # RGBA -> RGB
            for i in range(0, stride, 4):
                rgb[dst] = reconstructed[i]
                rgb[dst + 1] = reconstructed[i + 1]
                rgb[dst + 2] = reconstructed[i + 2]
                dst += 3

        previous = reconstructed

    return bytes(rgb)


def inflate_zlib(data: bytes) -> bytes:
    import zlib
    return zlib.decompress(data)


def gzip_or_zlib_decompress(data: bytes) -> bytes:
    """
    PNG IDAT uses zlib, not gzip.
    Kept separate from the gzip payload decoder.
    """
    return inflate_zlib(data)


# ---------------------------------------------------------------------------
# WebP EXIF wrapper
# ---------------------------------------------------------------------------

def webp_exif_ts(data: bytes):
    """
    Equivalent to the JS webpExifTS().

    Looks for:

        RIFF .... WEBP
                     EXIF

    and validates the custom TS signature at byte 0 and byte 188.
    """

    if len(data) < 16:
        return None

    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None

    offset = 12

    while offset + 8 <= len(data):
        chunk_type = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]

        offset += 8

        if offset + size > len(data):
            return None

        if chunk_type == b"EXIF":
            payload = data[offset:offset + size]

            if (
                len(payload) >= 189
                and payload[0] == 0x47
                and payload[188] == 0x47
            ):
                return payload

            return None

        # RIFF chunks are padded to an even size.
        offset += size + (size & 1)

    return None


# ---------------------------------------------------------------------------
# PNG IEND wrapper
# ---------------------------------------------------------------------------

def png_iend_ts(data: bytes):
    """
    Equivalent to pngIendTS().

    Looks for MPEG-TS immediately after PNG's IEND chunk.
    """

    if len(data) < 16:
        return None

    if data[0] != 0x89 or data[1] != 0x50:
        return None

    offset = 8

    while offset + 8 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]

        if length > len(data) - offset - 12:
            return None

        chunk_type = data[offset + 4:offset + 8]

        offset += 8 + length + 4

        if chunk_type == b"IEND":
            if (
                offset < len(data)
                and data[offset] == 0x47
                and offset + 188 < len(data)
                and data[offset + 188] == 0x47
            ):
                return data[offset:]

            return None

    return None


# ---------------------------------------------------------------------------
# PNG pixel-hidden wrapper
# ---------------------------------------------------------------------------

def unwrap_pixels(data: bytes):
    """
    Equivalent to the original unwrapPixels().

    The decoded RGB data starts with:

        TIKTIKPX
        <4-byte big-endian gzip length>
        <gzip data>
    """

    rgb = png_rgb(data)

    if not rgb or len(rgb) < 12:
        return None

    if rgb[:8] != b"TIKTIKPX":
        return None

    size = struct.unpack_from(">I", rgb, 8)[0]

    if size <= 0 or 12 + size > len(rgb):
        return None

    gz = rgb[12:12 + size]

    if len(gz) < 2 or gz[:2] != b"\x1f\x8b":
        return None

    try:
        ts = gzip.decompress(gz)
    except Exception:
        return None

    if not ts or ts[0] != 0x47:
        return None

    return ts


# ---------------------------------------------------------------------------
# Main decoder
# ---------------------------------------------------------------------------

def unwrap_ts(data: bytes) -> bytes:
    """
    Convert an encoded upstream segment into MPEG-TS.

    Supported formats from the original JavaScript:

      1. WebP EXIF
      2. PNG IEND
      3. PNG pixel-hidden gzip
      4. TIKTIKRAW
      5. TIKTIKTGZ
      6. Raw MPEG-TS
    """

    # 1. WebP EXIF
    result = webp_exif_ts(data)
    if result:
        return result

    # 2/3. PNG formats
    is_png = (
        len(data) >= 8
        and data[0] == 0x89
        and data[1] == 0x50
    )

    if is_png:

        # TS after IEND
        result = png_iend_ts(data)
        if result:
            return result

        # TS hidden in PNG pixels
        try:
            result = unwrap_pixels(data)
            if result:
                return result
        except Exception:
            pass

        raise ValueError("TS payload not found in PNG")

    # 4. TIKTIKRAW
    marker = b"TIKTIKRAW"

    for pos in find_all(data, marker):
        ts = data[pos + len(marker):]

        if ts and ts[0] == 0x47:
            return ts

    # 5. TIKTIKTGZ
    marker = b"TIKTIKTGZ"

    for pos in find_all(data, marker):
        try:
            ts = gzip.decompress(data[pos + len(marker):])

            if ts and ts[0] == 0x47:
                return ts
        except Exception:
            continue

    # 6. Already MPEG-TS
    for pos in range(len(data) - 188):
        if data[pos] == 0x47 and data[pos + 188] == 0x47:
            return data[pos:]

    raise ValueError("TS payload not found")


def find_all(data: bytes, needle: bytes):
    """Yield every occurrence of needle."""
    start = 0

    while True:
        pos = data.find(needle, start)

        if pos < 0:
            return

        yield pos
        start = pos + 1


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
                chunk_size=8192 * 1024
            ):
                encoded.extend(chunk)

            try:
                ts = unwrap_ts(bytes(encoded))

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
                chunk_size = 1024 * 1024

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
            await asyncio.sleep(random.randint(1700, 2000))
        except asyncio.CancelledError:
            continue


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
