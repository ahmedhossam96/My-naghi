"""JWT validation and ``tenant:brand:workload`` authorisation (ADR 0008).

Every service re-validates the bearer token even though the gateway already
did (defense in depth). Deny by default.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
import jwt
from fastapi import Request

from triaxis_common import context
from triaxis_common.config import MAX_TOKEN_LIFETIME_S, require_in_cluster_url
from triaxis_common.errors import AuthError, ErrorCode, ServiceUnavailableError, TriaxisError
from triaxis_common.logging import AllowListLogger, Event, Level
from triaxis_common.types import SLUG_PATTERN, Workload, is_slug

ALLOWED_ALGORITHMS: Final[frozenset[str]] = frozenset({"RS256", "PS256", "ES256"})
MAX_TOKEN_BYTES: Final = 8192
MAX_LEEWAY_S: Final = 30
BRAND_HEADER: Final = "x-triaxis-brand"
TENANT_HEADER: Final = "x-triaxis-tenant"

_SLUG = SLUG_PATTERN.removeprefix("^").removesuffix("$")
_WORKLOADS = "|".join(w.value for w in Workload)
_SCOPE_RE: Final = re.compile(
    rf"^(?P<tenant>{_SLUG}):(?P<brand>{_SLUG}):(?P<workload>{_WORKLOADS})$"
)


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@dataclass(frozen=True, slots=True)
class Scope:
    """One grant: a workload for a brand of a tenant. No wildcards."""

    tenant: str
    brand: str
    workload: Workload

    @classmethod
    def parse(cls, raw: str) -> Scope:
        match = _SCOPE_RE.fullmatch(raw)
        if match is None:
            raise ValueError("malformed scope")
        return cls(match["tenant"], match["brand"], Workload(match["workload"]))

    def __str__(self) -> str:
        return f"{self.tenant}:{self.brand}:{self.workload.value}"

    def matches(self, tenant: str, brand: str, workload: Workload) -> bool:
        # Evaluate all three comparisons; no short-circuit on the first mismatch.
        results = (
            _eq(self.tenant, tenant),
            _eq(self.brand, brand),
            _eq(self.workload.value, workload.value),
        )
        return all(results)


@dataclass(frozen=True, slots=True)
class Principal:
    """A validated caller."""

    subject: str
    client_id: str
    tenant: str
    scopes: frozenset[Scope]
    expires_at: int

    def allows(self, tenant: str, brand: str, workload: Workload) -> bool:
        granted = False
        for scope in self.scopes:
            granted |= scope.matches(tenant, brand, workload)
        return granted and _eq(self.tenant, tenant)

    def brands_for(self, workload: Workload) -> frozenset[str]:
        return frozenset(s.brand for s in self.scopes if s.workload is workload)


class KeyProvider(Protocol):
    async def get_key(self, kid: str, alg: str) -> Any:
        """Return the verification key for ``kid`` or raise :class:`AuthError`."""
        ...


def _key_from_jwk(jwk: Mapping[str, Any]) -> jwt.PyJWK | None:
    """Build a PyJWK for signature use, or None if the key is unusable."""
    if jwk.get("use", "sig") != "sig":
        return None
    alg = jwk.get("alg")
    if alg is not None and alg not in ALLOWED_ALGORITHMS:
        return None
    try:
        return jwt.PyJWK.from_dict(dict(jwk))
    except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError):
        return None


def _check_alg(jwk: jwt.PyJWK, alg: str) -> Any:
    jwk_alg = jwk._jwk_data.get("alg")
    if jwk_alg is not None and jwk_alg != alg:
        raise AuthError(ErrorCode.INVALID_TOKEN)
    return jwk.key


class StaticKeyProvider:
    """Fixed key set (tests, dev). Accepts a JWKS document."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._keys: dict[str, jwt.PyJWK] = {}
        for jwk in jwks.get("keys", []):
            key = _key_from_jwk(jwk)
            if key is not None and isinstance(jwk.get("kid"), str):
                self._keys[jwk["kid"]] = key

    async def get_key(self, kid: str, alg: str) -> Any:
        key = self._keys.get(kid)
        if key is None:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        return _check_alg(key, alg)


