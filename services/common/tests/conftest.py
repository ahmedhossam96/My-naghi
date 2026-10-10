from __future__ import annotations

import pytest

from tests.support.tokens import AUDIENCE, ISSUER, TokenFactory
from triaxis_common.auth import StaticKeyProvider, TokenValidator


@pytest.fixture(scope="session")
def tokens() -> TokenFactory:
    return TokenFactory()


@pytest.fixture
def validator(tokens: TokenFactory) -> TokenValidator:
    return TokenValidator(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeyProvider(tokens.jwks()))
