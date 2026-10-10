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
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jwt.algorithms import ECAlgorithm
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from triaxis_common import context
from triaxis_common.auth import (
    BRAND_HEADER,
    AuthContext,
    StaticKeyProvider,
    TokenValidator,
    bearer_token,
    require_scope,
)
from triaxis_common.config import ResilienceMode, TenantConfig, load_tenants
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.http import create_base_app
from triaxis_common.inference import ChatMessage, InferenceClients
from triaxis_common.logging import AllowListLogger
from triaxis_common.policy import PolicyEngine, load_policies, summarise
from triaxis_common.taxonomy import Classification, Destination
from triaxis_common.types import Slug, Workload
from triaxis_demo import Provenance, __version__
from triaxis_demo.audit import AuditBuffer
from triaxis_demo.demo_data import SecurityOps
from triaxis_demo.mocks import MockLlm, MockTts, mock_clients
from triaxis_demo.overview import build_overview
from triaxis_demo.pipeline import (
    AiPipeline,
    PolicyBlockedError,
    RequestScope,
    blocked_body,
    decision_headers,
)
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


class ChatTurn(_Body):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(min_length=1, max_length=8000)]


class ChatRequest(_Body):
    messages: Annotated[list[ChatTurn], Field(min_length=1, max_length=50)]
    # The client may raise the classification of its own request, never lower it.
    classification: Classification | None = None


class ClassifyRequest(_Body):
    text: Annotated[str, Field(min_length=1, max_length=8000)]
    workload: Workload = Workload.LLM
    destination: Destination = Destination.INFERENCE
    requested_level: Classification | None = None


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


async def any_scope(request: Request) -> AuthContext:
    """Valid token for the tenant, brand from the header or the token's single brand."""
    validator: TokenValidator = request.app.state.token_validator
    principal = await validator.validate(bearer_token(request.headers.get("authorization")))
    brands = {s.brand for s in principal.scopes}
    brand = request.headers.get(BRAND_HEADER) or (next(iter(brands)) if len(brands) == 1 else "")
    if brand not in brands:
        raise TriaxisError(ErrorCode.INSUFFICIENT_SCOPE)
    workload = next(s.workload for s in sorted(principal.scopes, key=str) if s.brand == brand)
    context.update_context(tenant=principal.tenant, brand=brand)
    return AuthContext(principal=principal, tenant=principal.tenant, brand=brand, workload=workload)


# --------------------------------------------------------------------- app factory


