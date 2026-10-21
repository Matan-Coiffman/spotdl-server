# Web server

`spotdl-server` is a self-hosted web app for spotDL: a small web UI plus the backend that runs the
downloads. Paste a Spotify (or YouTube Music) link for a track, album, playlist or artist, or
search for a song, and download the tagged audio files as they finish, one by one or as a ZIP.

- Progress shows live, track by track.
- One password is shared by everyone who uses the server; each browser sees its own downloads.
- Failed tracks are retried once automatically, and can be retried again from the UI.
- Finished downloads are deleted after 24 hours (configurable).
- Downloads survive a restart; ones that were interrupted can be retried.

It is separate from `spotdl web`, which runs a local UI for a single user.

## Run it with Docker

You only need [Docker](https://docs.docker.com/get-docker/). Images are published for Intel/AMD
and ARM machines (including Apple Silicon and Raspberry Pi 4/5) and rebuilt every week with the
newest yt-dlp.

### Just for you

```bash
docker run -d --name spotdl --restart unless-stopped \
  -p 127.0.0.1:8800:8800 -v spotdl-data:/data \
  ghcr.io/matan-coiffman/spotdl-server:latest
```

Open <http://localhost:8800>. The port is only reachable from this computer, so no password is
needed. Downloads are kept in the `spotdl-data` Docker volume.

### Shared with others

To let other devices or people use it, run it with a password. Download the compose file and the
example settings into an empty folder:

```bash
curl -fsSLO https://raw.githubusercontent.com/Matan-Coiffman/spotdl-server/main/docker-compose.server.yml
curl -fsSL -o .env https://raw.githubusercontent.com/Matan-Coiffman/spotdl-server/main/.env.example
```

Set `SPOTDL_PASSWORD` in `.env`, then start it:

```bash
docker compose -f docker-compose.server.yml up -d
```

Open `http://<your server>:8800` and sign in with the password.

### HTTPS on a VPS

The password is sent on every visit, so put a server on the internet behind HTTPS. The compose
file includes an optional [Caddy](https://caddyserver.com) proxy that gets a certificate from
Let's Encrypt for you:

1. Point a domain (for example `music.example.com`) at the server and open ports 80 and 443.
2. In `.env`, set `SPOTDL_DOMAIN=music.example.com` and `SPOTDL_BIND=127.0.0.1`, so port 8800 is
   only reachable through Caddy.
3. Start both services:

    ```bash
    docker compose -f docker-compose.server.yml --profile https up -d
    ```

Docker publishes ports past firewalls such as `ufw`, so `SPOTDL_BIND=127.0.0.1` is what actually
keeps port 8800 private.

### Updating

YouTube changes often, and old yt-dlp releases start failing with `HTTP Error 403: Forbidden`.
The published image is rebuilt weekly with the newest yt-dlp, so updating fixes that:

```bash
docker compose -f docker-compose.server.yml pull
docker compose -f docker-compose.server.yml up -d
```

With `docker run`, run `docker pull ghcr.io/matan-coiffman/spotdl-server:latest`, then remove
the container and start it again with the same command; downloads stay in the volume.

### Building the image yourself

From a clone of this repository, build under the same name and the commands above use your build:

```bash
docker build -f Dockerfile.server -t ghcr.io/matan-coiffman/spotdl-server:latest .
```

Add `--build-arg YT_DLP_VERSION=<release>` to use a newer
[yt-dlp release](https://github.com/yt-dlp/yt-dlp/releases) than the locked one.

## Run it without Docker

FFmpeg must be installed, and [Deno](installation.md#installing-deno) is recommended. From a clone
of this repository:

```bash
uv sync
SPOTDL_PASSWORD=change-me uv run spotdl-server
```

It listens on `127.0.0.1:8800` by default. Set `SPOTDL_HOST=0.0.0.0` to reach it from other
devices.

## Configuration

Everything is configured with environment variables.

| Variable | Default | Description |
| --- | --- | --- |
| `SPOTDL_PASSWORD` | none | Password for the UI and API. Without it, anyone who can reach the server can use it. |
| `SPOTDL_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Address to listen on. |
| `SPOTDL_PORT` | `PORT`, else `8800` | Port to listen on. |
| `SPOTDL_DATA_DIR` | `server` in spotDL's folder (`/data` in Docker) | Where downloads and job state are kept, e.g. `~/.spotdl/server` or `~/.config/spotdl/server` on Linux. |
| `SPOTDL_FORMAT` | `mp3` | Default format: `mp3`, `flac`, `ogg`, `opus`, `m4a` or `wav`. |
| `SPOTDL_BITRATE` | `auto` | Default quality: `auto`, `128k`, `160k`, `192k`, `256k` or `320k`. |
| `SPOTDL_RETENTION_HOURS` | `24` | Delete finished downloads after this many hours. `0` keeps them. |
| `SPOTDL_MAX_CONCURRENT_JOBS` | `2` | Downloads (albums, playlists, ...) processed at the same time. |
| `SPOTDL_THREADS` | `4` | Tracks downloaded at the same time within one download. |
| `SPOTDL_MAX_TRACKS` | `500` | Largest album, playlist or artist the server accepts. |
| `SPOTDL_MAX_QUEUED_JOBS` | `50` | Unfinished downloads allowed before new ones are refused. |
| `SPOTDL_AUDIO_PROVIDERS` | `youtube-music` | Comma-separated audio providers, as for `--audio`. |
| `SPOTDL_LYRICS_PROVIDERS` | `genius,azlyrics,musixmatch` | Comma-separated lyrics providers, as for `--lyrics`. |
| `SPOTDL_COOKIE_FILE` | none | Path to a YouTube `cookies.txt`, as for `--cookie-file`. |
| `SPOTDL_PROXY` | none | HTTP(S) proxy for downloads, as for `--proxy`. |
| `SPOTDL_CLIENT_ID`, `SPOTDL_CLIENT_SECRET` | spotDL's | Spotify API credentials. |
| `SPOTDL_USE_OFFICIAL_API` | `false` | Use the official Spotify Web API (needs your own credentials). |
| `SPOTDL_SPOTAPI_CACHE` | `true` | Share Spotify web-player sessions between lookups, see below. |
| `SPOTDL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `FORWARDED_ALLOW_IPS` | `*` | Proxies trusted for `X-Forwarded-*` headers. |

## When YouTube blocks the server

YouTube often blocks or rate-limits datacenter IP addresses, so a cheap VPS can fail where a home
server works. If most downloads fail:

- Update to the latest image, which carries the newest yt-dlp (see [Updating](#updating)).
- Export cookies from a browser logged in to YouTube to `cookies.txt`, mount it into the container
  (see the commented line in `docker-compose.server.yml`) and set `SPOTDL_COOKIE_FILE=/cookies.txt`.
- Route downloads through a proxy with `SPOTDL_PROXY`.
- Run the server at home instead.

## How it works

Each link or search becomes a job. The server looks it up on Spotify, then downloads every track
with spotDL's downloader into `<data dir>/jobs/<job id>/`, embedding metadata, album art and lyrics
just like the command line. If an identical file was already downloaded by another job, it is
reused instead of downloaded again. Job state is saved in `job.json` next to the files, so jobs
survive restarts.

By default spotDL reads Spotify through its web player instead of the official API. Every lookup
used to start a fresh web-player session, which costs about ten seconds and three of them are
needed per track. The server shares that session between lookups (`SPOTDL_SPOTAPI_CACHE`), which
brings a track lookup down to about a second.

## API

The UI uses a small JSON API, which also works from scripts. Send the password as a bearer token:

```bash
curl -H "Authorization: Bearer $SPOTDL_PASSWORD" \
     -H "Content-Type: application/json" \
     -d '{"query": "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj", "format": "mp3"}' \
     http://localhost:8800/api/jobs
```

| Endpoint | Description |
| --- | --- |
| `GET /api/health` | Health check, no password needed. |
| `GET /api/search?q=...` | Search Spotify for tracks. |
| `POST /api/jobs` | Start a download: `{"query", "format", "bitrate"}`. |
| `GET /api/jobs` | Your downloads (per browser cookie). |
| `GET /api/jobs/{id}` | One download, with its tracks. |
| `POST /api/jobs/{id}/retry` | Retry failed tracks. |
| `DELETE /api/jobs/{id}` | Cancel a download and delete its files. |
| `GET /api/jobs/{id}/tracks/{n}/file` | Download one track. |
| `GET /api/jobs/{id}/archive` | Download every finished track as a ZIP. |
| `GET /api/events` | Live updates as Server-Sent Events. |
