"""
Data model of the spotDL web server: jobs, their tracks, and validation of the
queries users submit.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from spotdl.types.song import Song

__all__ = [
    "ACTIVE_STATUSES",
    "Job",
    "Track",
    "JobError",
    "QueryError",
    "ServerBusyError",
    "normalize_query",
    "detect_kind",
    "cover_thumbnail",
    "clean_error",
    "song_state",
]

ACTIVE_STATUSES = ("resolving", "queued", "running")

STATE_VERSION = 1

SPOTIFY_URL_REGEX = re.compile(
    r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|album|playlist|artist|user)/"
)
SPOTIFY_URI_REGEX = re.compile(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]+)")

# Spotify CDN covers come in 640px by default, the UI only needs 300px
COVER_640 = "ab67616d0000b273"
COVER_300 = "ab67616d00001e02"

ALLOWED_HOSTS = {
    "open.spotify.com",
    "spotify.link",
    "music.youtube.com",
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "soundcloud.com",
    "m.soundcloud.com",
}

# Queries that make spotDL read local files or a logged-in user's library
RESERVED_QUERIES = {
    "saved",
    "all-user-playlists",
    "all-user-followed-artists",
    "all-user-saved-albums",
    "all-saved-playlists",
}


class JobError(Exception):
    """
    Raised when a job can't be created or changed.
    """


class QueryError(JobError):
    """
    Raised when a query isn't something the server will download.
    """


class ServerBusyError(JobError):
    """
    Raised when too many jobs are already waiting.
    """


def normalize_query(raw: str) -> str:
    """
    Validate a query from the UI and turn it into something spotDL understands.

    ### Arguments
    - raw: A Spotify/YouTube Music link, a Spotify URI or a search term.

    ### Returns
    - The query to pass to spotDL.

    ### Errors
    - QueryError: if the query is empty, too long or not supported.
    """

    query = raw.strip()
    if not query:
        raise QueryError("Paste a link or type a song name")

    if len(query) > 1000:
        raise QueryError("That query is too long")

    uri = SPOTIFY_URI_REGEX.fullmatch(query)
    if uri:
        return f"https://open.spotify.com/{uri.group(1)}/{uri.group(2)}"

    looks_like_link = "://" in query or query.lower().startswith(
        ("open.spotify.com", "spotify.link", "music.youtube.com", "www.", "youtu")
    )
    if not looks_like_link:
        if query.lower() in RESERVED_QUERIES or query.lower().endswith(".spotdl"):
            raise QueryError("That query isn't supported here")

        return query

    links = []
    for part in query.split("|"):
        link = part.strip()
        if "://" not in link:
            link = f"https://{link}"

        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not (
            host in ALLOWED_HOSTS or host.endswith(".bandcamp.com")
        ):
            raise QueryError("Only Spotify and YouTube Music links are supported")

        links.append(link)

    if len(links) == 1:
        link = links[0]
        host = (urlparse(link).hostname or "").lower()
        if host not in ("open.spotify.com", "spotify.link", "music.youtube.com") and (
            "list=" not in link and "browse/VLPL" not in link
        ):
            raise QueryError(
                "Plain YouTube links aren't supported, use a YouTube Music link instead"
            )

    return "|".join(links)


def detect_kind(url: Optional[str]) -> str:
    """
    Guess what a link points to.

    ### Arguments
    - url: The link (or None).

    ### Returns
    - One of track, album, playlist, artist, user.
    """

    if not url:
        return "track"

    match = SPOTIFY_URL_REGEX.search(url)
    if match:
        return match.group(1)

    if "list=" in url or "browse/VLPL" in url:
        return "album" if "list=OLAK5uy_" in url else "playlist"

    return "track"


def cover_thumbnail(url: Optional[str]) -> Optional[str]:
    """
    Get a smaller variant of a Spotify cover image when possible.
    """

    if url and "i.scdn.co/image/" in url:
        return url.replace(COVER_640, COVER_300)

    return url


def clean_error(error: str, song_url: str) -> str:
    """
    Turn one of `Downloader.errors` into a short message for the UI.
    """

    message = error
    if message.startswith(f"{song_url} - "):
        message = message[len(song_url) + 3 :]
    else:
        message = re.sub(r"^\S+://\S+ - ", "", message, count=1)

    # Drop the exception class name, e.g. "LookupError: No results found ..."
    message = re.sub(r"^[A-Za-z_][\w.]*: ", "", message, count=1)
    # Don't leak server paths from ffmpeg errors
    message = re.sub(r",? ?you can find error here: .*$", "", message)

    if message.startswith("Error occurred while reinitializing song"):
        return "Couldn't load this track's details from Spotify"

    return message.strip() or "Download failed"


@dataclass
class Track:  # pylint: disable=too-many-instance-attributes
    """
    One song inside a job.
    """

    id: int
    url: str
    title: str
    artists: List[str]
    album: Optional[str]
    cover: Optional[str]
    duration: int
    song: Dict[str, Any] = field(repr=False)
    status: str = "queued"
    progress: int = 0
    message: str = ""
    error: Optional[str] = None
    file: Optional[str] = None
    size: Optional[int] = None

    @classmethod
    def from_song(cls, index: int, song: Song) -> "Track":
        """
        Create a track from a spotDL song.
        """

        return cls(
            id=index,
            url=song.url,
            title=song.name,
            artists=list(song.artists or [song.artist]),
            album=song.album_name,
            cover=song.cover_url,
            duration=int(song.duration or 0),
            song=song_state(song),
        )

    def reset(self) -> None:
        """
        Put the track back in the queue to download it again.
        """

        self.status = "queued"
        self.progress = 0
        self.message = ""
        self.error = None

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize for the UI.
        """

        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "cover": cover_thumbnail(self.cover),
            "duration": self.duration,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "file": self.file,
            "size": self.size,
        }

    def to_state(self) -> Dict[str, Any]:
        """
        Serialize for `job.json`.
        """

        state = self.to_dict()
        state["cover"] = self.cover
        state["song"] = self.song
        return state

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "Track":
        """
        Restore from `job.json`.
        """

        fields = cls.__dataclass_fields__  # pylint: disable=no-member
        return cls(**{key: value for key, value in state.items() if key in fields})


