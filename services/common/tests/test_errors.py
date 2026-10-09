from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from triaxis_common.errors import (
    CATALOG,
    AuthError,
    ErrorCode,
    ErrorResponse,
    LimitExceededError,
    ServiceUnavailableError,
    TriaxisError,
    code_for_status,
)


def test_every_code_has_a_catalogue_entry() -> None:
    assert set(CATALOG) == set(ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode))
def test_catalogue_entries_are_sane(code: ErrorCode) -> None:
    spec = CATALOG[code]
    assert 400 <= spec.status <= 599
    assert spec.message.endswith(".")
    assert "{" not in spec.message  # no interpolation placeholders


def test_response_shape() -> None:
    body = ErrorResponse.for_code(ErrorCode.TOKEN_EXPIRED, request_id="req-12345678")
    assert body.model_dump(mode="json") == {
        "error": {
            "type": "authentication_error",
            "code": "token_expired",
            "message": "The access token has expired.",
            "request_id": "req-12345678",
        }
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.INVALID_REQUEST),
        (401, ErrorCode.INVALID_TOKEN),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (418, ErrorCode.INVALID_REQUEST),
        (422, ErrorCode.INVALID_REQUEST),
        (500, ErrorCode.INTERNAL_ERROR),
        (502, ErrorCode.INTERNAL_ERROR),
        (504, ErrorCode.TIMEOUT),
    ],
)
def test_code_for_status(status: int, code: ErrorCode) -> None:
    assert code_for_status(status) is code


def test_exception_defaults_and_overrides() -> None:
    assert AuthError().code is ErrorCode.INVALID_TOKEN
    assert AuthError().status == 401
    assert LimitExceededError().code is ErrorCode.PAYLOAD_TOO_LARGE
    assert ServiceUnavailableError().status == 503
    err = TriaxisError(ErrorCode.RATE_LIMITED, retry_after_s=5)
    assert err.retry_after_s == 5
    assert str(err) == CATALOG[ErrorCode.RATE_LIMITED].message


@given(code=st.sampled_from(list(ErrorCode)), content=st.text(min_size=8))
def test_property_errors_never_contain_request_content(code: ErrorCode, content: str) -> None:
    """There is no API to put request content into an error."""
    body = ErrorResponse.for_code(code).model_dump_json()
    exc = TriaxisError(code)
    if content not in CATALOG[code].message:
        assert content not in body
        assert content not in str(exc)
