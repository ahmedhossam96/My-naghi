"""Secrets from OpenBao at runtime (ADR 0017).

Values are wrapped in :class:`SecretValue` so they cannot be printed or logged
by accident. Nothing in this module logs a path's value or a token.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

import httpx

from triaxis_common.config import require_in_cluster_url
from triaxis_common.errors import ServiceUnavailableError

_PATH_RE: Final = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SERVICE_ACCOUNT_TOKEN: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


class SecretValue:
    """A secret string that never shows itself in ``repr``/``str``/f-strings."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(***)"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


class SecretError(Exception):
    """Secret lookup failed. Message never contains the secret or the path."""


def _check_ref(path: str, key: str) -> None:
    if not _PATH_RE.fullmatch(path) or ".." in path or not _KEY_RE.fullmatch(key):
        raise SecretError("invalid secret reference")


class SecretProvider(Protocol):
    async def get(self, path: str, key: str) -> SecretValue: ...


class FileSecretProvider:
    """Reads ``<base>/<path>/<key>``. For dev/tests or an OpenBao Agent on tmpfs."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir.resolve()

    async def get(self, path: str, key: str) -> SecretValue:
        _check_ref(path, key)
        target = (self._base / path / key).resolve()
        if not target.is_relative_to(self._base):
            raise SecretError("invalid secret reference")
        try:
            value = await asyncio.to_thread(target.read_text, "utf-8")
        except OSError:
            raise SecretError("secret not available") from None
        return SecretValue(value.rstrip("\n"))


class OpenBaoSecretProvider:
    """KV v2 reads with Kubernetes auth against an in-cluster OpenBao."""

    def __init__(
        self,
        address: str,
        role: str,
        client: httpx.AsyncClient,
        *,
        kv_mount: str = "secret",
        auth_mount: str = "kubernetes",
        jwt_path: Path = SERVICE_ACCOUNT_TOKEN,
        renew_margin_s: float = 60.0,
        timeout_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._address = require_in_cluster_url(address, allow_http=False).rstrip("/")
        if not _PATH_RE.fullmatch(kv_mount) or not _PATH_RE.fullmatch(auth_mount):
            raise ValueError("invalid mount name")
        self._role = role
        self._client = client
        self._kv_mount = kv_mount
        self._auth_mount = auth_mount
        self._jwt_path = jwt_path
        self._renew_margin_s = renew_margin_s
        self._timeout_s = timeout_s
        self._clock = clock
        self._token: SecretValue | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _client_token(self) -> SecretValue:
        async with self._lock:
            if self._token is not None and self._clock() < self._token_expires_at:
                return self._token
            try:
                jwt = (await asyncio.to_thread(self._jwt_path.read_text, "utf-8")).strip()
                response = await self._client.post(
                    f"{self._address}/v1/auth/{self._auth_mount}/login",
                    json={"role": self._role, "jwt": jwt},
                    timeout=self._timeout_s,
                )
                response.raise_for_status()
                auth = response.json()["auth"]
                token = auth["client_token"]
                lease = float(auth.get("lease_duration", 0))
            except (OSError, httpx.HTTPError, KeyError, TypeError, ValueError):
                raise ServiceUnavailableError() from None
            if not isinstance(token, str) or not token:
                raise ServiceUnavailableError()
            self._token = SecretValue(token)
            self._token_expires_at = self._clock() + max(lease - self._renew_margin_s, 0.0)
            return self._token

    async def get(self, path: str, key: str) -> SecretValue:
        _check_ref(path, key)
        token = await self._client_token()
        try:
            response = await self._client.get(
                f"{self._address}/v1/{self._kv_mount}/data/{path}",
                headers={"X-Vault-Token": token.reveal()},
                timeout=self._timeout_s,
            )
        except httpx.HTTPError:
            raise ServiceUnavailableError() from None
        if response.status_code == 404:
            raise SecretError("secret not available")
        if response.status_code == 403:
            self._token = None  # token revoked or expired early; re-login next time
            raise ServiceUnavailableError()
        try:
            response.raise_for_status()
            value = response.json()["data"]["data"][key]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise SecretError("secret not available") from None
        if not isinstance(value, str):
            raise SecretError("secret not available")
        return SecretValue(value)
