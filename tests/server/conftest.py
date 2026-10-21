import asyncio
from collections import Counter
from pathlib import Path
from typing import Dict, List

import pytest

from spotdl.server import jobs as jobs_module
from spotdl.server.settings import ServerSettings
from spotdl.types.song import Song


def make_song(index: int, name: str = "", list_url: str = "", **extra) -> Song:
    """
    Build a song with enough metadata for the server.
    """

    data = {
        "name": name or f"Song {index}",
        "artists": ["Artist"],
        "artist": "Artist",
        "album_name": "Album",
        "album_artist": "Artist",
        "duration": 180 + index,
        "url": f"https://open.spotify.com/track/track{index}",
        "song_id": f"track{index}",
        "cover_url": f"https://i.scdn.co/image/ab67616d0000b273cover{index % 2}",
    }
    if list_url:
        data.update(list_url=list_url, list_name="Test Album", list_length=0)
    data.update(extra)

    return Song.from_missing_data(**data)


class FakeDownloader:
    """
    Stands in for spotdl's Downloader: reports progress through the real
    progress handler and writes a small file instead of downloading.
    """

    instances: List["FakeDownloader"] = []
    failing: set = set()
    # Songs that fail on their first attempt only
    flaky: set = set()
    attempts: Counter = Counter()

    def __init__(self, settings, loop=None):
        self.settings = settings
        self.errors: List[str] = []
        self.progress_handler = None
        self.downloaded: List[str] = []
        FakeDownloader.instances.append(self)

    async def async_search_and_download(self, song: Song):
        tracker = self.progress_handler.get_new_tracker(song)
        tracker.notify_searching()
        await asyncio.sleep(0.01)
        FakeDownloader.attempts[song.name] += 1

        if (
            song.name in FakeDownloader.flaky
            and FakeDownloader.attempts[song.name] == 1
        ):
            # spotDL logs metadata failures without the song's url
            self.errors.append(
                "Error occurred while reinitializing song: Could not get song info"
            )
            return song, None

        if song.name in FakeDownloader.failing:
            self.errors.append(
                f"{song.url} - LookupError: No results found for song: {song.display_name}"
            )
            tracker.notify_error("failed", LookupError("no results"), True)
            return song, None

        tracker.progress = 60
        tracker.update("Downloading")
        output = self.settings["output"].replace(
            "{artists} - {title}.{output-ext}",
            f"{song.artist} - {song.name}.{self.settings['format']}",
        )
        path = Path(output)
        path.write_bytes(b"audio:" + song.name.encode())
        self.downloaded.append(song.url)
        tracker.notify_complete()

        return song, path


@pytest.fixture()
def fake_spotdl(monkeypatch):
    """
    Replace query resolution and downloading with fakes.

    Queries map to songs through the returned dict; unknown queries resolve to
    one song.
    """

    catalog: Dict[str, List[Song]] = {}

    def get_simple_songs(query, *args, **kwargs):
        songs = catalog.get(query[0])
        if songs is None:
            return [make_song(0)]
        if isinstance(songs, Exception):
            raise songs
        return list(songs)

    FakeDownloader.instances = []
    FakeDownloader.failing = set()
    FakeDownloader.flaky = set()
    FakeDownloader.attempts = Counter()
    monkeypatch.setattr(jobs_module, "get_simple_songs", get_simple_songs)
    monkeypatch.setattr(jobs_module, "Downloader", FakeDownloader)

    return catalog


@pytest.fixture()
def settings(tmp_path):
    return ServerSettings(data_dir=tmp_path / "data", spotapi_cache=False)


async def wait_until_finished(job, timeout: float = 5.0):
    """
    Wait for a job to leave the active states.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while job.is_active:
        if loop.time() > deadline:
            raise AssertionError(f"job still {job.status}")
        await asyncio.sleep(0.01)

    # Let the job task finish its bookkeeping
    if job.task:
        await asyncio.gather(job.task, return_exceptions=True)
