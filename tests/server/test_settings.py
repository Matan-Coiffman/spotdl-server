from pathlib import Path

import pytest

from spotdl.server.settings import ServerSettings, SettingsError


def test_defaults():
    settings = ServerSettings.from_env({})

    assert settings.host == "127.0.0.1"
    assert settings.port == 8800
    assert settings.password is None
    assert settings.default_format == "mp3"
    assert settings.audio_providers == ["youtube-music"]


def test_reads_environment(tmp_path):
    settings = ServerSettings.from_env(
        {
            "SPOTDL_HOST": "0.0.0.0",
            "SPOTDL_DATA_DIR": str(tmp_path),
            "SPOTDL_PASSWORD": "secret",
            "SPOTDL_THREADS": "8",
            "SPOTDL_RETENTION_HOURS": "0.5",
            "SPOTDL_FORMAT": "FLAC",
            "SPOTDL_AUDIO_PROVIDERS": "youtube-music, youtube",
            "SPOTDL_USE_OFFICIAL_API": "yes",
            "SPOTDL_LOG_LEVEL": "debug",
        }
    )

    assert settings.host == "0.0.0.0"
    assert settings.data_dir == Path(tmp_path)
    assert settings.jobs_dir == Path(tmp_path) / "jobs"
    assert settings.password == "secret"
    assert settings.threads == 8
    assert settings.retention_hours == 0.5
    assert settings.default_format == "flac"
    assert settings.audio_providers == ["youtube-music", "youtube"]
    assert settings.use_official_api is True
    assert settings.log_level == "DEBUG"


def test_platform_port_and_override():
    assert ServerSettings.from_env({"PORT": "10000"}).port == 10000
    assert (
        ServerSettings.from_env({"PORT": "10000", "SPOTDL_PORT": "9000"}).port == 9000
    )


def test_empty_values_are_unset():
    settings = ServerSettings.from_env({"SPOTDL_PASSWORD": "", "SPOTDL_THREADS": " "})

    assert settings.password is None
    assert settings.threads == 4


@pytest.mark.parametrize(
    "environ",
    [
        {"SPOTDL_THREADS": "many"},
        {"SPOTDL_THREADS": "0"},
        {"SPOTDL_FORMAT": "mp4"},
        {"SPOTDL_USE_OFFICIAL_API": "maybe"},
        {"SPOTDL_AUDIO_PROVIDERS": ","},
        {"PORT": "http"},
    ],
)
def test_invalid_values(environ):
    with pytest.raises(SettingsError):
        ServerSettings.from_env(environ)
