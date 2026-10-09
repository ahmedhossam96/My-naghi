"""Test-only token minting. Never shipped in a service image.

Mints Keycloak-shaped access tokens signed with throwaway keys generated per
test session, plus the matching JWKS.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = "https://keycloak.triaxis.svc/realms/triaxis"
AUDIENCE = "triaxis"
RSA_KID = "rsa-1"
EC_KID = "ec-1"


class TokenFactory:
    def __init__(self, issuer: str = ISSUER, audience: str = AUDIENCE) -> None:
        self.issuer = issuer
        self.audience = audience
        self.rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.other_rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        rsa_jwk: dict[str, Any] = RSAAlgorithm.to_jwk(self.rsa_key.public_key(), as_dict=True)
        ec_jwk: dict[str, Any] = ECAlgorithm.to_jwk(self.ec_key.public_key(), as_dict=True)
        rsa_jwk.update(kid=RSA_KID, alg="RS256", use="sig")
        ec_jwk.update(kid=EC_KID, alg="ES256", use="sig")
        return {"keys": [rsa_jwk, ec_jwk]}

    def claims(
        self,
        *,
        tenant: str = "mynaghi",
        scopes: Iterable[str] = ("mynaghi:toyota:stt",),
        lifetime_s: int = 300,
        now: float | None = None,
        overrides: Mapping[str, Any] | None = None,
        drop: Iterable[str] = (),
    ) -> dict[str, Any]:
        issued = int(now if now is not None else time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": "service-account-mynaghi-backend",
            "azp": "mynaghi-backend",
            "iat": issued,
            "exp": issued + lifetime_s,
            "tenant": tenant,
            "scope": " ".join(["profile", *scopes]),
        }
        claims.update(overrides or {})
        for name in drop:
            claims.pop(name, None)
        return claims

    def mint(
        self,
        *,
        alg: str = "RS256",
        kid: str | None = RSA_KID,
        key: Any = None,
        headers: Mapping[str, Any] | None = None,
        **claim_args: Any,
    ) -> str:
        signing_key = key
        if signing_key is None:
            signing_key = self.ec_key if alg.startswith("ES") else self.rsa_key
        extra_headers: dict[str, Any] = dict(headers or {})
        if kid is not None:
            extra_headers["kid"] = kid
        return jwt.encode(
            self.claims(**claim_args), signing_key, algorithm=alg, headers=extra_headers
        )