@dataclass
class Job:  # pylint: disable=too-many-instance-attributes
    """
    One download request.
    """

    id: str
    owner: str
    query: str
    format: str
    bitrate: str
    created_at: float = field(default_factory=time.time)
    status: str = "resolving"
    kind: str = "track"
    title: str = ""
    subtitle: str = ""
    covers: List[str] = field(default_factory=list)
    error: Optional[str] = None
    finished_at: Optional[float] = None
    tracks: List[Track] = field(default_factory=list)

    task: Optional["asyncio.Task[None]"] = field(default=None, repr=False)
    archive_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def is_active(self) -> bool:
        """
        Whether the job is still resolving, waiting or downloading.
        """

        return self.status in ACTIVE_STATUSES

    def track_for(self, url: str) -> Optional[Track]:
        """
        Find a track by its song url.
        """

        key = _url_key(url)
        for track in self.tracks:
            if _url_key(track.url) == key:
                return track

        return None

    def stats(self) -> Dict[str, int]:
        """
        Count tracks by status, plus overall progress in percent.
        """

        total = len(self.tracks)
        done = sum(1 for track in self.tracks if track.status == "done")
        failed = sum(1 for track in self.tracks if track.status == "error")
        progress = (
            round(sum(track.progress for track in self.tracks) / total) if total else 0
        )

        return {"total": total, "done": done, "failed": failed, "progress": progress}

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize for the UI.
        """

        return {
            "id": self.id,
            "query": self.query,
            "kind": self.kind,
            "title": self.title,
            "subtitle": self.subtitle,
            "covers": [cover_thumbnail(cover) for cover in self.covers],
            "format": self.format,
            "bitrate": self.bitrate,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "stats": self.stats(),
            "tracks": [track.to_dict() for track in self.tracks],
        }

    def to_state(self) -> Dict[str, Any]:
        """
        Serialize for `job.json`.
        """

        return {
            "version": STATE_VERSION,
            "id": self.id,
            "owner": self.owner,
            "query": self.query,
            "format": self.format,
            "bitrate": self.bitrate,
            "created_at": self.created_at,
            "status": self.status,
            "kind": self.kind,
            "title": self.title,
            "subtitle": self.subtitle,
            "covers": self.covers,
            "error": self.error,
            "finished_at": self.finished_at,
            "tracks": [track.to_state() for track in self.tracks],
        }

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "Job":
        """
        Restore from `job.json`.
        """

        return cls(
            id=state["id"],
            owner=state["owner"],
            query=state["query"],
            format=state["format"],
            bitrate=state["bitrate"],
            created_at=state["created_at"],
            status=state["status"],
            kind=state.get("kind", "track"),
            title=state.get("title", ""),
            subtitle=state.get("subtitle", ""),
            covers=state.get("covers", []),
            error=state.get("error"),
            finished_at=state.get("finished_at"),
            tracks=[Track.from_state(track) for track in state.get("tracks", [])],
        )


def _url_key(url: str) -> str:
    # Spotify urls can differ in scheme or query string, the id is what matters
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def song_state(song: Song) -> Dict[str, Any]:
    """
    A song's data as kept in a job's state, so it can be downloaded again later.
    """

    state = song.json
    # Lyrics are embedded in the file already and can be large
    state["lyrics"] = None
    return state
