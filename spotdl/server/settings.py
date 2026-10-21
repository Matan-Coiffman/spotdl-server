"""
Settings for the spotDL web server.

Every setting is read from an environment variable prefixed with `SPOTDL_`,
so the server can be configured the same way on a laptop, in Docker
or on a hosting platform.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional

from spotdl.utils.config import DOWNLOADER_OPTIONS, SPOTIFY_OPTIONS, get_spotdl_path
from spotdl.utils.ffmpeg import FFMPEG_FORMATS

__all__ = ["ServerSettings", "SettingsError", "FORMATS", "BITRATES"]

FORMATS = list(FFMPEG_FORMATS.keys())
BITRATES = ["auto", "128k", "160k", "192k", "256k", "320k"]
LOG_LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


class SettingsError(Exception):
    """
    Raised when an environment variable holds an invalid value.
    """


def _default_data_dir() -> Path:
    return get_spotdl_path() / "server"


@dataclass
class ServerSettings:  # pylint: disable=too-many-instance-attributes
    """
    Settings for the web server. Use `ServerSettings.from_env()` to build them
    from the environment.
    """

    host: str = "127.0.0.1"
    port: int = 8800
    data_dir: Path = field(default_factory=_default_data_dir)
    password: Optional[str] = None
    max_concurrent_jobs: int = 2
    threads: int = 4
    max_tracks: int = 500
    max_queued_jobs: int = 50
    retention_hours: float = 24.0
    default_format: str = "mp3"
    default_bitrate: str = "auto"
    audio_providers: List[str] = field(
        default_factory=lambda: list(DOWNLOADER_OPTIONS["audio_providers"])
    )
    lyrics_providers: List[str] = field(
        default_factory=lambda: list(DOWNLOADER_OPTIONS["lyrics_providers"])
    )
    cookie_file: Optional[str] = None
    proxy: Optional[str] = None
    client_id: str = SPOTIFY_OPTIONS["client_id"]
    client_secret: str = SPOTIFY_OPTIONS["client_secret"]
    use_official_api: bool = False
    spotapi_cache: bool = True
    log_level: str = "INFO"

    @property
    def jobs_dir(self) -> Path:
        """
        Directory that holds one sub-directory per job.
        """

        return self.data_dir / "jobs"

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "ServerSettings":
        """
        Build settings from environment variables.

        ### Arguments
        - environ: Mapping to read from, defaults to `os.environ`.

        ### Returns
        - The server settings.

        ### Notes
        - `PORT` (without prefix) is honoured because most hosting platforms set it.
        """

        env = _Env(os.environ if environ is None else environ)
        defaults = cls()

        settings = cls(
            host=env.get_str("HOST", defaults.host),
            port=env.get_int("PORT", env.platform_port(defaults.port), minimum=1),
            data_dir=Path(env.get_str("DATA_DIR", str(defaults.data_dir))).expanduser(),
            password=env.get_str("PASSWORD", "") or None,
            max_concurrent_jobs=env.get_int(
                "MAX_CONCURRENT_JOBS", defaults.max_concurrent_jobs, minimum=1
            ),
            threads=env.get_int("THREADS", defaults.threads, minimum=1),
            max_tracks=env.get_int("MAX_TRACKS", defaults.max_tracks, minimum=1),
            max_queued_jobs=env.get_int(
                "MAX_QUEUED_JOBS", defaults.max_queued_jobs, minimum=1
            ),
            retention_hours=env.get_float(
                "RETENTION_HOURS", defaults.retention_hours, minimum=0
            ),
            default_format=env.get_choice("FORMAT", defaults.default_format, FORMATS),
            default_bitrate=env.get_choice(
                "BITRATE", defaults.default_bitrate, BITRATES
            ),
            audio_providers=env.get_list("AUDIO_PROVIDERS", defaults.audio_providers),
            lyrics_providers=env.get_list(
                "LYRICS_PROVIDERS", defaults.lyrics_providers
            ),
            cookie_file=env.get_str("COOKIE_FILE", "") or None,
            proxy=env.get_str("PROXY", "") or None,
            client_id=env.get_str("CLIENT_ID", defaults.client_id),
            client_secret=env.get_str("CLIENT_SECRET", defaults.client_secret),
            use_official_api=env.get_bool(
                "USE_OFFICIAL_API", defaults.use_official_api
            ),
            spotapi_cache=env.get_bool("SPOTAPI_CACHE", defaults.spotapi_cache),
            log_level=env.get_choice(
                "LOG_LEVEL", defaults.log_level, LOG_LEVELS
            ).upper(),
        )

        if not settings.audio_providers:
            raise SettingsError("SPOTDL_AUDIO_PROVIDERS needs at least one provider")

        return settings


class _Env:
    """
    Typed accessors for `SPOTDL_*` environment variables.
    """

    def __init__(self, environ: Mapping[str, str]):
        self.environ = environ

    def raw(self, name: str) -> Optional[str]:
        """
        Read an unprefixed variable.
        """

        value = self.environ.get(name)
        return value.strip() if value is not None else None

    def platform_port(self, default: int) -> int:
        """
        Read the unprefixed `PORT` variable that hosting platforms set.
        """

        value = self.raw("PORT")
        if not value:
            return default

        try:
            return int(value)
        except ValueError as exc:
            raise SettingsError("PORT must be an integer") from exc

    def get(self, name: str) -> Optional[str]:
        """
        Read a prefixed variable, treating empty values as unset.
        """

        value = self.raw(f"SPOTDL_{name}")
        return value if value else None

    def get_str(self, name: str, default: str) -> str:
        """
        Read a string.
        """

        value = self.get(name)
        return default if value is None else value

    def get_int(self, name: str, default: int, minimum: int = 0) -> int:
        """
        Read an integer that must be at least `minimum`.
        """

        value = self.get(name)
        if value is None:
            return default

        try:
            number = int(value)
        except ValueError as exc:
            raise SettingsError(f"SPOTDL_{name} must be an integer") from exc

        if number < minimum:
            raise SettingsError(f"SPOTDL_{name} must be at least {minimum}")

        return number

    def get_float(self, name: str, default: float, minimum: float = 0) -> float:
        """
        Read a number that must be at least `minimum`.
        """

        value = self.get(name)
        if value is None:
            return default

        try:
            number = float(value)
        except ValueError as exc:
            raise SettingsError(f"SPOTDL_{name} must be a number") from exc

        if number < minimum:
            raise SettingsError(f"SPOTDL_{name} must be at least {minimum}")

        return number

    def get_bool(self, name: str, default: bool) -> bool:
        """
        Read a boolean (1/0, true/false, yes/no, on/off).
        """

        value = self.get(name)
        if value is None:
            return default

        lowered = value.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False

        raise SettingsError(f"SPOTDL_{name} must be true or false")

    def get_choice(self, name: str, default: str, choices: List[str]) -> str:
        """
        Read a value that must be one of `choices` (case-insensitive).
        """

        value = self.get(name)
        if value is None:
            return default

        lowered = {choice.lower(): choice for choice in choices}
        if value.lower() not in lowered:
            raise SettingsError(f"SPOTDL_{name} must be one of: {', '.join(choices)}")

        return lowered[value.lower()]

    def get_list(self, name: str, default: List[str]) -> List[str]:
        """
        Read a comma-separated list.
        """

        value = self.get(name)
        if value is None:
            return list(default)

        return [item.strip() for item in value.split(",") if item.strip()]
