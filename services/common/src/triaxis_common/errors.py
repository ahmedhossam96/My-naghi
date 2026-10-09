"""Fixed error catalogue and the single error response shape (ADR 0016).

Every error a client can see is a :class:`ErrorCode`. Its HTTP status, error
``type`` and ``message`` come from :data:`CATALOG`; messages never interpolate
request data, so an error can never echo customer content.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from triaxis_common.types import RequestId


class ErrorType(StrEnum):
    INVALID_REQUEST = "invalid_request_error"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"
    RATE_LIMIT = "rate_limit_error"
    TIMEOUT = "timeout_error"
    UNAVAILABLE = "service_unavailable_error"
    INTERNAL = "internal_error"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    AUDIO_TOO_LONG = "audio_too_long"
    TOO_MANY_TOKENS = "too_many_tokens"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    BRAND_REQUIRED = "brand_required"
    MISSING_TOKEN = "missing_token"  # noqa: S105 - error code, not a secret
    INVALID_TOKEN = "invalid_token"  # noqa: S105 - error code, not a secret
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 - error code, not a secret
    INSUFFICIENT_SCOPE = "insufficient_scope"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    TIMEOUT = "timeout"
    NOT_READY = "not_ready"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    status: int
    type: ErrorType
    message: str


CATALOG: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.INVALID_REQUEST: ErrorSpec(
        400,
        ErrorType.INVALID_REQUEST,
        "The request is invalid. Check it against the API reference.",
    ),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(
        413, ErrorType.INVALID_REQUEST, "The request body exceeds the size limit."
    ),
    ErrorCode.AUDIO_TOO_LONG: ErrorSpec(
        400, ErrorType.INVALID_REQUEST, "The audio exceeds the duration limit."
    ),
    ErrorCode.TOO_MANY_TOKENS: ErrorSpec(
        400, ErrorType.INVALID_REQUEST, "The request exceeds the token limit."
    ),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: ErrorSpec(
        415, ErrorType.INVALID_REQUEST, "The media type or audio format is not supported."
    ),
    ErrorCode.BRAND_REQUIRED: ErrorSpec(
        400, ErrorType.INVALID_REQUEST, "The X-Triaxis-Brand header is required for this token."
    ),
    ErrorCode.MISSING_TOKEN: ErrorSpec(
        401, ErrorType.AUTHENTICATION, "A bearer access token is required."
    ),
    ErrorCode.INVALID_TOKEN: ErrorSpec(
        401, ErrorType.AUTHENTICATION, "The access token is invalid."
    ),
    ErrorCode.TOKEN_EXPIRED: ErrorSpec(
        401, ErrorType.AUTHENTICATION, "The access token has expired."
    ),
    ErrorCode.INSUFFICIENT_SCOPE: ErrorSpec(
        403, ErrorType.PERMISSION, "The access token does not grant this operation."
    ),
    ErrorCode.NOT_FOUND: ErrorSpec(404, ErrorType.NOT_FOUND, "The resource was not found."),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(
        405, ErrorType.INVALID_REQUEST, "The method is not allowed for this resource."
    ),
    ErrorCode.RATE_LIMITED: ErrorSpec(
        429, ErrorType.RATE_LIMIT, "Too many requests. Retry after the indicated delay."
    ),
    ErrorCode.QUOTA_EXCEEDED: ErrorSpec(
        429, ErrorType.RATE_LIMIT, "The quota for this brand and workload is exhausted."
    ),
    ErrorCode.TIMEOUT: ErrorSpec(504, ErrorType.TIMEOUT, "The request timed out."),
    ErrorCode.NOT_READY: ErrorSpec(
        503, ErrorType.UNAVAILABLE, "The service is starting and not ready yet."
    ),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(
        503, ErrorType.UNAVAILABLE, "The service is temporarily unavailable."
    ),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, ErrorType.INTERNAL, "An internal error occurred."),
}

_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.INVALID_TOKEN,
    403: ErrorCode.INSUFFICIENT_SCOPE,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.INVALID_REQUEST,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.TIMEOUT,
}


def code_for_status(status: int) -> ErrorCode:
    """Map an arbitrary HTTP status (e.g. from Starlette) to a catalogue code."""
    if status in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status]
    return ErrorCode.INVALID_REQUEST if 400 <= status < 500 else ErrorCode.INTERNAL_ERROR


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: ErrorType
    code: ErrorCode
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """The only error body Triaxis returns (OpenAI-compatible shape)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorDetail

    @classmethod
    def for_code(cls, code: ErrorCode, request_id: RequestId | None = None) -> ErrorResponse:
        spec = CATALOG[code]
        return cls(
            error=ErrorDetail(
                type=spec.type, code=code, message=spec.message, request_id=request_id
            )
        )


class TriaxisError(Exception):
    """Base for errors that map to a catalogue code.

    The exception carries only the code (and an optional ``Retry-After`` value),
    never request data. ``str(exc)`` is the fixed catalogue message.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, code: ErrorCode | None = None, *, retry_after_s: int | None = None) -> None:
        if code is not None:
            self.code = code
        self.retry_after_s = retry_after_s
        super().__init__(CATALOG[self.code].message)

    @property
    def status(self) -> int:
        return CATALOG[self.code].status


class AuthError(TriaxisError):
    code = ErrorCode.INVALID_TOKEN


class LimitExceededError(TriaxisError):
    code = ErrorCode.PAYLOAD_TOO_LARGE


class ServiceUnavailableError(TriaxisError):
    code = ErrorCode.SERVICE_UNAVAILABLE
