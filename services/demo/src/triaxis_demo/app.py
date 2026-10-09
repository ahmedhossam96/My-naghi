"""Demo API and static UI.

Built on ``triaxis_common.create_base_app``, so request IDs, body limits, the
error catalogue and allow-list logging are the real Milestone 1 code.
Authentication uses the real ``TokenValidator`` and ``require_scope`` against
a demo identity provider that signs tokens with a key generated at start-up
(standing in for Keycloak).
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jwt.algorithms import ECAlgorithm
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from triaxis_common.auth import AuthContext, StaticKeyProvider, TokenValidator, require_scope
from triaxis_common.config import ResilienceMode, TenantConfig, load_tenants
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.http import create_base_app
from triaxis_common.logging import AllowListLogger
from triaxis_common.types import Slug, Workload
from triaxis_demo import Provenance, __version__
from triaxis_demo.audit import AuditBuffer
from triaxis_demo.demo_data import SecurityOps
from triaxis_demo.mocks import MockLlm, MockStt, MockTts
from triaxis_demo.overview import build_overview
from triaxis_demo.simulation import FailoverSimulator

STATIC_DIR: Final = Path(__file__).parent / "static"
DEFAULT_REPO: Final = Path(__file__).resolve().parents[4]
DEMO_ISSUER: Final = "http://127.0.0.1/demo-idp"
AUDIENCE: Final = "triaxis"
TOKEN_LIFETIME_S: Final = 900
MAX_BODY_BYTES: Final = 8 * 1024 * 1024
DEMO_KID: Final = "demo-idp-1"

SECURITY_HEADERS: Final = [
    (
        b"content-security-policy",
        (
            b"default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            b"style-src 'self'; script-src 'self'; connect-src 'self'; "
            b"frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        ),
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"microphone=(self), camera=(), geolocation=()"),
    (b"cache-control", b"no-store"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *SECURITY_HEADERS]}
            await send(message)

        await self._app(scope, receive, send_with_headers)


# --------------------------------------------------------------------- request bodies


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(_Body):
    tenant: Slug
    brand: Slug


class ChatMessage(_Body):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=8000)]


class ChatRequest(_Body):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=50)]


class SpeechRequest(_Body):
    input: Annotated[str, Field(min_length=1, max_length=2000)]
    voice: Literal["warm", "deep"] = "warm"


class SiteRequest(_Body):
    site: Literal["riyadh", "jeddah"]


class ModeRequest(_Body):
    mode: ResilienceMode


# --------------------------------------------------------------------- demo identity provider


class DemoIdentityProvider:
    """Signs short-lived Keycloak-shaped tokens. SIMULATED stand-in for Keycloak."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        jwk: dict[str, Any] = ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        jwk.update(kid=DEMO_KID, alg="ES256", use="sig")
        self.jwks: dict[str, Any] = {"keys": [jwk]}

    def mint(self, tenant: TenantConfig, brand_id: str) -> dict[str, Any]:
        brand = tenant.brand(brand_id)
        if brand is None:
            raise TriaxisError(ErrorCode.NOT_FOUND)
        now = int(time.time())
        scopes = sorted(f"{tenant.id}:{brand.id}:{w.value}" for w in brand.workloads)
        claims = {
            "iss": DEMO_ISSUER,
            "aud": AUDIENCE,
            "sub": f"demo-user-{uuid.uuid4().hex[:8]}",
            "azp": "triaxis-demo-console",
            "iat": now,
            "exp": now + TOKEN_LIFETIME_S,
            "tenant": tenant.id,
            "scope": " ".join(scopes),
        }
        token = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": DEMO_KID})
        return {
            "token": token,
            "expires_in": TOKEN_LIFETIME_S,
            "tenant": tenant.id,
            "brand": brand.id,
            "scopes": scopes,
            "provenance": {"issuer": Provenance.SIMULATED, "validation": Provenance.LIVE},
        }


# --------------------------------------------------------------------- app factory