class JwksCache:
    """JWKS from the in-cluster Keycloak, cached (ADR 0008).

    - Refresh when older than ``ttl_s``.
    - Refresh on an unknown ``kid`` at most once per ``min_refresh_interval_s``
      (an attacker cannot force a fetch per request).
    - If Keycloak is unreachable, keep serving the last good set for up to
      ``max_stale_s``; after that, fail closed.
    """

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        *,
        allow_http: bool = False,
        ttl_s: float = 300.0,
        min_refresh_interval_s: float = 30.0,
        max_stale_s: float = 3600.0,
        timeout_s: float = 2.0,
        logger: AllowListLogger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = require_in_cluster_url(url, allow_http=allow_http)
        self._client = client
        self._ttl_s = ttl_s
        self._min_refresh_interval_s = min_refresh_interval_s
        self._max_stale_s = max_stale_s
        self._timeout_s = timeout_s
        self._logger = logger
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str, alg: str) -> Any:
        now = self._clock()
        if (
            self._fetched_at is None
            or now - self._fetched_at >= self._ttl_s
            or kid not in self._keys
        ):
            await self._refresh()
        if self._fetched_at is None or self._clock() - self._fetched_at >= self._max_stale_s:
            raise ServiceUnavailableError()
        key = self._keys.get(kid)
        if key is None:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        return _check_alg(key, alg)

    async def _refresh(self) -> None:
        async with self._lock:
            now = self._clock()
            # One attempt per interval, successful or not: no stampede on Keycloak,
            # and an unknown kid cannot force a fetch per request.
            if (
                self._last_attempt is not None
                and now - self._last_attempt < self._min_refresh_interval_s
            ):
                return
            self._last_attempt = now
            try:
                response = await self._client.get(self._url, timeout=self._timeout_s)
                response.raise_for_status()
                document = response.json()
                keys = {
                    jwk["kid"]: key
                    for jwk in document.get("keys", [])
                    if isinstance(jwk, dict)
                    and isinstance(jwk.get("kid"), str)
                    and (key := _key_from_jwk(jwk)) is not None
                }
            except (httpx.HTTPError, json.JSONDecodeError, AttributeError, TypeError) as exc:
                if self._logger is not None:
                    self._logger.log(Event.JWKS_REFRESH_FAILED, Level.WARNING, exc=exc)
                return
            self._keys = keys
            self._fetched_at = now
            if self._logger is not None:
                self._logger.info(Event.JWKS_REFRESHED)


