from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from tests.support.tokens import AUDIENCE, EC_KID, ISSUER, RSA_KID, TokenFactory
from triaxis_common import context
from triaxis_common.auth import (
    MAX_TOKEN_BYTES,
    AuthContext,
    Principal,
    Scope,
    StaticKeyProvider,
    TokenValidator,
    authorize,
    bearer_token,
    require_scope,
)
from triaxis_common.errors import AuthError, ErrorCode, TriaxisError
from triaxis_common.http import create_base_app
from triaxis_common.logging import AllowListLogger
from triaxis_common.types import Workload

# --------------------------------------------------------------------- scope parsing


def test_scope_round_trip() -> None:
    scope = Scope.parse("mynaghi:toyota:stt")
    assert scope == Scope("mynaghi", "toyota", Workload.STT)
    assert str(scope) == "mynaghi:toyota:stt"


@pytest.mark.parametrize(
    "raw",
    [
        "mynaghi:toyota",
        "mynaghi:toyota:stt:extra",
        "mynaghi:*:stt",
        "*:*:*",
        "MyNaghi:toyota:stt",
        "mynaghi:toyota:STT",
        "mynaghi:toyota:admin",
        "-mynaghi:toyota:stt",
        "mynaghi-:toyota:stt",
        "mynaghi::stt",
        ":toyota:stt",
        "mynaghi:toyota:stt ",
        "mynaghi:toyota:stt\n",
        "mynaghi:tوyota:stt",  # Arabic letter
        "mynaghi:١٢:stt",  # Arabic-Indic digits
        "a" * 33 + ":toyota:stt",
    ],
)
def test_scope_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError, match="malformed scope"):
        Scope.parse(raw)


@given(st.text(max_size=80))
def test_property_scope_parse_is_total_and_canonical(raw: str) -> None:
    """Any string either fails cleanly or round-trips exactly."""
    try:
        scope = Scope.parse(raw)
    except ValueError:
        return
    assert str(scope) == raw


@given(
    tenant=st.from_regex(r"[a-z0-9]{1,10}", fullmatch=True),
    brand=st.from_regex(r"[a-z0-9]{1,10}", fullmatch=True),
    workload=st.sampled_from(list(Workload)),
)
def test_property_principal_allows_only_exact_scope(
    tenant: str, brand: str, workload: Workload
) -> None:
    principal = Principal("s", "c", tenant, frozenset({Scope(tenant, brand, workload)}), 0)
    assert principal.allows(tenant, brand, workload)
    other = next(w for w in Workload if w is not workload)
    assert not principal.allows(tenant, brand, other)
    assert not principal.allows(tenant, brand + "x", workload)
    assert not principal.allows(tenant + "x", brand, workload)


def test_principal_requires_matching_tenant_claim() -> None:
    # Defensive: even if a scope names tenant B, the principal's tenant must match.
    principal = Principal("s", "c", "tenanta", frozenset({Scope("tenantb", "x", Workload.STT)}), 0)
    assert not principal.allows("tenantb", "x", Workload.STT)


# --------------------------------------------------------------------- token validation


async def test_valid_rsa_token(tokens: TokenFactory, validator: TokenValidator) -> None:
    principal = await validator.validate(
        tokens.mint(scopes=["mynaghi:toyota:stt", "mynaghi:lexus:llm"])
    )
    assert principal.tenant == "mynaghi"
    assert principal.client_id == "mynaghi-backend"
    assert principal.scopes == {
        Scope("mynaghi", "toyota", Workload.STT),
        Scope("mynaghi", "lexus", Workload.LLM),
    }


async def test_valid_ec_token(tokens: TokenFactory, validator: TokenValidator) -> None:
    principal = await validator.validate(tokens.mint(alg="ES256", kid=EC_KID))
    assert principal.tenant == "mynaghi"


async def _rejects(validator: TokenValidator, token: str, code: ErrorCode) -> None:
    with pytest.raises(AuthError) as excinfo:
        await validator.validate(token)
    assert excinfo.value.code is code