def create_app(repo: Path = DEFAULT_REPO, *, llm: MockLlm | None = None) -> FastAPI:  # noqa: PLR0915 - route table
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))  # repository tooling (licence gate, registry)
    logger = AllowListLogger("demo")
    audit = AuditBuffer()
    tenants = load_tenants(repo / "config" / "tenants")
    idp = DemoIdentityProvider()
    simulator = FailoverSimulator()
    security = SecurityOps()
    mock_llm = llm or MockLlm()
    mock_stt = MockStt()
    mock_tts = MockTts()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        audit.attach()
        _app.state.ready = True
        try:
            yield
        finally:
            audit.detach()

    app = create_base_app(
        logger=logger, max_body_bytes=MAX_BODY_BYTES, title="triaxis-demo", version=__version__
    )
    app.router.lifespan_context = lifespan
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.token_validator = TokenValidator(
        issuer=DEMO_ISSUER, audience=AUDIENCE, keys=StaticKeyProvider(idp.jwks)
    )
    app.state.audit = audit
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ----------------------------------------------------------------- pages and meta

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return {
            "product": "Triaxis",
            "version": __version__,
            "notice": "Inference, site infrastructure and security operations are "
            "simulated; see the label on each panel.",
            "labels": {
                Provenance.LIVE: "Computed at runtime by real Triaxis code or configuration.",
                Provenance.SIMULATED: "A mock standing in for a service that is not built yet.",
                Provenance.DEMO_DATA: "Illustrative values, not measurements.",
            },
        }

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return build_overview(repo, tenants, simulator.state())

    @app.get("/api/tenants")
    async def list_tenants() -> dict[str, Any]:
        return {
            "provenance": Provenance.LIVE,
            "source": "config/tenants/*.yaml",
            "tenants": [
                {
                    "id": t.id,
                    "display_name": t.display_name,
                    "data_residency": t.data_residency,
                    "approved_origin_countries": sorted(t.approved_origin_countries),
                    "models": t.models.model_dump(),
                    "brands": [
                        {
                            "id": b.id,
                            "display_name": b.display_name,
                            "workloads": sorted(w.value for w in b.workloads),
                            "guardrail_policy": b.guardrail_policy,
                            "quotas": {
                                w.value: q.model_dump(exclude_none=True)
                                for w, q in sorted(b.quotas.items())
                            },
                        }
                        for b in t.brands
                    ],
                }
                for t in tenants.values()
            ],
        }

    @app.post("/api/session")
    async def session(body: SessionRequest) -> dict[str, Any]:
        tenant = tenants.get(body.tenant)
        if tenant is None:
            raise TriaxisError(ErrorCode.NOT_FOUND)
        return idp.mint(tenant, body.brand)

    # ----------------------------------------------------------------- playground

    @app.post("/api/chat")
    async def chat(
        body: ChatRequest,
        auth: AuthContext = Depends(require_scope(Workload.LLM)),  # noqa: B008
    ) -> StreamingResponse:
        prompt = body.messages[-1].content
        tokens_in = sum(MockLlm.count_tokens(m.content) for m in body.messages)

        async def events() -> AsyncIterator[bytes]:
            started = time.perf_counter()
            ttft: float | None = None
            tokens_out = 0
            async for delta in mock_llm.stream(prompt):
                if ttft is None:
                    ttft = (time.perf_counter() - started) * 1000
                tokens_out += 1
                yield _sse({"delta": delta})
            yield _sse(
                {
                    "done": True,
                    "ttft_ms": round(ttft or 0.0, 1),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": "mock-llm (simulated)",
                    "brand": auth.brand,
                }
            )

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"x-triaxis-provenance": "simulated"}
        )

    @app.post("/api/stt")
    async def stt(
        request: Request,
        language: Annotated[Literal["en", "ar"], Query()] = "en",
        _auth: AuthContext = Depends(require_scope(Workload.STT)),  # noqa: B008
    ) -> dict[str, Any]:
        data = await request.body()  # raw body: no multipart temp-file spooling
        result = mock_stt.transcribe(data, language)
        return {
            "text": result.text,
            "language": result.language,
            "audio_format": result.audio_format.value,
            "duration_ms": result.duration_ms,
            "bytes": len(data),
            "provenance": {"transcript": Provenance.SIMULATED, "validation": Provenance.LIVE},
        }

    @app.get("/api/stt/sample")
    async def stt_sample() -> Response:
        return Response(MockTts.sample_wav(), media_type="audio/wav")

    @app.post("/api/tts")
    async def tts(
        body: SpeechRequest,
        _auth: AuthContext = Depends(require_scope(Workload.TTS)),  # noqa: B008
    ) -> Response:
        speech = mock_tts.synthesise(body.input, body.voice)
        return Response(
            speech.wav,
            media_type="audio/wav",
            headers={
                "x-triaxis-provenance": "simulated",
                "x-triaxis-sentences": str(speech.sentences),
                "x-triaxis-duration-ms": str(speech.duration_ms),
            },
        )

    # ----------------------------------------------------------------- security operations

    @app.get("/api/security")
    async def security_snapshot() -> dict[str, Any]:
        return security.snapshot()

    @app.post("/api/security/remediation/{item_id}/advance")
    async def advance(item_id: Annotated[str, Field(pattern=r"^REM-\d{1,5}$")]) -> dict[str, Any]:
        return security.advance(item_id)

    # ----------------------------------------------------------------- infrastructure

    @app.get("/api/infra")
    async def infra() -> dict[str, Any]:
        return simulator.state()

    @app.post("/api/infra/failover")
    async def failover(body: SiteRequest) -> dict[str, Any]:
        simulator.fail(body.site)
        return simulator.state()

    @app.post("/api/infra/restore")
    async def restore() -> dict[str, Any]:
        simulator.restore()
        return simulator.state()

    @app.post("/api/infra/reset")
    async def reset() -> dict[str, Any]:
        simulator.reset()
        return simulator.state()

    @app.post("/api/infra/mode")
    async def mode(body: ModeRequest) -> dict[str, Any]:
        simulator.set_mode(body.mode)
        return simulator.state()

    # ----------------------------------------------------------------- audit

    @app.get("/api/audit")
    async def audit_log(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
        return {
            "provenance": Provenance.LIVE,
            "source": "allow-listed log records emitted by this process (ADR 0007)",
            "records": audit.records(limit),
        }

    return app


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
