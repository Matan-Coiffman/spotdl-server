"""
Download jobs for the spotDL web server.

A job is one request from the UI: a track, album, playlist or artist link, or a
search term. The job first resolves its query into songs, then downloads every
song with spotDL's `Downloader` into its own directory. Progress is published
as events that the web UI receives over Server-Sent Events.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

from spotdl.download.downloader import Downloader
from spotdl.download.progress_handler import ProgressHandler, SongTracker
from spotdl.server.models import (
    Job,
    JobError,
    ServerBusyError,
    Track,
    clean_error,
    detect_kind,
    normalize_query,
    song_state,
)
from spotdl.server.settings import ServerSettings
from spotdl.types.song import Song
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.search import get_simple_songs

__all__ = ["JobManager", "Subscriber"]

logger = logging.getLogger(__name__)

# How often expired jobs and orphaned directories are removed, in seconds
CLEANUP_INTERVAL = 10 * 60

# Directories in the jobs folder that no job owns are removed after this long
ORPHAN_AGE = 60 * 60

ARCHIVE_NAME = "_archive.zip"
STATE_FILE = "job.json"


class Subscriber:
    """
    A connected UI that receives events for one owner's jobs.
    """

    def __init__(self, owner: str, maxsize: int = 1000):
        self.owner = owner
        self.queue: "asyncio.Queue[Tuple[str, Any]]" = asyncio.Queue(maxsize)

    def push(self, event: str, data: Any) -> None:
        """
        Queue an event without blocking.
        """

        try:
            self.queue.put_nowait((event, data))
        except asyncio.QueueFull:
            # The client can't keep up, have it start over from a fresh snapshot
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(("resync", None))


class JobManager:
    """
    Creates, runs, tracks and cleans up jobs.
    """

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.jobs: Dict[str, Job] = {}
        self._subscribers: Dict[str, Set[Subscriber]] = {}
        self._song_locks: Dict[str, List[Any]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._slots: Optional[asyncio.Semaphore] = None
        self._cleanup_task: Optional["asyncio.Task[None]"] = None
        self._stopping = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """
        The event loop the manager was started on.
        """

        if self._loop is None:
            raise RuntimeError("JobManager.start() has not been called")

        return self._loop

    async def start(self) -> None:
        """
        Load jobs from disk and start the cleanup loop.
        """

        self._loop = asyncio.get_running_loop()
        self._slots = asyncio.Semaphore(self.settings.max_concurrent_jobs)
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """
        Cancel running jobs and the cleanup loop. Running jobs are saved as
        interrupted so they can be retried after a restart.
        """

        self._stopping = True
        tasks = [job.task for job in self.jobs.values() if job.task]
        if self._cleanup_task:
            tasks.append(self._cleanup_task)

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

    # Queries

    def get(self, job_id: str) -> Optional[Job]:
        """
        Find a job by id.
        """

        return self.jobs.get(job_id)

    def for_owner(self, owner: str) -> List[Job]:
        """
        All of an owner's jobs, newest first.
        """

        jobs = [job for job in self.jobs.values() if job.owner == owner]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def job_dir(self, job: Job) -> Path:
        """
        The directory a job downloads into.
        """

        return self.settings.jobs_dir / job.id

    def track_path(self, job: Job, track: Track) -> Optional[Path]:
        """
        Path to a downloaded track, if it exists and is inside the job directory.
        """

        if track.status != "done" or not track.file:
            return None

        job_dir = self.job_dir(job).resolve()
        path = (job_dir / track.file).resolve()
        if path.parent != job_dir or not path.is_file():
            return None

        return path

    # Commands

    def create(self, owner: str, query: str, output_format: str, bitrate: str) -> Job:
        """
        Create a job and start working on it.

        ### Arguments
        - owner: Id of the browser that created the job.
        - query: Link or search term, see `normalize_query`.
        - output_format: Audio format, e.g. mp3.
        - bitrate: Bitrate passed to spotDL, e.g. auto or 320k.

        ### Returns
        - The new job.
        """

        query = normalize_query(query)

        active = sum(1 for job in self.jobs.values() if job.is_active)
        if active >= self.settings.max_queued_jobs:
            raise ServerBusyError(
                "The server is busy right now, try again in a few minutes"
            )

        job = Job(
            id=secrets.token_hex(8),
            owner=owner,
            query=query,
            format=output_format,
            bitrate=bitrate,
            kind=detect_kind(query) if "://" in query else "track",
            title=query,
        )
        self.jobs[job.id] = job
        # Saved right away so a restart before the job runs can't lose it
        self._save(job)

        logger.info("Job %s created: %s", job.id, query)
        self._publish_job(job)
        job.task = asyncio.create_task(self._run(job))

        return job

    def retry(self, job: Job) -> Job:
        """
        Download a finished job's failed tracks again, or resolve it again if
        resolving failed.
        """

        if job.is_active:
            raise JobError("This download is still running")

        failed = [track for track in job.tracks if track.status != "done"]
        if job.tracks and not failed:
            raise JobError("Nothing to retry, every track downloaded")

        for track in failed:
            track.reset()

        job.status = "resolving" if not job.tracks else "queued"
        job.error = None
        job.finished_at = None
        self._remove_archive(job)

        logger.info("Job %s retrying %d tracks", job.id, len(failed))
        self._publish_job(job)
        job.task = asyncio.create_task(self._run(job, failed or None))

        return job

    async def delete(self, job: Job) -> None:
        """
        Cancel a job if it's running and delete its files.
        """

        self.jobs.pop(job.id, None)
        if job.task and not job.task.done():
            job.task.cancel()
            job.status = "cancelled"

        self._publish(job.owner, "removed", {"id": job.id})
        await self.loop.run_in_executor(
            None, lambda: shutil.rmtree(self.job_dir(job), ignore_errors=True)
        )

        logger.info("Job %s deleted", job.id)

    async def archive(self, job: Job) -> Path:
        """
        Zip a job's downloaded tracks, building the archive on first request.
        """

        tracks = [
            (track, path)
            for track in job.tracks
            if (path := self.track_path(job, track)) is not None
        ]
        if not tracks:
            raise JobError("No downloaded tracks yet")

        archive = self.job_dir(job) / ARCHIVE_NAME
        async with job.archive_lock:
            if not archive.is_file():
                await self.loop.run_in_executor(
                    None, _build_archive, archive, tracks, len(job.tracks)
                )

        return archive

    # Events

    def subscribe(self, owner: str) -> Subscriber:
        """
        Start receiving events for an owner's jobs.
        """

        subscriber = Subscriber(owner)
        self._subscribers.setdefault(owner, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """
        Stop receiving events.
        """

        subscribers = self._subscribers.get(subscriber.owner)
        if subscribers is None:
            return

        subscribers.discard(subscriber)
        if not subscribers:
            del self._subscribers[subscriber.owner]

    def _publish(self, owner: str, event: str, data: Any) -> None:
        for subscriber in list(self._subscribers.get(owner, ())):
            subscriber.push(event, data)

    def _publish_job(self, job: Job) -> None:
        if job.id in self.jobs:
            self._publish(job.owner, "job", job.to_dict())

    def _publish_track(self, job: Job, track: Track) -> None:
        if job.id in self.jobs:
            self._publish(
                job.owner, "track", {"job_id": job.id, "track": track.to_dict()}
            )

    # Running

    async def _run(self, job: Job, tracks: Optional[List[Track]] = None) -> None:
        assert self._slots is not None

        try:
            if not job.tracks:
                await self._resolve(job)
                tracks = job.tracks

            job.status = "queued"
            self._publish_job(job)

            async with self._slots:
                job.status = "running"
                self._publish_job(job)
                tracks = tracks or job.tracks
                await self._download(job, tracks)

                # Most failures are transient (rate limits, flaky metadata
                # lookups), so give failed tracks one more try
                failed = [track for track in tracks if track.status == "error"]
                if failed:
                    logger.info("Job %s retrying %d tracks", job.id, len(failed))
                    for track in failed:
                        track.reset()
                    self._publish_job(job)
                    await self._download(job, failed)

            stats = job.stats()
            job.status = "done" if stats["done"] else "failed"
            if not stats["done"]:
                job.error = (
                    "Couldn't download this track"
                    if stats["total"] == 1
                    else "None of the tracks could be downloaded"
                )

            logger.info(
                "Job %s finished: %d/%d tracks", job.id, stats["done"], stats["total"]
            )
        except asyncio.CancelledError:
            if not self._stopping:
                job.status = "cancelled"
            raise
        except Exception as exc:  # pylint: disable=broad-except
            if not isinstance(exc, JobError):
                logger.exception("Job %s failed", job.id)
            job.status = "failed"
            job.error = str(exc) or exc.__class__.__name__
            for track in job.tracks:
                if track.status in ("queued", "active"):
                    track.status = "error"
                    track.progress = 100
                    track.message = "Failed"
                    track.error = job.error
        finally:
            job.finished_at = time.time()
            if job.id in self.jobs:
                self._save(job)
                self._publish_job(job)

    async def _resolve(self, job: Job) -> None:
        job.status = "resolving"
        self._publish_job(job)

        try:
            songs: List[Song] = await self.loop.run_in_executor(
                None, get_simple_songs, [job.query]
            )
        except Exception as exc:
            logger.warning("Job %s could not be resolved: %r", job.id, exc)
            raise JobError(_resolve_error(exc)) from exc

        # The same song can appear twice in a playlist, download it once
        unique: Dict[str, Song] = {}
        for song in songs:
            unique.setdefault(song.url, song)
        songs = list(unique.values())

        if not songs:
            raise JobError("No songs found")

        if len(songs) > self.settings.max_tracks:
            raise JobError(
                f"This has {len(songs)} songs, the limit is {self.settings.max_tracks}"
            )

        first = songs[0]
        job.kind = detect_kind(first.list_url) if first.list_url else "track"
        if job.kind == "track":
            job.title = first.name
            job.subtitle = ", ".join(first.artists or [first.artist])
        else:
            job.title = first.list_name or first.album_name or job.query
            job.subtitle = first.album_artist if job.kind == "album" else ""
            if len({song.list_url for song in songs}) > 1:
                job.kind = "playlist"
                job.title = "Playlists"

        covers: List[str] = []
        for song in songs:
            if song.cover_url and song.cover_url not in covers:
                covers.append(song.cover_url)
            if len(covers) == 4:
                break
        job.covers = covers

        job.tracks = [Track.from_song(index, song) for index, song in enumerate(songs)]
        self._save(job)

    def _downloader_settings(self, job: Job) -> Dict[str, Any]:
        settings = self.settings
        return {
            **DOWNLOADER_OPTIONS,
            "audio_providers": settings.audio_providers,
            "lyrics_providers": settings.lyrics_providers,
            "cookie_file": settings.cookie_file,
            "proxy": settings.proxy,
            "format": job.format,
            "bitrate": job.bitrate,
            "output": str(self.job_dir(job) / "{artists} - {title}.{output-ext}"),
            "threads": settings.threads,
            "log_level": settings.log_level,
            "simple_tui": True,
            "overwrite": "skip",
            "scan_for_songs": False,
            "fetch_albums": False,
            "archive": None,
            "m3u": None,
            "save_file": None,
            "save_errors": None,
            "print_errors": False,
            "load_config": False,
        }

    async def _download(self, job: Job, tracks: List[Track]) -> None:
        settings = self._downloader_settings(job)
        downloader = await self.loop.run_in_executor(
            None, lambda: Downloader(settings, loop=self.loop)  # type: ignore
        )

        def on_progress(tracker: SongTracker, message: str) -> None:
            # Called from the event loop and from yt-dlp's worker threads
            self.loop.call_soon_threadsafe(
                self._on_progress, job, tracker.song.url, int(tracker.progress), message
            )

        handler = ProgressHandler(
            simple_tui=True, update_callback=on_progress, web_ui=True
        )
        # ProgressTracker keeps songs in a class attribute, give this job its own
        handler.progress_tracker.songs = {}
        handler.set_song_count(len(tracks))
        downloader.progress_handler = handler

        slots = asyncio.Semaphore(self.settings.threads)

        async def download(track: Track) -> None:
            async with slots:
                await self._download_track(job, downloader, track)

        await asyncio.gather(*(download(track) for track in tracks))

    async def _download_track(
        self, job: Job, downloader: Downloader, track: Track
    ) -> None:
        track.status = "active"
        track.progress = 2
        track.message = "Getting metadata"
        self._publish_track(job, track)

        song = Song.from_dict(track.song)
        errors_before = len(downloader.errors)
        path: Optional[Path] = None

        try:
            async with self._song_lock(track.url):
                path = await self._reuse_download(job, track)
                if path is None:
                    song, path = await downloader.async_search_and_download(song)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Job %s: %s failed", job.id, song.display_name)
            downloader.errors.append(f"{track.url} - {exc.__class__.__name__}: {exc}")

        if path is not None and path.is_file():
            track.status = "done"
            track.progress = 100
            track.message = "Done"
            track.file = path.name
            track.size = path.stat().st_size
            track.song = song_state(song)
            # A zip built before this track finished would be missing it
            self._remove_archive(job)
            self._backfill_art(job, track, song)
        else:
            error = _find_error(downloader.errors[errors_before:], track, song)
            track.status = "error"
            track.progress = 100
            track.message = "Failed"
            track.error = clean_error(error, track.url)

        self._publish_track(job, track)

    def _backfill_art(self, job: Job, track: Track, song: Song) -> None:
        """
        Playlist listings come without album art; spotDL looks it up while
        downloading, so keep it for the UI.
        """

        track.cover = track.cover or song.cover_url
        track.album = track.album or song.album_name
        if track.cover and len(job.covers) < 4 and track.cover not in job.covers:
            job.covers.append(track.cover)
            self._publish_job(job)

    def _on_progress(self, job: Job, url: str, progress: int, message: str) -> None:
        track = job.track_for(url)
        if track is None or track.status != "active" or message == "Error":
            return

        progress = max(2, min(progress, 99))
        changed = message != track.message or progress - track.progress >= 2
        track.progress = max(track.progress, progress)
        track.message = message

        if changed:
            self._publish_track(job, track)

    async def _reuse_download(self, job: Job, track: Track) -> Optional[Path]:
        """
        Copy an identical file another job already downloaded, if there is one.
        """

        for other in self.jobs.values():
            if other is job or (other.format, other.bitrate) != (
                job.format,
                job.bitrate,
            ):
                continue

            source_track = other.track_for(track.url)
            source = source_track and self.track_path(other, source_track)
            if not source:
                continue

            target = self.job_dir(job) / source.name
            try:
                await self.loop.run_in_executor(None, _link_or_copy, source, target)
            except OSError as exc:
                logger.debug("Could not reuse %s: %s", source, exc)
                return None

            logger.info("Job %s reused %s from job %s", job.id, source.name, other.id)
            return target

        return None

    @contextlib.asynccontextmanager
    async def _song_lock(self, url: str) -> AsyncIterator[None]:
        # spotDL downloads into a shared temp folder named after the YouTube id,
        # so two jobs must not download the same song at the same time
        entry = self._song_locks.setdefault(url, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self._song_locks[url]

    # Persistence and cleanup

    def _save(self, job: Job) -> None:
        if job.id not in self.jobs:
            return

        job_dir = self.job_dir(job)
        job_dir.mkdir(parents=True, exist_ok=True)
        state_file = job_dir / STATE_FILE
        temp_file = state_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(job.to_state()), encoding="utf-8")
        os.replace(temp_file, state_file)

    def _load(self) -> None:
        for state_file in self.settings.jobs_dir.glob(f"*/{STATE_FILE}"):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                job = Job.from_state(state)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Skipping unreadable job %s: %s", state_file, exc)
                continue

            if job.is_active:
                # The server stopped while this job was running
                for track in job.tracks:
                    if track.status not in ("done", "error"):
                        track.status = "error"
                        track.progress = 100
                        track.message = "Failed"
                        track.error = "Interrupted by a server restart"
                done = job.stats()["done"]
                job.status = "done" if done else "failed"
                job.error = None if done else "Interrupted by a server restart"
                job.finished_at = time.time()

            self.jobs[job.id] = job
            if job.status != state["status"]:
                self._save(job)

        if self.jobs:
            logger.info(
                "Loaded %d jobs from %s", len(self.jobs), self.settings.jobs_dir
            )

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await self.cleanup()
            except Exception:  # pylint: disable=broad-except
                logger.exception("Cleanup failed")

            await asyncio.sleep(CLEANUP_INTERVAL)

    async def cleanup(self) -> None:
        """
        Delete jobs older than the retention period and directories no job owns.
        """

        if self.settings.retention_hours > 0:
            cutoff = time.time() - self.settings.retention_hours * 3600
            expired = [
                job
                for job in self.jobs.values()
                if not job.is_active and (job.finished_at or job.created_at) < cutoff
            ]
            for job in expired:
                logger.info("Job %s expired", job.id)
                await self.delete(job)

        def remove_orphans() -> None:
            cutoff = time.time() - ORPHAN_AGE
            for path in self.settings.jobs_dir.iterdir():
                if (
                    path.is_dir()
                    and path.name not in self.jobs
                    and path.stat().st_mtime < cutoff
                ):
                    logger.info("Removing orphaned directory %s", path)
                    shutil.rmtree(path, ignore_errors=True)

        await self.loop.run_in_executor(None, remove_orphans)

    def _remove_archive(self, job: Job) -> None:
        archive = self.job_dir(job) / ARCHIVE_NAME
        archive.unlink(missing_ok=True)


def _find_error(errors: List[str], track: Track, song: Song) -> str:
    """
    Pick this track's message out of the errors logged while it downloaded.
    Other tracks of the job may have logged errors in the meantime.
    """

    for error in errors:
        if error.startswith(f"{track.url} - ") or error.startswith(f"{song.url} - "):
            return error

    for error in errors:
        if song.display_name in error:
            return error

    # spotDL logs some errors without saying which song they belong to
    unattributed = [error for error in errors if "://" not in error.split(" - ")[0]]
    if len(unattributed) == 1:
        return unattributed[0]

    return "Couldn't find a matching audio source"


def _resolve_error(exc: Exception) -> str:
    # The underlying errors are logged; they rarely mean anything to the user
    if "No results found" in str(exc):
        return "No songs found for that search"

    return "Couldn't find that on Spotify. Check the link and try again."


def _link_or_copy(source: Path, target: Path) -> None:
    if target.exists():
        return

    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _build_archive(archive: Path, tracks: List[Tuple[Track, Path]], total: int) -> None:
    width = len(str(total))
    temp_archive = archive.with_suffix(".tmp")
    # Audio is already compressed, storing is as small as deflating and much faster
    with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_STORED) as zip_file:
        for track, path in tracks:
            name = (
                path.name if total == 1 else f"{track.id + 1:0{width}d} - {path.name}"
            )
            zip_file.write(path, arcname=name)

    os.replace(temp_archive, archive)