async def test_expired(tokens: TokenFactory, validator: TokenValidator) -> None:
    token = tokens.mint(now=time.time() - 1000, lifetime_s=300)
    await _rejects(validator, token, ErrorCode.TOKEN_EXPIRED)


async def test_expired_within_library_leeway_but_past_our_clock(tokens: TokenFactory) -> None:
    # Our own clock check catches expiry independently of the library.
    future = time.time() + 120
    validator = TokenValidator(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys=StaticKeyProvider(tokens.jwks()),
        leeway_s=0,
        clock=lambda: future,
    )
    token = tokens.mint(lifetime_s=60)
    await _rejects(validator, token, ErrorCode.TOKEN_EXPIRED)


async def test_issued_in_future(tokens: TokenFactory) -> None:
    validator = TokenValidator(
        issuer=ISSUER,
        audience=AUDIENCE,
        keys=StaticKeyProvider(tokens.jwks()),
        clock=lambda: time.time() - 600,
    )
    await _rejects(validator, tokens.mint(), ErrorCode.INVALID_TOKEN)


async def test_not_yet_valid(tokens: TokenFactory, validator: TokenValidator) -> None:
    token = tokens.mint(overrides={"nbf": int(time.time()) + 600})
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


async def test_lifetime_over_15_minutes(tokens: TokenFactory, validator: TokenValidator) -> None:
    await _rejects(validator, tokens.mint(lifetime_s=901), ErrorCode.INVALID_TOKEN)


async def test_lifetime_exactly_15_minutes(tokens: TokenFactory, validator: TokenValidator) -> None:
    await validator.validate(tokens.mint(lifetime_s=900))


async def test_wrong_issuer(tokens: TokenFactory, validator: TokenValidator) -> None:
    token = tokens.mint(overrides={"iss": "https://evil.example/realms/triaxis"})
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


async def test_wrong_audience(tokens: TokenFactory, validator: TokenValidator) -> None:
    await _rejects(validator, tokens.mint(overrides={"aud": "account"}), ErrorCode.INVALID_TOKEN)


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub", "azp", "tenant", "scope"])
async def test_missing_required_claim(
    tokens: TokenFactory, validator: TokenValidator, claim: str
) -> None:
    await _rejects(validator, tokens.mint(drop=[claim]), ErrorCode.INVALID_TOKEN)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant": "MyNaghi"},
        {"tenant": 7},
        {"sub": ""},
        {"azp": ""},
        {"scope": ["mynaghi:toyota:stt"]},
        {"exp": 1.5e12},
    ],
)
async def test_malformed_claims(
    tokens: TokenFactory, validator: TokenValidator, overrides: dict[str, object]
) -> None:
    with pytest.raises(AuthError):
        await validator.validate(tokens.mint(overrides=overrides))


async def test_float_iat_rejected(tokens: TokenFactory, validator: TokenValidator) -> None:
    now = time.time()
    token = tokens.mint(overrides={"iat": now, "exp": int(now) + 60})
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


async def test_no_triaxis_scope(tokens: TokenFactory, validator: TokenValidator) -> None:
    await _rejects(validator, tokens.mint(scopes=[]), ErrorCode.INSUFFICIENT_SCOPE)


async def test_malformed_scope_rejects_token(
    tokens: TokenFactory, validator: TokenValidator
) -> None:
    token = tokens.mint(scopes=["mynaghi:toyota:stt", "mynaghi:*:llm"])
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


async def test_cross_tenant_scope_rejects_token(
    tokens: TokenFactory, validator: TokenValidator
) -> None:
    token = tokens.mint(scopes=["mynaghi:toyota:stt", "othertenant:brand:stt"])
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


async def test_alg_none_rejected(tokens: TokenFactory, validator: TokenValidator) -> None:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": RSA_KID}).encode())
    payload = base64.urlsafe_b64encode(json.dumps(tokens.claims()).encode())
    token = f"{header.decode().rstrip('=')}.{payload.decode().rstrip('=')}."
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


