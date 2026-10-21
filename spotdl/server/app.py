"""
FastAPI application for the spotDL web server.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from spotdl._version import __version__
from spotdl.server import spotapi_cache
from spotdl.server.jobs import JobManager
from spotdl.server.models import Job, JobError, QueryError, ServerBusyError
from spotdl.server.settings import BITRATES, FORMATS, ServerSettings
from spotdl.types.song import Song
from spotdl.utils.ffmpeg import is_ffmpeg_installed
from spotdl.utils.spotify import SpotifyClient

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

SESSION_COOKIE = "spotdl_session"
CLIENT_COOKIE = "spotdl_client"
CLIENT_ID_REGEX = re.compile(r"[0-9a-f]{32}")

SESSION_MAX_AGE = 30 * 24 * 3600
CLIENT_MAX_AGE = 365 * 24 * 3600

SEARCH_LIMIT = 8
SEARCH_CACHE_SIZE = 256
SEARCH_CACHE_TTL = 10 * 60

# Slim containers ship without a system MIME table, so don't rely on guessing
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
}

# Comment line sent on idle event streams so proxies don't close them
HEARTBEAT_INTERVAL = 15

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
)


class CreateJobRequest(BaseModel):
    """
    Body of `POST /api/jobs`.
    """

    query: str = Field(max_length=1000)
    format: Optional[str] = None
    bitrate: Optional[str] = None


class LoginRequest(BaseModel):
    """
    Body of `POST /api/login`.
    """

    password: str = Field(max_length=1000)


class SearchCache:
    """
    Small in-memory LRU cache for search results.
    """

    def __init__(self, size: int = SEARCH_CACHE_SIZE, ttl: float = SEARCH_CACHE_TTL):
        self.size = size
        self.ttl = ttl
        self.items: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = (
            OrderedDict()
        )

    def get(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """
        Get cached results if they haven't expired.
        """

        item = self.items.get(key)
        if item is None or time.monotonic() - item[0] > self.ttl:
            return None

        self.items.move_to_end(key)
        return item[1]

    def set(self, key: str, results: List[Dict[str, Any]]) -> None:
        """
        Cache results, evicting the least recently used entry when full.
        """

        self.items[key] = (time.monotonic(), results)
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)


def session_token(password: str) -> str:
    """
    Value of the session cookie for a password. Changing the password signs
    everyone out.
    """

    return hmac.new(
        password.encode("utf-8"), b"spotdl-server-session-v1", hashlib.sha256
    ).hexdigest()


def search_result(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Turn a raw Spotify search item into what the UI needs.
    """

    album = item.get("album") or {}
    images = [image for image in album.get("images") or [] if image.get("url")]
    # Prefer the smallest image that still looks sharp in a list
    images.sort(key=lambda image: image.get("width") or 0)
    cover = next(
        (image["url"] for image in images if (image.get("width") or 0) >= 200),
        images[-1]["url"] if images else None,
    )
    track_id = item.get("id")
    url = (item.get("external_urls") or {}).get(
        "spotify"
    ) or f"https://open.spotify.com/track/{track_id}"

    return {
        "url": url,
        "title": item.get("name") or "Unknown",
        "artists": [artist.get("name") for artist in item.get("artists") or []],
        "album": album.get("name"),
        "cover": cover,
        "duration": int((item.get("duration_ms") or 0) / 1000),
        "explicit": bool(item.get("explicit")),
    }


def _log_warmup(future: "asyncio.Future[Any]") -> None:
    if not future.cancelled() and future.exception() is not None:
        logger.warning("Spotify warm-up failed: %s", future.exception())


