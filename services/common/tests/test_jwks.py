from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.support.logs import LogCapture
from tests.support.tokens import AUDIENCE, ISSUER, RSA_KID, TokenFactory
from triaxis_common.auth import JwksCache, TokenValidator
from triaxis_common.errors import AuthError, ServiceUnavailableError
from triaxis_common.logging import AllowListLogger

JWKS_URL = "https://keycloak.triaxis.svc/realms/triaxis/protocol/openid-connect/certs"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class JwksServer:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.calls = 0
        self.fail: Exception | None = None
        self.status = 200

    def handler(self, _request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return httpx.Response(self.status, json=self.document)


def _cache(
    server: JwksServer, clock: FakeClock, logger: AllowListLogger | None = None
) -> JwksCache:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return JwksCache(JWKS_URL, client, clock=clock, logger=logger)


async def test_fetches_once_and_caches(tokens: TokenFactory, logs: LogCapture) -> None:
    server, clock = JwksServer(tokens.jwks()), FakeClock()
    cache = _cache(server, clock, AllowListLogger("gateway"))
    validator = TokenValidator(issuer=ISSUER, audience=AUDIENCE, keys=cache)
    for _ in range(5):
        await validator.validate(tokens.mint())
    assert server.calls == 1
    assert [r["event"] for r in logs.records] == ["jwks_refreshed"]


async def test_refreshes_after_ttl(tokens: TokenFactory) -> None:
    server, clock = JwksServer(tokens.jwks()), FakeClock()
    cache = _cache(server, clock)
    await cache.get_key(RSA_KID, "RS256")
    clock.now += 301
    await cache.get_key(RSA_KID, "RS256")
    assert server.calls == 2


async def test_unknown_kid_refresh_is_rate_limited(tokens: TokenFactory) -> None:
    server, clock = JwksServer(tokens.jwks()), FakeClock()
    cache = _cache(server, clock)
    await cache.get_key(RSA_KID, "RS256")
    for _ in range(10):
        with pytest.raises(AuthError):
            await cache.get_key("attacker-kid", "RS256")
    assert server.calls == 1  # within the 30 s window: no extra fetches
    clock.now += 31
    with pytest.raises(AuthError):
        await cache.get_key("attacker-kid", "RS256")
    assert server.calls == 2


async def test_key_rotation_picked_up(tokens: TokenFactory) -> None:
    server, clock = (
        JwksServer(
            {"keys": []},
        ),
        FakeClock(),
    )
    cache = _cache(server, clock)
    with pytest.raises(AuthError):
        await cache.get_key(RSA_KID, "RS256")
    server.document = tokens.jwks()
    clock.now += 31
    assert await cache.get_key(RSA_KID, "RS256") is not None


async def test_serves_stale_keys_when_keycloak_down(tokens: TokenFactory, logs: LogCapture) -> None:
    server, clock = JwksServer(tokens.jwks()), FakeClock()
    cache = _cache(server, clock, AllowListLogger("gateway"))
    await cache.get_key(RSA_KID, "RS256")
    server.fail = httpx.ConnectError("down")
    clock.now += 600
    assert await cache.get_key(RSA_KID, "RS256") is not None
    assert "jwks_refresh_failed" in [r["event"] for r in logs.records]
    assert logs.records[-1]["error_class"] == "ConnectError"
    clock.now += 3600
    with pytest.raises(ServiceUnavailableError):
        await cache.get_key(RSA_KID, "RS256")


@pytest.mark.parametrize("fail", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_fails_closed_without_any_keys(fail: Exception) -> None:
    server, clock = JwksServer({}), FakeClock()
    server.fail = fail
    cache = _cache(server, clock)
    with pytest.raises(ServiceUnavailableError):
        await cache.get_key(RSA_KID, "RS256")


async def test_http_error_status_fails_closed(tokens: TokenFactory) -> None:
    server, clock = JwksServer(tokens.jwks()), FakeClock()
    server.status = 500
    with pytest.raises(ServiceUnavailableError):
        await _cache(server, clock).get_key(RSA_KID, "RS256")


async def test_malformed_document_ignored() -> None:
    server, clock = JwksServer({"keys": ["nope", {"kid": 3}]}), FakeClock()
    cache = _cache(server, clock)
    with pytest.raises(AuthError):
        await cache.get_key(RSA_KID, "RS256")


@pytest.mark.parametrize(
    "url",
    [
        "http://keycloak.triaxis.svc/certs",
        "https://keycloak.example.com/certs",
        "https://8.8.8.8/certs",
        "https://user:pw@keycloak.triaxis.svc/certs",
    ],
)
def test_rejects_non_cluster_or_insecure_urls(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        JwksCache(url, httpx.AsyncClient())


def test_allows_http_in_dev() -> None:
    JwksCache("http://keycloak:8080/certs", httpx.AsyncClient(), allow_http=True)
