import asyncio
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from spotdl.server.jobs import JobManager, Subscriber
from spotdl.server.models import (
    QueryError,
    ServerBusyError,
    clean_error,
    cover_thumbnail,
    detect_kind,
    normalize_query,
)
from tests.server.conftest import FakeDownloader, make_song, wait_until_finished

ALBUM_URL = "https://open.spotify.com/album/album1"


@pytest.mark.parametrize(
    "query, expected",
    [
        ("  never gonna give you up ", "never gonna give you up"),
        (
            "spotify:album:4yP0hdKOZPNshxUOjY0cZj",
            "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
        ),
        ("open.spotify.com/track/abc?si=1", "https://open.spotify.com/track/abc?si=1"),
        (
            "https://open.spotify.com/intl-de/playlist/xyz",
            "https://open.spotify.com/intl-de/playlist/xyz",
        ),
        ("https://spotify.link/abcdef", "https://spotify.link/abcdef"),
        (
            "https://music.youtube.com/watch?v=abc",
            "https://music.youtube.com/watch?v=abc",
        ),
        (
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/playlist?list=PL123",
        ),
        (
            "https://www.youtube.com/watch?v=abc|https://open.spotify.com/track/xyz",
            "https://www.youtube.com/watch?v=abc|https://open.spotify.com/track/xyz",
        ),
    ],
)
def test_normalize_query_accepts(query, expected):
    assert normalize_query(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "x" * 1001,
        "saved",
        "all-user-playlists",
        "/etc/passwd.spotdl",
        "https://example.com/track/1",
        "file:///etc/passwd",
        "https://www.youtube.com/watch?v=abc",
    ],
)
def test_normalize_query_rejects(query):
    with pytest.raises(QueryError):
        normalize_query(query)


def test_helpers():
    assert detect_kind(None) == "track"
    assert detect_kind("https://open.spotify.com/intl-fr/album/x") == "album"
    assert detect_kind("https://open.spotify.com/playlist/x") == "playlist"
    assert detect_kind("https://music.youtube.com/playlist?list=OLAK5uy_x") == "album"
    assert detect_kind("https://music.youtube.com/playlist?list=PLx") == "playlist"

    assert (
        cover_thumbnail("https://i.scdn.co/image/ab67616d0000b273abc")
        == "https://i.scdn.co/image/ab67616d00001e02abc"
    )
    assert cover_thumbnail("https://example.com/a.jpg") == "https://example.com/a.jpg"
    assert cover_thumbnail(None) is None

    url = "https://open.spotify.com/track/1"
    assert (
        clean_error(f"{url} - LookupError: No results found", url) == "No results found"
    )
    assert (
        clean_error(
            "http://open.spotify.com/track/1 - FFmpegError: Failed to convert X, "
            "you can find error here: /home/spotdl/errors/ffmpeg.txt",
            url,
        )
        == "Failed to convert X"
    )
    assert clean_error("", url) == "Download failed"


def test_subscriber_overflow_resyncs():
    async def run():
        subscriber = Subscriber("owner", maxsize=2)
        for index in range(5):
            subscriber.push("track", index)

        assert subscriber.queue.qsize() == 1
        assert subscriber.queue.get_nowait() == ("resync", None)

    asyncio.run(run())


async def test_job_downloads_album_and_builds_archive(settings, fake_spotdl):
    fake_spotdl[ALBUM_URL] = [
        make_song(index, list_url=ALBUM_URL) for index in range(3)
    ]
    manager = JobManager(settings)
    await manager.start()
    subscriber = manager.subscribe("me")

    try:
        job = manager.create("me", ALBUM_URL, "mp3", "auto")
        await wait_until_finished(job)

        assert job.status == "done"
        assert job.kind == "album"
        assert job.title == "Test Album"
        assert job.stats() == {"total": 3, "done": 3, "failed": 0, "progress": 100}
        assert len(job.covers) == 2
        assert all(track.file and track.size for track in job.tracks)
        assert manager.for_owner("me") == [job]
        assert manager.for_owner("someone else") == []

        archive = await manager.archive(job)
        with zipfile.ZipFile(archive) as zip_file:
            assert zip_file.namelist() == [
                "1 - Artist - Song 0.mp3",
                "2 - Artist - Song 1.mp3",
                "3 - Artist - Song 2.mp3",
            ]

        events = []
        while not subscriber.queue.empty():
            events.append(subscriber.queue.get_nowait())
        kinds = {event for event, _ in events}
        assert {"job", "track"} <= kinds
        statuses = [data["status"] for event, data in events if event == "job"]
        assert statuses[0] == "resolving"
        assert statuses[-1] == "done"
        progress = [
            data["track"]["progress"]
            for event, data in events
            if event == "track" and data["track"]["id"] == 0
        ]
        assert progress == sorted(progress)
        assert progress[-1] == 100

        state = json.loads((manager.job_dir(job) / "job.json").read_text())
        assert state["status"] == "done"
        assert state["tracks"][0]["song"]["name"] == "Song 0"
    finally:
        manager.unsubscribe(subscriber)
        await manager.stop()


