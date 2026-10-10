"""Shared constrained types and enums.

These types are the vocabulary of the allow-list schemas (ADR 0007) and of the
scope model (ADR 0008). Anything that can appear in a log record, a span
attribute, a metric label or a token scope is defined here.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"
SLUG_RE = re.compile(SLUG_PATTERN)
REQUEST_ID_PATTERN = r"^[A-Za-z0-9-]{8,64}$"
REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
LOGGER_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$"
MODEL_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"

# Lowercase ASCII slug, 1-32 chars, no leading/trailing hyphen. Tenant, brand, site.
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, strict=True)]
RequestId = Annotated[str, StringConstraints(pattern=REQUEST_ID_PATTERN, strict=True)]
# Python identifier, e.g. an exception class name.
Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN, strict=True)]
LoggerName = Annotated[str, StringConstraints(pattern=LOGGER_NAME_PATTERN, strict=True)]
ModelId = Annotated[str, StringConstraints(pattern=MODEL_ID_PATTERN, strict=True)]
# e.g. "default@2026-10-09.1"; slug-like, never free text.
POLICY_VERSION_PATTERN = r"^[a-z0-9][a-z0-9._@-]{0,79}$"
PolicyVersion = Annotated[str, StringConstraints(pattern=POLICY_VERSION_PATTERN, strict=True)]


class Workload(StrEnum):
    """What a token scope grants. Part of ``tenant:brand:workload``."""

    STT = "stt"
    TTS = "tts"
    LLM = "llm"
    VOICE = "voice"
    BATCH = "batch"


class Lane(StrEnum):
    """Router lanes. Real-time always has priority over batch."""

    REALTIME = "realtime"
    BATCH = "batch"


class Environment(StrEnum):
    DEV = "dev"
    CI = "ci"
    STAGING = "staging"
    PROD = "prod"

    @property
    def is_production_like(self) -> bool:
        """Staging and production enforce every runtime guard (fail closed)."""
        return self in (Environment.STAGING, Environment.PROD)


def is_slug(value: str) -> bool:
    return SLUG_RE.fullmatch(value) is not None


def is_request_id(value: str) -> bool:
    return REQUEST_ID_RE.fullmatch(value) is not None
