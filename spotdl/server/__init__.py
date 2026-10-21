"""
A self-hostable web server for spotDL, with a minimal web UI.

Run it with `spotdl-server` or `python -m spotdl.server`. It is configured with
`SPOTDL_*` environment variables, see `spotdl.server.settings`.
"""

from spotdl.server.app import create_app
from spotdl.server.settings import ServerSettings

__all__ = ["create_app", "ServerSettings"]
