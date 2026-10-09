from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support.logs import LogCapture
from tests.support.tokens import AUDIENCE, ISSUER, TokenFactory
from triaxis_common import logging as tlog
from triaxis_common.auth import StaticKeyProvider, TokenValidator


@pytest.fixture(scope="session")
def tokens() -> TokenFactory:
    return TokenFactory()


@pytest.fixture
def validator(tokens: TokenFactory) -> TokenValidator:
    return TokenValidator(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeyProvider(tokens.jwks()))


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    """Capture log output of this test (the session schema gate still sees it)."""
    capture = LogCapture()
    previous = tlog._state.sink
    tlog._state.sink = capture
    try:
        yield capture
    finally:
        tlog._state.sink = previous