async def test_failed_track_can_be_retried(settings, fake_spotdl):
    fake_spotdl[ALBUM_URL] = [
        make_song(0, list_url=ALBUM_URL),
        make_song(1, name="Broken", list_url=ALBUM_URL),
    ]
    FakeDownloader.failing = {"Broken"}
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", ALBUM_URL, "mp3", "auto")
        await wait_until_finished(job)

        assert job.status == "done"
        assert job.stats()["failed"] == 1
        broken = job.tracks[1]
        assert broken.status == "error"
        assert broken.error == "No results found for song: Artist - Broken"

        await manager.archive(job)
        FakeDownloader.failing = set()
        manager.retry(job)
        assert not (manager.job_dir(job) / "_archive.zip").exists()
        await wait_until_finished(job)

        assert job.status == "done"
        assert job.stats()["done"] == 2
        # Only the failed track was downloaded again
        assert FakeDownloader.instances[-1].downloaded == [broken.url]

        with pytest.raises(Exception, match="Nothing to retry"):
            manager.retry(job)
    finally:
        await manager.stop()


async def test_transient_failures_are_retried_automatically(settings, fake_spotdl):
    fake_spotdl[ALBUM_URL] = [
        make_song(0, list_url=ALBUM_URL),
        make_song(1, name="Flaky", list_url=ALBUM_URL),
    ]
    FakeDownloader.flaky = {"Flaky"}
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", ALBUM_URL, "mp3", "auto")
        await wait_until_finished(job)

        assert job.stats()["done"] == 2
        assert FakeDownloader.attempts == {"Song 0": 1, "Flaky": 2}
    finally:
        await manager.stop()


async def test_unattributed_errors_are_reported(settings, fake_spotdl):
    fake_spotdl["flaky"] = [make_song(0, name="Flaky")]
    FakeDownloader.flaky = {"Flaky"}
    FakeDownloader.failing = {"Flaky"}
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", "flaky", "mp3", "auto")
        await wait_until_finished(job)

        # The automatic second try fails with a different, attributed error
        assert job.tracks[0].error == "No results found for song: Artist - Flaky"
        assert (
            clean_error(
                "Error occurred while reinitializing song: Could not get song info", "x"
            )
            == "Couldn't load this track's details from Spotify"
        )
    finally:
        await manager.stop()


async def test_every_track_failing_fails_the_job(settings, fake_spotdl):
    fake_spotdl["broken song"] = [make_song(0, name="Broken")]
    FakeDownloader.failing = {"Broken"}
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", "broken song", "mp3", "auto")
        await wait_until_finished(job)

        assert job.status == "failed"
        assert job.error == "Couldn't download this track"
        assert job.kind == "track"
        assert job.title == "Broken"
        assert job.subtitle == "Artist"
    finally:
        await manager.stop()


async def test_resolve_errors(settings, fake_spotdl):
    settings.max_tracks = 2
    fake_spotdl["nothing"] = []
    fake_spotdl["missing"] = Exception("No results found for: missing")
    fake_spotdl[ALBUM_URL] = [
        make_song(index, list_url=ALBUM_URL) for index in range(3)
    ]
    manager = JobManager(settings)
    await manager.start()

    try:
        jobs = [
            manager.create("me", query, "mp3", "auto")
            for query in ("nothing", "missing", ALBUM_URL)
        ]
        for job in jobs:
            await wait_until_finished(job)

        assert [job.status for job in jobs] == ["failed"] * 3
        assert jobs[0].error == "No songs found"
        assert jobs[1].error == "No songs found for that search"
        assert jobs[2].error == "This has 3 songs, the limit is 2"
    finally:
        await manager.stop()


async def test_playlist_art_is_filled_in_after_download(
    settings, fake_spotdl, monkeypatch
):
    url = "https://open.spotify.com/playlist/list1"
    fake_spotdl[url] = [make_song(0, list_url=url, cover_url=None)]

    async def download(self, song):
        song.cover_url = "https://i.scdn.co/image/ab67616d0000b273found"
        path = Path(
            self.settings["output"].replace("{artists} - {title}.{output-ext}", "a.mp3")
        )
        path.write_bytes(b"audio")
        return song, path

    monkeypatch.setattr(FakeDownloader, "async_search_and_download", download)
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", url, "mp3", "auto")
        await wait_until_finished(job)

        assert job.tracks[0].cover == "https://i.scdn.co/image/ab67616d0000b273found"
        assert job.covers == ["https://i.scdn.co/image/ab67616d0000b273found"]
    finally:
        await manager.stop()


