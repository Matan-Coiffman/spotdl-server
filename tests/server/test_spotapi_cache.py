import time
from collections import Counter
from types import SimpleNamespace

import pytest
import spotapi.client

from spotdl.server import spotapi_cache


class FakeBaseClient:
    """
    Mimics the parts of spotapi's BaseClient that bootstrap a session.
    """

    calls: Counter = Counter()

    js_pack = None
    client_version = None
    device_id = None
    access_token = None
    client_id = None
    access_token_expires_at_ms = 0
    client_token = None
    raw_hashes = None

    def get_session(self):
        FakeBaseClient.calls["session"] += 1
        self.js_pack = "pack.js"
        self.client_version = "1.2.3"
        self.device_id = "device"
        self.access_token = f"token-{FakeBaseClient.calls['session']}"
        self.client_id = "client"
        self.access_token_expires_at_ms = (time.time() + 3600) * 1000

    def get_client_token(self):
        FakeBaseClient.calls["client_token"] += 1
        if not (self.client_id and self.device_id and self.client_version):
            self.get_session()
        self.client_token = f"client-token-{FakeBaseClient.calls['client_token']}"

    def get_sha256_hash(self):
        FakeBaseClient.calls["hashes"] += 1
        if not self.js_pack:
            self.get_session()
        self.raw_hashes = "hashes"

    def _handle_auth_failure(self, response):
        FakeBaseClient.calls["auth_failure"] += 1
        return True

    def bootstrap(self):
        # The order spotapi's auth rule and part_hash call these in
        self.get_client_token()
        self.get_session()
        self.get_sha256_hash()


@pytest.fixture()
def fake_client(monkeypatch):
    FakeBaseClient.calls = Counter()
    original = {
        name: getattr(FakeBaseClient, name)
        for name in (
            "get_session",
            "get_client_token",
            "get_sha256_hash",
            "_handle_auth_failure",
        )
    }
    monkeypatch.setattr(spotapi.client, "BaseClient", FakeBaseClient)
    monkeypatch.setattr(spotapi_cache, "_installed", False)
    spotapi_cache.clear()

    yield FakeBaseClient

    for name, method in original.items():
        setattr(FakeBaseClient, name, method)
    spotapi_cache.clear()


def test_clients_share_bootstrap(fake_client):
    assert spotapi_cache.install() is True
    assert spotapi_cache.install() is True

    first = fake_client()
    first.bootstrap()
    second = fake_client()
    second.bootstrap()

    assert fake_client.calls == {"session": 1, "client_token": 1, "hashes": 1}
    for name in ("access_token", "client_token", "raw_hashes", "js_pack", "device_id"):
        assert getattr(second, name) == getattr(first, name)


def test_auth_failure_drops_cache(fake_client):
    spotapi_cache.install()
    fake_client().bootstrap()

    assert fake_client()._handle_auth_failure(SimpleNamespace(status_code=401)) is True

    client = fake_client()
    client.bootstrap()
    assert fake_client.calls["session"] == 2
    assert fake_client.calls["hashes"] == 2
    assert client.access_token == "token-2"


def test_other_failures_keep_cache(fake_client):
    spotapi_cache.install()
    fake_client().bootstrap()
    fake_client()._handle_auth_failure(SimpleNamespace(status_code=500))
    fake_client().bootstrap()

    assert fake_client.calls["session"] == 1


def test_expired_session_is_refreshed(fake_client, monkeypatch):
    spotapi_cache.install()
    fake_client().bootstrap()

    monkeypatch.setattr(spotapi_cache._state, "session_expires", time.time() - 1)
    client = fake_client()
    client.bootstrap()

    assert fake_client.calls["session"] == 2
    # A new session gets a new client token, but the hashes are still fresh
    assert fake_client.calls["client_token"] == 2
    assert fake_client.calls["hashes"] == 1


def test_unknown_spotapi_is_left_alone(monkeypatch):
    monkeypatch.setattr(spotapi.client, "BaseClient", object)
    monkeypatch.setattr(spotapi_cache, "_installed", False)

    assert spotapi_cache.install() is False