def create_app(settings: Optional[ServerSettings] = None) -> FastAPI:
    """
    Create the web server application.

    ### Arguments
    - settings: Server settings, read from the environment when omitted.

    ### Returns
    - The FastAPI application.
    """

    settings = settings or ServerSettings.from_env()
    manager = JobManager(settings)
    search_cache = SearchCache()
    expected_session = session_token(settings.password) if settings.password else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        # Every download runs several blocking steps in the default executor,
        # the stock pool (cpu count + 4) is far too small on small instances
        workers = max(32, settings.max_concurrent_jobs * settings.threads * 4)
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="spotdl")
        loop.set_default_executor(executor)

        if settings.spotapi_cache:
            spotapi_cache.install()

        try:
            SpotifyClient.init(
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                headless=True,
                use_official_api=settings.use_official_api,
            )
        except Exception as exc:  # pylint: disable=broad-except
            # Already initialized, e.g. by tests or an embedding application
            logger.debug("Spotify client not initialized: %s", exc)

        if not is_ffmpeg_installed():
            logger.warning(
                "FFmpeg was not found, downloads will fail until it is installed"
            )

        await manager.start()

        if not settings.use_official_api:
            # The first Spotify call bootstraps a web-player session (~10s),
            # pay for it now rather than on the first search
            warmup = loop.run_in_executor(None, Song.search, "spotdl")
            warmup.add_done_callback(_log_warmup)

        logger.info(
            "spotDL server %s ready, saving downloads to %s",
            __version__,
            settings.data_dir,
        )

        try:
            yield
        finally:
            await manager.stop()
            executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        title="spotDL",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.manager = manager
    app.state.settings = settings

    # Dependencies

    def is_authenticated(request: Request) -> bool:
        if expected_session is None:
            return True

        cookie = request.cookies.get(SESSION_COOKIE, "")
        if cookie and hmac.compare_digest(cookie, expected_session):
            return True

        # Scripts can send the password as a bearer token instead
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        return (
            scheme.lower() == "bearer"
            and settings.password is not None
            and hmac.compare_digest(token.strip(), settings.password)
        )

    async def require_auth(request: Request) -> None:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="Sign in to continue")

    def client_id_of(request: Request) -> str:
        client_id = request.cookies.get(CLIENT_COOKIE, "")
        return client_id if CLIENT_ID_REGEX.fullmatch(client_id) else "anonymous"

    async def owner(request: Request) -> str:
        return client_id_of(request)

    async def get_job(job_id: str) -> Job:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Download not found")

        return job

    # Public routes

    public = APIRouter()

    @public.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        response = Response(
            html.replace("{{version}}", __version__),
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache",
                "Content-Security-Policy": CONTENT_SECURITY_POLICY,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
        if client_id_of(request) == "anonymous":
            response.set_cookie(
                CLIENT_COOKIE,
                secrets.token_hex(16),
                max_age=CLIENT_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )

        return response

    @public.get("/api/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @public.get("/api/session")
    def session(request: Request) -> Dict[str, Any]:
        return {
            "version": __version__,
            "auth_required": expected_session is not None,
            "authenticated": is_authenticated(request),
            "formats": FORMATS,
            "bitrates": BITRATES,
            "defaults": {
                "format": settings.default_format,
                "bitrate": settings.default_bitrate,
            },
            "max_tracks": settings.max_tracks,
            "retention_hours": settings.retention_hours,
        }

    @public.post("/api/login")
    async def login(body: LoginRequest, request: Request, response: Response) -> Dict:
        if expected_session is None:
            return {"authenticated": True}

        if not hmac.compare_digest(body.password, settings.password or ""):
            # Slow down password guessing
            await asyncio.sleep(1)
            raise HTTPException(status_code=401, detail="Wrong password")

        response.set_cookie(
            SESSION_COOKIE,
            expected_session,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return {"authenticated": True}

    @public.post("/api/logout")
    def logout(response: Response) -> Dict[str, bool]:
        response.delete_cookie(SESSION_COOKIE)
        return {"authenticated": False}

    # Routes that need a password when one is set

    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    @api.get("/search")
    async def search(q: str) -> Dict[str, Any]:
        query = q.strip()[:200]
        if not query:
            return {"results": []}

        key = query.lower()
        results = search_cache.get(key)
        if results is None:
            loop = asyncio.get_running_loop()
            try:
                raw = await loop.run_in_executor(None, Song.search, query)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Search for %r failed: %s", query, exc)
                raise HTTPException(
                    status_code=502, detail="Spotify search failed, try again"
                ) from exc

            items = ((raw or {}).get("tracks") or {}).get("items") or []
            results = [search_result(item) for item in items[:SEARCH_LIMIT] if item]
            search_cache.set(key, results)

        return {"results": results}

    @api.get("/jobs")
    async def list_jobs(client: str = Depends(owner)) -> Dict[str, Any]:
        return {"jobs": [job.to_dict() for job in manager.for_owner(client)]}

    @api.post("/jobs", status_code=201)
    async def create_job(
        body: CreateJobRequest, client: str = Depends(owner)
    ) -> Dict[str, Any]:
        output_format = body.format or settings.default_format
        bitrate = body.bitrate or settings.default_bitrate
        if output_format not in FORMATS:
            raise HTTPException(status_code=422, detail="Unsupported format")
        if bitrate not in BITRATES:
            raise HTTPException(status_code=422, detail="Unsupported bitrate")

        try:
            job = manager.create(client, body.query, output_format, bitrate)
        except QueryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ServerBusyError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

        return job.to_dict()

    @api.get("/jobs/{job_id}")
    async def read_job(job: Job = Depends(get_job)) -> Dict[str, Any]:
        return job.to_dict()

    @api.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(job: Job = Depends(get_job)) -> Response:
        await manager.delete(job)
        return Response(status_code=204)

    @api.post("/jobs/{job_id}/retry")
    async def retry_job(job: Job = Depends(get_job)) -> Dict[str, Any]:
        try:
            manager.retry(job)
        except JobError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return job.to_dict()

    @api.get("/jobs/{job_id}/tracks/{track_id}/file")
    async def download_track(
        track_id: int, job: Job = Depends(get_job)
    ) -> FileResponse:
        if not 0 <= track_id < len(job.tracks):
            raise HTTPException(status_code=404, detail="Track not found")

        path = manager.track_path(job, job.tracks[track_id])
        if path is None:
            raise HTTPException(status_code=404, detail="This track isn't downloaded")

        return FileResponse(
            path,
            filename=path.name,
            media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        )

    @api.get("/jobs/{job_id}/archive")
    async def download_archive(job: Job = Depends(get_job)) -> FileResponse:
        try:
            path = await manager.archive(job)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        name = re.sub(r'[\\/:*?"<>|]+', "", job.title).strip() or "spotdl"
        return FileResponse(
            path, filename=f"{name[:120]}.zip", media_type="application/zip"
        )

    @api.get("/events")
    async def events(request: Request, client: str = Depends(owner)) -> Response:
        subscriber = manager.subscribe(client)

        def message(event: str, data: Any) -> str:
            return (
                f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
            )

        def snapshot() -> str:
            jobs = [job.to_dict() for job in manager.for_owner(client)]
            return message("snapshot", {"jobs": jobs})

        async def stream() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                yield snapshot()
                while not await request.is_disconnected():
                    try:
                        event, data = await asyncio.wait_for(
                            subscriber.queue.get(), timeout=HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue

                    yield snapshot() if event == "resync" else message(event, data)
            finally:
                manager.unsubscribe(subscriber)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(public)
    app.include_router(api)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
