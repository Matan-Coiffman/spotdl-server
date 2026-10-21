import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from spotdl.server.app import CLIENT_COOKIE, SearchCache, create_app, search_result
from spotdl.types.song import Song
from tests.server.conftest import make_song

ALBUM_URL = "https://open.spotify.com/album/album1"

RAW_SEARCH = {
    "tracks": {
        "items": [
            {
                "id": "abc",
                "name": "One More Time",
                "duration_ms": 320357,
                "explicit": False,
                "artists": [{"name": "Daft Punk"}],
                "album": {
                    "name": "Discovery",
                    "images": [
                        {"url": "https://i.scdn.co/640", "width": 640, "height": 640},
                        {"url": "https://i.scdn.co/64", "width": 64, "height": 64},
                        {"url": "https://i.scdn.co/300", "width": 300, "height": 300},
                    ],
                },
                "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
            }
        ]
    }
}


@pytest.fixture()
def search_calls(monkeypatch):
    calls = []

    def search(query):
        calls.append(query)
        return RAW_SEARCH

    monkeypatch.setattr(Song, "search", staticmethod(search))
    return calls


@pytest.fixture()
def client(settings, fake_spotdl, search_calls):
    with TestClient(create_app(settings)) as test_client:
        # Loading the page hands out the browser's client cookie
        test_client.get("/")
        yield test_client


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("resolving", "queued", "running"):
            return job
        time.sleep(0.02)

    raise AssertionError("job did not finish")


def test_index_and_session(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert client.cookies.get(CLIENT_COOKIE)

    assert client.get("/api/health").json()["status"] == "ok"

    session = client.get("/api/session").json()
    assert session["auth_required"] is False
    assert session["authenticated"] is True
    assert "flac" in session["formats"]
    assert session["defaults"] == {"format": "mp3", "bitrate": "auto"}


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_search(client, search_calls):
    first = client.get("/api/search", params={"q": " Daft Punk "}).json()
    second = client.get("/api/search", params={"q": "daft punk"}).json()

    assert first == second
    assert search_calls[-1] == "Daft Punk"
    assert search_calls.count("Daft Punk") == 1
    assert first["results"] == [
        {
            "url": "https://open.spotify.com/track/abc",
            "title": "One More Time",
            "artists": ["Daft Punk"],
            "album": "Discovery",
            "cover": "https://i.scdn.co/300",
            "duration": 320,
            "explicit": False,
        }
    ]
    assert client.get("/api/search", params={"q": "  "}).json() == {"results": []}


def test_search_result_handles_missing_fields():
    result = search_result({"id": "x"})

    assert result["url"] == "https://open.spotify.com/track/x"
    assert result["title"] == "Unknown"
    assert result["cover"] is None
    assert result["artists"] == []


def test_search_cache_evicts_and_expires():
    cache = SearchCache(size=2, ttl=60)
    cache.set("a", [])
    cache.set("b", [])
    cache.get("a")
    cache.set("c", [])

    assert cache.get("b") is None
    assert cache.get("a") == []

    cache.ttl = -1
    assert cache.get("a") is None


def test_create_job_validation(client):
    def create(**body):
        return client.post("/api/jobs", json={"query": "song", **body})

    assert create(format="mp4").status_code == 422
    assert create(bitrate="999k").status_code == 422

    response = client.post("/api/jobs", json={"query": "saved"})
    assert response.status_code == 422
    assert response.json()["detail"] == "That query isn't supported here"


def test_job_lifecycle(client, fake_spotdl):
    fake_spotdl[ALBUM_URL] = [
        make_song(index, list_url=ALBUM_URL) for index in range(2)
    ]

    response = client.post(
        "/api/jobs", json={"query": ALBUM_URL, "format": "flac", "bitrate": "320k"}
    )
    assert response.status_code == 201
    job = wait_for_job(client, response.json()["id"])

    assert job["status"] == "done"
    assert job["format"] == "flac"
    assert job["covers"][0] == "https://i.scdn.co/image/ab67616d00001e02cover0"
    assert [track["file"] for track in job["tracks"]] == [
        "Artist - Song 0.flac",
        "Artist - Song 1.flac",
    ]
    assert [listed["id"] for listed in client.get("/api/jobs").json()["jobs"]] == [
        job["id"]
    ]

    track = client.get(f"/api/jobs/{job['id']}/tracks/1/file")
    assert track.status_code == 200
    assert track.content == b"audio:Song 1"
    assert track.headers["content-type"] == "audio/flac"
    assert "Artist%20-%20Song%201.flac" in track.headers["content-disposition"]
    assert client.get(f"/api/jobs/{job['id']}/tracks/5/file").status_code == 404

    archive = client.get(f"/api/jobs/{job['id']}/archive")
    assert archive.status_code == 200
    assert "Test%20Album.zip" in archive.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zip_file:
        assert len(zip_file.namelist()) == 2

    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 409

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_jobs_are_listed_per_browser(client):
    job = client.post("/api/jobs", json={"query": "song"}).json()
    wait_for_job(client, job["id"])

    # Not a context manager: that would run the app's lifespan a second time
    other_browser = TestClient(client.app)
    other_browser.get("/")
    assert other_browser.cookies.get(CLIENT_COOKIE) != client.cookies.get(CLIENT_COOKIE)
    assert other_browser.get("/api/jobs").json() == {"jobs": []}
    # A job id still works as a link to share
    assert other_browser.get(f"/api/jobs/{job['id']}").status_code == 200


def test_password(settings, fake_spotdl, search_calls):
    settings.password = "hunter2"

    with TestClient(create_app(settings)) as client:
        session = client.get("/api/session").json()
        assert session["auth_required"] is True
        assert session["authenticated"] is False

        assert client.get("/api/jobs").status_code == 401
        assert client.get("/api/events").status_code == 401
        assert client.get("/api/health").status_code == 200

        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
        assert (
            client.post("/api/login", json={"password": "hunter2"}).status_code == 200
        )
        assert client.get("/api/session").json()["authenticated"] is True
        assert client.get("/api/jobs").status_code == 200

        client.post("/api/logout")
        client.cookies.clear()
        assert client.get("/api/jobs").status_code == 401

        headers = {"Authorization": "Bearer hunter2"}
        assert client.get("/api/jobs", headers=headers).status_code == 200
        headers = {"Authorization": "Bearer wrong"}
        assert client.get("/api/jobs", headers=headers).status_code == 401