async def test_hs256_with_public_key_rejected(
    tokens: TokenFactory, validator: TokenValidator
) -> None:
    """Algorithm confusion: HMAC signed with the RSA public key as the secret."""
    claims = tokens.claims()
    header = {"alg": "HS256", "kid": RSA_KID, "typ": "JWT"}

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    pem = tokens.rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    signature = hmac.new(pem, signing_input.encode(), hashlib.sha256).digest()
    await _rejects(validator, f"{signing_input}.{b64(signature)}", ErrorCode.INVALID_TOKEN)


async def test_alg_mismatch_with_jwk(tokens: TokenFactory, validator: TokenValidator) -> None:
    # PS256 is allowed globally, but the RSA JWK declares RS256.
    await _rejects(validator, tokens.mint(alg="PS256"), ErrorCode.INVALID_TOKEN)


async def test_unknown_kid(tokens: TokenFactory, validator: TokenValidator) -> None:
    await _rejects(validator, tokens.mint(kid="unknown"), ErrorCode.INVALID_TOKEN)


@pytest.mark.parametrize("kid", [None, "", "k" * 257])
async def test_bad_kid(tokens: TokenFactory, validator: TokenValidator, kid: str | None) -> None:
    await _rejects(validator, tokens.mint(kid=kid), ErrorCode.INVALID_TOKEN)


async def test_signed_by_other_key(tokens: TokenFactory, validator: TokenValidator) -> None:
    await _rejects(validator, tokens.mint(key=tokens.other_rsa_key), ErrorCode.INVALID_TOKEN)


async def test_tampered_payload(tokens: TokenFactory, validator: TokenValidator) -> None:
    header, _payload, signature = tokens.mint().split(".")
    forged = tokens.claims(scopes=["mynaghi:lexus:llm"])
    payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    await _rejects(validator, f"{header}.{payload}.{signature}", ErrorCode.INVALID_TOKEN)


@pytest.mark.parametrize("token", ["", "abc", "a.b.c", "x" * (MAX_TOKEN_BYTES + 1), "١.٢.٣"])
async def test_garbage_tokens(validator: TokenValidator, token: str) -> None:
    await _rejects(validator, token, ErrorCode.INVALID_TOKEN)


@given(st.text(max_size=300))
async def test_property_garbage_never_validates(token: str) -> None:
    factory = _shared_factory()
    v = TokenValidator(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeyProvider(factory.jwks()))
    with pytest.raises(AuthError):
        await v.validate(token)


_FACTORY: list[TokenFactory] = []


def _shared_factory() -> TokenFactory:
    if not _FACTORY:
        _FACTORY.append(TokenFactory())
    return _FACTORY[0]


def test_validator_rejects_unsafe_configuration(tokens: TokenFactory) -> None:
    keys = StaticKeyProvider(tokens.jwks())
    with pytest.raises(ValueError, match="lifetime"):
        TokenValidator(issuer=ISSUER, audience=AUDIENCE, keys=keys, max_lifetime_s=3600)
    with pytest.raises(ValueError, match="leeway"):
        TokenValidator(issuer=ISSUER, audience=AUDIENCE, keys=keys, leeway_s=300)


def test_static_provider_skips_unusable_keys(tokens: TokenFactory) -> None:
    jwks = tokens.jwks()
    jwks["keys"] += [
        {"kty": "oct", "k": "c2VjcmV0", "kid": "hmac", "alg": "HS256"},
        {**jwks["keys"][0], "kid": "enc", "use": "enc"},
        {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
        {**jwks["keys"][0], "kid": None},
    ]
    provider = StaticKeyProvider(jwks)
    assert set(provider._keys) == {RSA_KID, EC_KID}


# --------------------------------------------------------------------- authorisation


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, ErrorCode.MISSING_TOKEN),
        ("", ErrorCode.MISSING_TOKEN),
        ("Basic abc", ErrorCode.MISSING_TOKEN),
        ("Bearer", ErrorCode.MISSING_TOKEN),
        ("Bearer   ", ErrorCode.MISSING_TOKEN),
    ],
)
def test_bearer_token_errors(header: str | None, expected: ErrorCode) -> None:
    with pytest.raises(AuthError) as excinfo:
        bearer_token(header)
    assert excinfo.value.code is expected


def test_bearer_token_case_insensitive() -> None:
    assert bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


