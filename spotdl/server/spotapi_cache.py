"""
Process-wide cache for spotapi's web-player bootstrap.

SpotipyFree (the default Spotify backend) creates a new spotapi client for every
metadata call. Each new client fetches open.spotify.com, mints fresh tokens and
downloads the whole web-player JavaScript bundle just to read GraphQL query
hashes. That costs ~9 seconds per call, and a single track needs three calls.

`install()` shares that bootstrap state between clients for `TTL` seconds,
which brings a track lookup down from ~30 seconds to about one. The cache is
dropped whenever Spotify rejects a request, so stale state is refreshed on the
next call.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

__all__ = ["install", "clear", "TTL"]

logger = logging.getLogger(__name__)

TTL = 60 * 60

# Attributes that spotapi's `BaseClient.get_session` sets on the instance.
SESSION_FIELDS = (
    "js_pack",
    "client_version",
    "device_id",
    "server_cfg",
    "_raw_app_server_config",
    "access_token",
    "client_id",
    "access_token_expires_at_ms",
)

# Refresh the shared access token this long before Spotify expires it.
EXPIRY_SKEW = 120


class _SharedState:
    """
    Bootstrap state shared by every spotapi client in the process.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.session: Optional[Dict[str, Any]] = None
        self.session_expires = 0.0
        self.client_token: Optional[str] = None
        self.hashes: Optional[str] = None
        self.hashes_expires = 0.0

    def clear(self) -> None:
        """
        Forget everything, the next client bootstraps from scratch.
        """

        with self.lock:
            self.session = None
            self.client_token = None
            self.hashes = None


_state = _SharedState()
_installed = False  # pylint: disable=invalid-name


def clear() -> None:
    """
    Drop the cached bootstrap state.
    """

    _state.clear()


def _session_expiry(client: Any) -> float:
    expiry = time.time() + TTL
    token_expiry_ms = getattr(client, "access_token_expires_at_ms", 0) or 0
    if token_expiry_ms:
        expiry = min(expiry, token_expiry_ms / 1000 - EXPIRY_SKEW)

    return expiry


def install() -> bool:
    """
    Patch spotapi's `BaseClient` to share its bootstrap state.

    ### Returns
    - True if the cache is active, False if spotapi is missing or has changed
        in a way this module does not recognise.
    """

    global _installed  # pylint: disable=global-statement

    if _installed:
        return True

    try:
        from spotapi.client import BaseClient  # pylint: disable=import-outside-toplevel
    except ImportError:
        logger.debug("spotapi is not installed, skipping the bootstrap cache")
        return False

    required = (
        "get_session",
        "get_client_token",
        "get_sha256_hash",
        "_handle_auth_failure",
    )
    if not all(callable(getattr(BaseClient, name, None)) for name in required):
        logger.warning("Unrecognised spotapi version, skipping the bootstrap cache")
        return False

    original_session: Callable = BaseClient.get_session
    original_client_token: Callable = BaseClient.get_client_token
    original_hashes: Callable = BaseClient.get_sha256_hash
    original_auth_failure: Callable = (
        BaseClient._handle_auth_failure  # pylint: disable=protected-access
    )

    def get_session(self) -> None:
        with _state.lock:
            if _state.session is None or time.time() >= _state.session_expires:
                original_session(self)
                _state.session = {
                    name: getattr(self, name)
                    for name in SESSION_FIELDS
                    if hasattr(self, name)
                }
                _state.session_expires = _session_expiry(self)
                # A client token belongs to a session, mint a new one with it
                _state.client_token = None
                return

            session = _state.session

        for name, value in session.items():
            setattr(self, name, value)

    def get_client_token(self) -> None:
        with _state.lock:
            if _state.session is not None and time.time() >= _state.session_expires:
                # Refreshing the session drops the client token minted for it
                get_session(self)

            if _state.client_token is None:
                original_client_token(self)
                _state.client_token = self.client_token
                return

            self.client_token = _state.client_token

    def get_sha256_hash(self) -> None:
        with _state.lock:
            if _state.hashes is None or time.time() >= _state.hashes_expires:
                original_hashes(self)
                _state.hashes = self.raw_hashes
                _state.hashes_expires = time.time() + TTL
                return

            self.raw_hashes = _state.hashes

    def handle_auth_failure(self, response: Any) -> bool:
        # Spotify rejected something we may have served from the cache
        if getattr(response, "status_code", None) in (400, 401):
            _state.clear()

        return original_auth_failure(self, response)

    BaseClient.get_session = get_session
    BaseClient.get_client_token = get_client_token
    BaseClient.get_sha256_hash = get_sha256_hash
    BaseClient._handle_auth_failure = handle_auth_failure  # pylint: disable=W0212

    _installed = True
    logger.debug("spotapi bootstrap cache installed")

    return True