def create_app(  # noqa: PLR0915 - route table
    repo: Path = DEFAULT_REPO,
    *,
    llm: MockLlm | None = None,
    inference: InferenceClients | None = None,
    backup_root: Path | None = None,
) -> FastAPI:
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))  # repository tooling (licence gate, registry)
    logger = AllowListLogger("demo")
    audit = AuditBuffer()
    tenants = load_tenants(repo / "config" / "tenants")
    idp = DemoIdentityProvider()
    simulator = FailoverSimulator()
    security = SecurityOps()
    engine = PolicyEngine(
        load_policies(repo / "config" / "policies"),
        {t.id: t.data_policy for t in tenants.values()},
        logger=logger,
    )
    pipeline = AiPipeline(engine, inference or mock_clients(llm), logger)

    def scope_of(auth: AuthContext) -> RequestScope:
        return RequestScope(tenants[auth.tenant], auth.brand, context.current().request_id)

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
    app.state.pipeline = pipeline

    async def policy_blocked(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, PolicyBlockedError)  # noqa: S101 - registered for this type
        request.state.triaxis_error_code = ErrorCode.POLICY_BLOCKED
        request.state.triaxis_error_class = type(exc).__name__
        return JSONResponse(
            blocked_body(context.current().request_id),
            status_code=422,
            headers=decision_headers(exc.result),
        )

    app.add_exception_handler(PolicyBlockedError, policy_blocked)
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
            "notice": (
                "Model output is simulated in mock mode (see the mode indicator); site "
                "infrastructure and security operations are simulated. Classification, "
                "policy, auth, validation and audit are real. See the label on each panel."
            ),
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

    @app.get("/api/inference/status")
    async def inference_status(
        auth: AuthContext = Depends(any_scope),  # noqa: B008
    ) -> dict[str, Any]:
        status = await pipeline.status(scope_of(auth))
        status["provenance"] = Provenance.LIVE
        return status

    @app.get("/api/backup/status")
    async def backup_status(auth: AuthContext = Depends(any_scope)) -> dict[str, Any]:  # noqa: B008
        """Live backup inventory (unverified listing) and verification / restore-test ledgers."""
        del auth
        if backup_root is None:
            return {
                "provenance": Provenance.LIVE,
                "configured": False,
                "state": "unavailable",
                "reason": "TRIAXIS_BACKUP_DIR not set",
            }
        from tools.backup import (  # noqa: PLC0415 - repository tooling
            RESTORE_TESTS,
            VERIFICATIONS,
            BackupService,
            NoKeys,
        )

        service = BackupService(backup_root, NoKeys())
        backups = service.list_backups()
        completed = [b for b in backups if "created_at" in b]
        latest = max(completed, key=lambda b: b["created_at"], default=None)
        return {
            "provenance": Provenance.LIVE,
            "configured": True,
            "state": "ok" if latest else "no_backups",
            "count": len(completed),
            "latest": latest,
            "verifications": service.ledger(VERIFICATIONS)[-5:],
            "restore_tests": service.ledger(RESTORE_TESTS)[-5:],
        }

    @app.post("/api/classify")
    async def classify(
        body: ClassifyRequest,
        auth: AuthContext = Depends(any_scope),  # noqa: B008
    ) -> dict[str, Any]:
        """Show how the policy treats a (synthetic) sample. Nothing reaches a model."""
        ctx = engine.context(
            auth.tenant,
            auth.brand,
            body.workload,
            correlation_id=context.current().request_id,
            requested_level=body.requested_level,
        )
        enforced = engine.enforce(body.text, ctx, body.destination)
        return {
            "provenance": Provenance.LIVE,
            "classification": summarise(enforced.result),
            # What the destination would receive (the caller's own text, never logged).
            "forwarded_text": enforced.text,
            "blocked": enforced.blocked,
        }

    @app.post("/api/chat")
    async def chat(
        body: ChatRequest,
        auth: AuthContext = Depends(require_scope(Workload.LLM)),  # noqa: B008
    ) -> StreamingResponse:
        messages = [ChatMessage(role=m.role, content=m.content) for m in body.messages]
        events = await pipeline.start_chat(messages, scope_of(auth), body.classification)
        return StreamingResponse(
            events,
            media_type="text/event-stream",
            headers={"x-triaxis-mode": pipeline.mode},
        )

    @app.post("/api/stt")
    async def stt(
        request: Request,
        language: Annotated[Literal["en", "ar"], Query()] = "en",
        analytics: Annotated[bool, Query()] = False,
        auth: AuthContext = Depends(require_scope(Workload.STT)),  # noqa: B008
    ) -> dict[str, Any]:
        data = await request.body()  # raw body: no multipart temp-file spooling
        scope = scope_of(auth)
        try:
            text, audio_res, transcript_res, info = await pipeline.transcribe(data, language, scope)
            analysis: dict[str, Any] | None = None
            if analytics:
                if not auth.principal.allows(auth.tenant, auth.brand, Workload.LLM):
                    raise TriaxisError(ErrorCode.INSUFFICIENT_SCOPE)
                analysis = await pipeline.analyse_transcript(text, transcript_res, scope)
        finally:
            del data  # audio is never kept beyond the request
        return {
            "text": text,
            "language": language,
            **info,
            "classification": {
                "audio": summarise(audio_res),
                "transcript": summarise(transcript_res),
            },
            "analytics": analysis,
            "provenance": {
                "transcript": Provenance.SIMULATED if pipeline.mode == "mock" else Provenance.LIVE,
                "validation": Provenance.LIVE,
                "classification": Provenance.LIVE,
            },
        }

    @app.get("/api/stt/sample")
    async def stt_sample() -> Response:
        return Response(MockTts.sample_wav(), media_type="audio/wav")

    @app.post("/api/tts")
    async def tts(
        body: SpeechRequest,
        auth: AuthContext = Depends(require_scope(Workload.TTS)),  # noqa: B008
    ) -> Response:
        audio, media_type, result = await pipeline.synthesise(
            body.input, body.voice, scope_of(auth)
        )
        return Response(
            audio,
            media_type=media_type,
            headers={"x-triaxis-mode": pipeline.mode, **decision_headers(result)},
        )

    @app.post("/api/voice/turn")
    async def voice_turn(
        request: Request,
        language: Annotated[Literal["en", "ar"], Query()] = "en",
        voice: Annotated[Literal["warm", "deep"], Query()] = "warm",
        auth: AuthContext = Depends(require_scope(Workload.VOICE)),  # noqa: B008
    ) -> dict[str, Any]:
        data = await request.body()
        try:
            return await pipeline.voice_turn(data, language, voice, scope_of(auth))
        finally:
            del data

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
