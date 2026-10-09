from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from triaxis_common.errors import ServiceUnavailableError
from triaxis_common.secrets import (
    FileSecretProvider,
    OpenBaoSecretProvider,
    SecretError,
    SecretValue,
)

BAO = "https://openbao.control.svc:8200"
SECRET = "s3cr3t-value-do-not-print"  # test value, not a real secret


def test_secret_value_never_prints() -> None:
    value = SecretValue(SECRET)
    assert SECRET not in repr(value)
    assert SECRET not in str(value)
    assert SECRET not in f"{value}"
    assert SECRET not in f"{value!r}"
    assert value.reveal() == SECRET
    assert value != SecretValue(SECRET)  # no equality oracle
    with pytest.raises(TypeError):
        hash(value)


async def test_file_provider(tmp_path: Path) -> None:
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "client-secret").write_text(SECRET + "\n", "utf-8")
    provider = FileSecretProvider(tmp_path)
    assert (await provider.get("gateway", "client-secret")).reveal() == SECRET
    with pytest.raises(SecretError, match="not available"):
        await provider.get("gateway", "missing")


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("../etc", "passwd"),
        ("gateway", "../x"),
        ("/abs", "k"),
        ("a//b", "k"),
        ("gateway", ""),
        ("gate way", "k"),
    ],
)
async def test_file_provider_rejects_traversal(tmp_path: Path, path: str, key: str) -> None:
    with pytest.raises(SecretError, match="invalid secret reference"):
        await FileSecretProvider(tmp_path).get(path, key)


async def test_file_provider_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("x", "utf-8")
    base = tmp_path / "base"
    (base / "svc").mkdir(parents=True)
    (base / "svc" / "key").symlink_to(outside)
    with pytest.raises(SecretError):
        await FileSecretProvider(base).get("svc", "key")


class Bao:
    def __init__(self) -> None:
        self.logins = 0
        self.reads = 0
        self.read_status = 200
        self.login_status = 200
        self.lease = 3600
        self.payload: object = {"data": {"data": {"client-secret": SECRET}}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/kubernetes/login":
            self.logins += 1
            body = json.loads(request.content)
            assert body == {"role": "stt", "jwt": "sa-jwt"}
            return httpx.Response(
                self.login_status,
                json={"auth": {"client_token": "hvs.test-token", "lease_duration": self.lease}},
            )
        assert request.url.path == "/v1/secret/data/stt/oidc"
        assert request.headers["X-Vault-Token"] == "hvs.test-token"
        self.reads += 1
        return httpx.Response(self.read_status, json=self.payload)


def _provider(bao: Bao, tmp_path: Path, now: list[float]) -> OpenBaoSecretProvider:
    jwt_path = tmp_path / "token"
    jwt_path.write_text("sa-jwt\n", "utf-8")
    return OpenBaoSecretProvider(
        BAO,
        "stt",
        httpx.AsyncClient(transport=httpx.MockTransport(bao.handler)),
        jwt_path=jwt_path,
        clock=lambda: now[0],
    )


async def test_openbao_login_cached_until_lease_margin(tmp_path: Path) -> None:
    bao, now = Bao(), [0.0]
    provider = _provider(bao, tmp_path, now)
    assert (await provider.get("stt/oidc", "client-secret")).reveal() == SECRET
    await provider.get("stt/oidc", "client-secret")
    assert bao.logins == 1
    now[0] = 3600 - 59
    await provider.get("stt/oidc", "client-secret")
    assert bao.logins == 2


async def test_openbao_forbidden_forces_relogin(tmp_path: Path) -> None:
    bao, now = Bao(), [0.0]
    provider = _provider(bao, tmp_path, now)
    bao.read_status = 403
    with pytest.raises(ServiceUnavailableError):
        await provider.get("stt/oidc", "client-secret")
    bao.read_status = 200
    await provider.get("stt/oidc", "client-secret")
    assert bao.logins == 2


@pytest.mark.parametrize(
    ("status", "payload"),
    [(404, {}), (500, {}), (200, {"data": {}}), (200, {"data": {"data": {"client-secret": 5}}})],
)
async def test_openbao_read_failures(tmp_path: Path, status: int, payload: object) -> None:
    bao, now = Bao(), [0.0]
    bao.read_status, bao.payload = status, payload
    with pytest.raises(SecretError):
        await _provider(bao, tmp_path, now).get("stt/oidc", "client-secret")


async def test_openbao_login_failures(tmp_path: Path) -> None:
    bao, now = Bao(), [0.0]
    bao.login_status = 403
    with pytest.raises(ServiceUnavailableError):
        await _provider(bao, tmp_path, now).get("stt/oidc", "client-secret")


async def test_openbao_missing_service_account_token(tmp_path: Path) -> None:
    provider = OpenBaoSecretProvider(
        BAO,
        "stt",
        httpx.AsyncClient(transport=httpx.MockTransport(Bao().handler)),
        jwt_path=tmp_path / "absent",
    )
    with pytest.raises(ServiceUnavailableError):
        await provider.get("stt/oidc", "client-secret")


async def test_openbao_empty_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"auth": {"client_token": ""}})

    jwt_path = tmp_path / "token"
    jwt_path.write_text("sa-jwt", "utf-8")
    provider = OpenBaoSecretProvider(
        BAO, "stt", httpx.AsyncClient(transport=httpx.MockTransport(handler)), jwt_path=jwt_path
    )
    with pytest.raises(ServiceUnavailableError):
        await provider.get("stt/oidc", "client-secret")


async def test_openbao_network_error_on_read(tmp_path: Path) -> None:
    bao, now = Bao(), [0.0]
    provider = _provider(bao, tmp_path, now)
    await provider.get("stt/oidc", "client-secret")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    with pytest.raises(ServiceUnavailableError):
        await provider.get("stt/oidc", "client-secret")


def test_openbao_requires_in_cluster_https() -> None:
    with pytest.raises(ValueError, match="URL"):
        OpenBaoSecretProvider("http://openbao.control.svc", "stt", httpx.AsyncClient())
    with pytest.raises(ValueError, match="URL"):
        OpenBaoSecretProvider("https://vault.example.com", "stt", httpx.AsyncClient())
    with pytest.raises(ValueError, match="mount"):
        OpenBaoSecretProvider(BAO, "stt", httpx.AsyncClient(), kv_mount="../sys")