class TokenValidator:
    """Validates Keycloak access tokens against the Triaxis token model."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        keys: KeyProvider,
        max_lifetime_s: int = MAX_TOKEN_LIFETIME_S,
        leeway_s: int = MAX_LEEWAY_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < max_lifetime_s <= MAX_TOKEN_LIFETIME_S:
            raise ValueError("token lifetime must be within (0, 900] seconds")
        if not 0 <= leeway_s <= MAX_LEEWAY_S:
            raise ValueError("leeway must be within [0, 30] seconds")
        self._issuer = issuer
        self._audience = audience
        self._keys = keys
        self._max_lifetime_s = max_lifetime_s
        self._leeway_s = leeway_s
        self._clock = clock

    async def validate(self, token: str) -> Principal:
        if not token or len(token) > MAX_TOKEN_BYTES or not token.isascii():
            raise AuthError(ErrorCode.INVALID_TOKEN)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError(ErrorCode.INVALID_TOKEN) from None
        alg = header.get("alg")
        kid = header.get("kid")
        if not isinstance(alg, str) or alg not in ALLOWED_ALGORITHMS:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        key = await self._keys.get_key(kid, alg)
        claims = self._decode(token, key, alg)
        return self._principal(claims)

    def _decode(self, token: str, key: Any, alg: str) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_s,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError:
            raise AuthError(ErrorCode.TOKEN_EXPIRED) from None
        except jwt.PyJWTError:
            raise AuthError(ErrorCode.INVALID_TOKEN) from None
        return claims

    def _principal(self, claims: Mapping[str, Any]) -> Principal:
        exp, iat = claims["exp"], claims["iat"]
        if not isinstance(exp, int) or not isinstance(iat, int):
            raise AuthError(ErrorCode.INVALID_TOKEN)
        if exp - iat > self._max_lifetime_s:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        # Re-check expiry against our own clock (independent of library defaults).
        if exp + self._leeway_s <= self._clock():
            raise AuthError(ErrorCode.TOKEN_EXPIRED)
        if iat - self._leeway_s > self._clock():
            raise AuthError(ErrorCode.INVALID_TOKEN)

        tenant = claims.get("tenant")
        subject = claims.get("sub")
        client_id = claims.get("azp")
        raw_scope = claims.get("scope")
        if not isinstance(tenant, str) or not is_slug(tenant):
            raise AuthError(ErrorCode.INVALID_TOKEN)
        if not isinstance(subject, str) or not subject or len(subject) > 256:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        if not isinstance(client_id, str) or not client_id or len(client_id) > 256:
            raise AuthError(ErrorCode.INVALID_TOKEN)
        if not isinstance(raw_scope, str):
            raise AuthError(ErrorCode.INVALID_TOKEN)

        scopes = _triaxis_scopes(raw_scope, tenant)
        return Principal(
            subject=subject,
            client_id=client_id,
            tenant=tenant,
            scopes=scopes,
            expires_at=exp,
        )


def _triaxis_scopes(raw_scope: str, tenant: str) -> frozenset[Scope]:
    """Parse the ``scope`` claim. Malformed or cross-tenant entries reject the token."""
    scopes: set[Scope] = set()
    for entry in raw_scope.split():
        if ":" not in entry:
            continue  # standard OIDC scopes (profile, email) are ignored
        try:
            scope = Scope.parse(entry)
        except ValueError:
            raise AuthError(ErrorCode.INVALID_TOKEN) from None
        if not _eq(scope.tenant, tenant):
            raise AuthError(ErrorCode.INVALID_TOKEN)  # cross-tenant scope: reject the token
        scopes.add(scope)
    if not scopes:
        raise AuthError(ErrorCode.INSUFFICIENT_SCOPE)
    return frozenset(scopes)


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: Principal
    tenant: str
    brand: str
    workload: Workload


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthError(ErrorCode.MISSING_TOKEN)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(ErrorCode.MISSING_TOKEN)
    return token.strip()


async def authorize(
    validator: TokenValidator,
    *,
    authorization: str | None,
    brand_header: str | None,
    tenant_header: str | None,
    workload: Workload,
) -> AuthContext:
    """Validate the token and authorise ``(tenant, brand, workload)``."""
    principal = await validator.validate(bearer_token(authorization))
    tenant = principal.tenant
    if tenant_header is not None and not _eq(tenant_header, tenant):
        raise AuthError(ErrorCode.INSUFFICIENT_SCOPE)
    if brand_header is None:
        brands = principal.brands_for(workload)
        if len(brands) != 1:
            if not brands:
                raise AuthError(ErrorCode.INSUFFICIENT_SCOPE)
            raise TriaxisError(ErrorCode.BRAND_REQUIRED)
        (brand,) = brands
    else:
        brand = brand_header
        if not is_slug(brand):
            raise AuthError(ErrorCode.INSUFFICIENT_SCOPE)
    if not principal.allows(tenant, brand, workload):
        raise AuthError(ErrorCode.INSUFFICIENT_SCOPE)
    context.update_context(tenant=tenant, brand=brand, workload=workload)
    return AuthContext(principal=principal, tenant=tenant, brand=brand, workload=workload)


def require_scope(workload: Workload) -> Callable[[Request], Any]:
    """FastAPI dependency: ``Depends(require_scope(Workload.STT))``.

    Expects ``app.state.token_validator`` to hold a :class:`TokenValidator`.
    """

    async def dependency(request: Request) -> AuthContext:
        validator: TokenValidator = request.app.state.token_validator
        return await authorize(
            validator,
            authorization=request.headers.get("authorization"),
            brand_header=request.headers.get(BRAND_HEADER),
            tenant_header=request.headers.get(TENANT_HEADER),
            workload=workload,
        )

    return dependency