async def test_duplicate_songs_are_downloaded_once(settings, fake_spotdl):
    fake_spotdl[ALBUM_URL] = [make_song(0, list_url=ALBUM_URL)] * 2
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", ALBUM_URL, "mp3", "auto")
        await wait_until_finished(job)
        assert len(job.tracks) == 1
    finally:
        await manager.stop()


async def test_reuses_files_from_other_jobs(settings, fake_spotdl):
    manager = JobManager(settings)
    await manager.start()

    try:
        first = manager.create("me", "song", "mp3", "auto")
        await wait_until_finished(first)
        second = manager.create("you", "song", "mp3", "auto")
        await wait_until_finished(second)
        other_format = manager.create("you", "song", "flac", "auto")
        await wait_until_finished(other_format)

        assert second.status == "done"
        assert (
            manager.track_path(second, second.tracks[0]).read_bytes() == b"audio:Song 0"
        )
        downloads = [
            len(downloader.downloaded) for downloader in FakeDownloader.instances
        ]
        assert downloads == [1, 0, 1]

        # Deleting the original keeps the copy intact
        await manager.delete(first)
        assert manager.track_path(second, second.tracks[0]) is not None
    finally:
        await manager.stop()


async def test_server_too_busy(settings, fake_spotdl):
    settings.max_queued_jobs = 1
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", "song", "mp3", "auto")
        with pytest.raises(ServerBusyError):
            manager.create("me", "another", "mp3", "auto")
        await wait_until_finished(job)
    finally:
        await manager.stop()


async def test_delete_cancels_and_removes_files(settings, fake_spotdl):
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", "song", "mp3", "auto")
        job_dir = manager.job_dir(job)
        await manager.delete(job)
        await asyncio.gather(job.task, return_exceptions=True)

        assert manager.get(job.id) is None
        assert not job_dir.exists()
        assert job.status == "cancelled"
    finally:
        await manager.stop()


async def test_interrupted_jobs_are_restored(settings, fake_spotdl):
    manager = JobManager(settings)
    await manager.start()
    job = manager.create("me", ALBUM_URL, "mp3", "auto")
    await wait_until_finished(job)
    await manager.stop()

    # Pretend the server died halfway through the download
    state_file = manager.job_dir(job) / "job.json"
    state = json.loads(state_file.read_text())
    state["status"] = "running"
    state["tracks"][0].update(status="active", progress=40, file=None)
    state_file.write_text(json.dumps(state))

    restarted = JobManager(settings)
    await restarted.start()
    try:
        restored = restarted.get(job.id)
        assert restored.status == "failed"
        assert restored.error == "Interrupted by a server restart"
        assert restored.tracks[0].error == "Interrupted by a server restart"

        restarted.retry(restored)
        await wait_until_finished(restored)
        assert restored.status == "done"
    finally:
        await restarted.stop()


async def test_shutdown_keeps_running_jobs_retryable(settings, fake_spotdl):
    manager = JobManager(settings)
    await manager.start()
    job = manager.create("me", "song", "mp3", "auto")
    await manager.stop()

    state = json.loads((manager.job_dir(job) / "job.json").read_text())
    assert state["status"] in ("resolving", "queued", "running")


async def test_cleanup_removes_expired_jobs_and_orphans(settings, fake_spotdl):
    settings.retention_hours = 1
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", "song", "mp3", "auto")
        await wait_until_finished(job)
        fresh = manager.create("me", "fresh", "mp3", "auto")
        await wait_until_finished(fresh)
        job.finished_at = time.time() - 2 * 3600

        orphan = settings.jobs_dir / "orphan"
        orphan.mkdir()
        old = time.time() - 2 * 3600
        os.utime(orphan, (old, old))

        await manager.cleanup()

        assert manager.get(job.id) is None
        assert manager.get(fresh.id) is fresh
        assert not orphan.exists()
    finally:
        await manager.stop()


async def test_track_path_stays_inside_job_dir(settings, fake_spotdl):
    manager = JobManager(settings)
    await manager.start()

    try:
        job = manager.create("me", "song", "mp3", "auto")
        await wait_until_finished(job)
        track = job.tracks[0]
        assert manager.track_path(job, track) is not None

        track.file = "../../outside.mp3"
        (settings.data_dir / "outside.mp3").write_bytes(b"secret")
        assert manager.track_path(job, track) is None
    finally:
        await manager.stop()
