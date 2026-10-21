"""
Entry point for the spotDL web server: `spotdl-server` or `python -m spotdl.server`.
"""

import logging
import os
import sys

import uvicorn

from spotdl.server.app import create_app
from spotdl.server.settings import ServerSettings, SettingsError

__all__ = ["main"]


def main() -> None:
    """
    Read settings from the environment and serve the app until stopped.
    """

    try:
        settings = ServerSettings.from_env()
    except SettingsError as exc:
        sys.exit(f"spotdl-server: {exc}")

    # Plain lines rather than spotDL's rich console, which wraps at 80 columns
    # when there is no terminal and garbles hosting platforms' log viewers
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:  %(message)s")
    logging.getLogger("spotdl").setLevel(settings.log_level)

    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.password:
        print(
            "spotdl-server: no SPOTDL_PASSWORD set, "
            "anyone who can reach this port can use the server",
            file=sys.stderr,
        )

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        # Hosting platforms put a proxy in front of the app, trust it for https
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
        # Event streams never end by themselves, don't wait on them forever
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
