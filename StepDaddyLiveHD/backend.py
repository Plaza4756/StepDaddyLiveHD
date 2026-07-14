import os
import asyncio
# import httpx
from StepDaddyLiveHD.step_daddy import StepDaddy, Channel
from fastapi import Response, status, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from .utils import urlsafe_base64_decode
from urllib.parse import quote, urlparse
import random
import curl_cffi.requests

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
                        "details": f"{response.text[:200]}"
                    },
                    status_code=response.status_code
                )

            async def proxy_stream():
                async for chunk in response.aiter_content(chunk_size=1024*1024):
                    yield chunk
            return StreamingResponse(proxy_stream(), media_type="application/octet-stream")
    except curl_cffi.requests.exceptions.HTTPError as e:
        # If we catch a specific httpx error, we can often extract the correct status code
        # from the exception object itself.
        status_code = getattr(e.response, 'status_code',
                              status.HTTP_503_SERVICE_UNAVAILABLE)
        return JSONResponse(
            content={
                "error": f"Failed to connect or stream from upstream source.",
                "details": str(e)
            },
            status_code=status_code
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
            # await step_daddy.resolve_base_url()
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
   # except httpx.ConnectTimeout:
   #    return JSONResponse(content={"error": "Request timed out"}, status_code=status.HTTP_504_GATEWAY_TIMEOUT)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