async def _authorize(
    validator: TokenValidator,
    token: str,
    workload: Workload = Workload.STT,
    brand: str | None = None,
    tenant: str | None = None,
) -> AuthContext:
    return await authorize(
        validator,
        authorization=f"Bearer {token}",
        brand_header=brand,
        tenant_header=tenant,
        workload=workload,
    )


async def test_authorize_single_brand_inferred(
    tokens: TokenFactory, validator: TokenValidator
) -> None:
    token_ctx = context.set_context(context.RequestContext(request_id="req-00000001"))
    try:
        auth = await _authorize(validator, tokens.mint())
        assert (auth.tenant, auth.brand, auth.workload) == ("mynaghi", "toyota", Workload.STT)
        assert context.current().brand == "toyota"
    finally:
        context.reset_context(token_ctx)


async def test_authorize_explicit_brand(tokens: TokenFactory, validator: TokenValidator) -> None:
    token = tokens.mint(scopes=["mynaghi:toyota:stt", "mynaghi:lexus:stt"])
    auth = await _authorize(validator, token, brand="lexus")
    assert auth.brand == "lexus"


async def test_authorize_ambiguous_brand(tokens: TokenFactory, validator: TokenValidator) -> None:
    token = tokens.mint(scopes=["mynaghi:toyota:stt", "mynaghi:lexus:stt"])
    with pytest.raises(TriaxisError) as excinfo:
        await _authorize(validator, token)
    assert excinfo.value.code is ErrorCode.BRAND_REQUIRED


@pytest.mark.parametrize(
    ("workload", "brand", "tenant"),
    [
        (Workload.LLM, None, None),  # wrong workload
        (Workload.STT, "lexus", None),  # cross-brand
        (Workload.STT, "Toyota", None),  # non-slug brand
        (Workload.STT, "toyota", "othertenant"),  # cross-tenant header
    ],
)
async def test_authorize_denied(
    tokens: TokenFactory,
    validator: TokenValidator,
    workload: Workload,
    brand: str | None,
    tenant: str | None,
) -> None:
    with pytest.raises(AuthError) as excinfo:
        await _authorize(validator, tokens.mint(), workload, brand, tenant)
    assert excinfo.value.code is ErrorCode.INSUFFICIENT_SCOPE


async def test_authorize_other_tenant_token_cannot_reach_brand(
    tokens: TokenFactory, validator: TokenValidator
) -> None:
    token = tokens.mint(tenant="othertenant", scopes=["othertenant:toyota:stt"])
    with pytest.raises(AuthError):
        await _authorize(validator, token, brand="toyota", tenant="mynaghi")


def test_require_scope_dependency(tokens: TokenFactory, validator: TokenValidator) -> None:
    app = create_base_app(logger=AllowListLogger("stt"), max_body_bytes=1024)
    app.state.token_validator = validator

    @app.get("/v1/whoami")
    async def whoami(auth: AuthContext = Depends(require_scope(Workload.STT))) -> dict[str, str]:  # noqa: B008
        return {"tenant": auth.tenant, "brand": auth.brand}

    client = TestClient(app)
    ok = client.get("/v1/whoami", headers={"Authorization": f"Bearer {tokens.mint()}"})
    assert ok.status_code == 200
    assert ok.json() == {"tenant": "mynaghi", "brand": "toyota"}

    missing = client.get("/v1/whoami")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"]["code"] == "missing_token"

    expired = client.get(
        "/v1/whoami",
        headers={"Authorization": f"Bearer {tokens.mint(now=time.time() - 3600)}"},
    )
    assert expired.json()["error"]["code"] == "token_expired"

    cross = client.get(
        "/v1/whoami",
        headers={"Authorization": f"Bearer {tokens.mint()}", "X-Triaxis-Tenant": "other"},
    )
    assert cross.status_code == 403


def test_pyjwt_algorithms_still_reject_none() -> None:
    # Guard against a library upgrade silently changing behaviour.
    unsigned = jwt.encode({"a": 1}, key=None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(
            unsigned,
            options={"verify_signature": True},
            algorithms=["RS256"],
            key="x",
        )
