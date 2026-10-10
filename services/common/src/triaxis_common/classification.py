"""Deterministic data-classification engine (ADR 0020).

The engine classifies content **before** it reaches a model, a store or any
other processing boundary. It is deterministic and testable: no model is
consulted (restricted data is never sent to an LLM to ask whether it is safe).

Signals, combined by taking the highest level:

1. Baseline level for the workload, from the server-side policy.
2. Field-level rules for structured records.
3. Pattern detectors for identifiers and secrets (Saudi national ID / Iqama,
   IBAN, payment card, Saudi mobile number, e-mail, VIN, credentials).
4. A level propagated from the source (e.g. audio → transcript).
5. A level requested by the client, which can only **raise** the result.

The :class:`ClassificationResult` never contains the classified content. The
internal :class:`Detection` list (with character spans) is used only for
redaction and is never logged or serialised.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict

from triaxis_common.taxonomy import (
    DETECTION_REASON,
    Classification,
    Control,
    DataTag,
    Decision,
    PrivacyTag,
    ReasonCode,
)
from triaxis_common.types import PolicyVersion, RequestId, Slug, Workload

CLASSIFIER_VERSION: Final = "triaxis-classifier@1.0.0"

# --------------------------------------------------------------------- normalisation

# Characters an attacker can insert to split an identifier without changing how
# it reads: zero-width spaces/joiners, bidi controls, BOM, soft hyphen.
_IGNORABLE: Final = frozenset(
    chr(cp)
    for cp in (
        0x00AD,  # soft hyphen
        *range(0x200B, 0x2010),  # zero-width space/joiners, LRM/RLM
        *range(0x202A, 0x202F),  # bidi embeddings and overrides
        *range(0x2060, 0x2065),  # word joiner, invisible operators
        *range(0x2066, 0x206A),  # bidi isolates
        0xFEFF,  # byte-order mark / zero-width no-break space
    )
)
_DIGITS: Final = {
    **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # Extended Arabic-Indic (Persian)
}


def normalise(text: str) -> str:
    """Unicode NFKC normalisation, applied before classification and guardrails."""
    return unicodedata.normalize("NFKC", text)


def _skeleton(text: str) -> tuple[str, list[int]]:
    """Detection view of ``text``: ignorable characters removed, digits in ASCII.

    Returns the skeleton and, for each skeleton index, the index in ``text``.
    """
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch in _IGNORABLE:
            continue
        chars.append(_DIGITS.get(ch, ch))
        index.append(i)
    return "".join(chars), index


# --------------------------------------------------------------------- validators


def luhn_valid(digits: str) -> bool:
    if not digits.isdigit() or not digits:
        return False
    total = 0
    for pos, ch in enumerate(reversed(digits)):
        d = int(ch)
        if pos % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def saudi_id_checksum_valid(digits: str) -> bool:
    """De-facto Saudi national ID / Iqama check (ADR 0018, unverified spec).

    10 digits, first digit 1 (citizen) or 2 (resident); Luhn-style sum over all
    ten digits, doubling odd positions counted from the left.
    """
    if len(digits) != 10 or not digits.isdigit() or digits[0] not in "12":
        return False
    total = 0
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d // 10 + d % 10
        total += d
    return total % 10 == 0


def iban_valid(iban: str) -> bool:
    """ISO 13616 mod-97 check (country-specific lengths for SA only)."""
    candidate = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", candidate):
        return False
    if candidate.startswith("SA") and len(candidate) != 24:
        return False
    rearranged = candidate[4:] + candidate[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


# --------------------------------------------------------------------- detectors


@dataclass(frozen=True, slots=True)
class Detection:
    """One finding. Spans index into the normalised text. Never logged."""

    tag: DataTag
    start: int
    end: int
    detector: str
    confidence: float


@dataclass(frozen=True, slots=True)
class _Pattern:
    detector: str
    tag: DataTag
    regex: re.Pattern[str]
    validate: Callable[[str], float | None] = field(default=lambda _m: 1.0)


def _digits_only(s: str) -> str:
    return re.sub(r"[ \-]", "", s)


def _national_id(match: str) -> float:
    # Checksum-failing candidates are still reported (lower confidence) so a
    # wrong checksum assumption fails towards masking (ADR 0018).
    return 0.95 if saudi_id_checksum_valid(match) else 0.6


def _card(match: str) -> float | None:
    digits = _digits_only(match)
    return 0.95 if 13 <= len(digits) <= 19 and luhn_valid(digits) else None


def _iban(match: str) -> float | None:
    return 0.95 if iban_valid(match) else None


def _vin(match: str) -> float | None:
    letters = sum(ch.isalpha() for ch in match)
    digits = sum(ch.isdigit() for ch in match)
    return 0.7 if letters >= 3 and digits >= 3 else None


PATTERNS: Final[tuple[_Pattern, ...]] = (
    # Credentials and secrets first: they are the most sensitive.
    _Pattern(
        "pem_private_key",
        DataTag.CREDENTIAL,
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
        ),
    ),
    _Pattern("aws_access_key", DataTag.CREDENTIAL, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Pattern("github_token", DataTag.CREDENTIAL, re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    _Pattern("slack_token", DataTag.CREDENTIAL, re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    _Pattern("openbao_token", DataTag.CREDENTIAL, re.compile(r"\bhv[sbr]\.[A-Za-z0-9_-]{20,}\b")),
    _Pattern(
        "jwt",
        DataTag.CREDENTIAL,
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    _Pattern(
        "bearer_token", DataTag.CREDENTIAL, re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*")
    ),
    _Pattern(
        "password_assignment",
        DataTag.CREDENTIAL,
        re.compile(
            r"(?i)(?:\b(?:password|passwd|pwd|passcode|secret|api[_-]?key|access[_-]?key|"
            r"client[_-]?secret|token)\b|كلمة\s*(?:المرور|السر)|رمز\s*الدخول)"
            r"\s*(?:is|هي|:|=)\s*\S{4,}"
        ),
    ),
    # Financial identifiers.
    _Pattern(
        "iban", DataTag.IBAN, re.compile(r"(?i)\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"), _iban
    ),
    _Pattern(
        "payment_card",
        DataTag.PAYMENT_CARD,
        re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"),
        _card,
    ),
    # Personal identifiers.
    _Pattern(
        "saudi_national_id",
        DataTag.NATIONAL_ID,
        re.compile(r"(?<![\d])[12]\d{9}(?![\d])"),
        _national_id,
    ),
    _Pattern(
        "saudi_mobile",
        DataTag.PHONE_NUMBER,
        re.compile(r"(?<![\d+])(?:\+966|00966|0)\s?-?5\d(?:[ -]?\d){7}(?![\d])"),
    ),
    _Pattern(
        "email",
        DataTag.EMAIL_ADDRESS,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    ),
    _Pattern("vin", DataTag.VEHICLE_ID, re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), _vin),
)

# Default sensitivity and privacy characteristics of each tag.
TAG_LEVEL: Final[dict[DataTag, Classification]] = {
    DataTag.CREDENTIAL: Classification.RESTRICTED,
    DataTag.PAYMENT_CARD: Classification.RESTRICTED,
    DataTag.IBAN: Classification.RESTRICTED,
    DataTag.NATIONAL_ID: Classification.RESTRICTED,
    DataTag.PHONE_NUMBER: Classification.CONFIDENTIAL,
    DataTag.EMAIL_ADDRESS: Classification.CONFIDENTIAL,
    DataTag.VEHICLE_ID: Classification.CONFIDENTIAL,
    DataTag.CUSTOMER_RECORD: Classification.CONFIDENTIAL,
    DataTag.VOICE_RECORDING: Classification.CONFIDENTIAL,
}
TAG_PRIVACY: Final[dict[DataTag, frozenset[PrivacyTag]]] = {
    DataTag.CREDENTIAL: frozenset({PrivacyTag.CREDENTIALS_SECRETS}),
    DataTag.PAYMENT_CARD: frozenset({PrivacyTag.PERSONAL_DATA, PrivacyTag.REGULATED_FINANCIAL}),
    DataTag.IBAN: frozenset({PrivacyTag.PERSONAL_DATA, PrivacyTag.REGULATED_FINANCIAL}),
    DataTag.NATIONAL_ID: frozenset({PrivacyTag.PERSONAL_DATA, PrivacyTag.SENSITIVE_PERSONAL_DATA}),
    DataTag.PHONE_NUMBER: frozenset({PrivacyTag.PERSONAL_DATA}),
    DataTag.EMAIL_ADDRESS: frozenset({PrivacyTag.PERSONAL_DATA}),
    DataTag.VEHICLE_ID: frozenset({PrivacyTag.PERSONAL_DATA}),
    DataTag.CUSTOMER_RECORD: frozenset({PrivacyTag.PERSONAL_DATA}),
    DataTag.VOICE_RECORDING: frozenset({PrivacyTag.PERSONAL_DATA}),
}


def detect(text: str) -> list[Detection]:
    """Run every pattern detector over ``text`` (already normalised).

    Overlapping findings are resolved in favour of the earlier (more sensitive)
    pattern, so one span is reported once.
    """
    skeleton, index = _skeleton(text)
    found: list[Detection] = []
    taken: list[tuple[int, int]] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(skeleton):
            s, e = match.span()
            if s == e or any(s < te and ts < e for ts, te in taken):
                continue
            confidence = pattern.validate(match.group(0))
            if confidence is None:
                continue
            taken.append((s, e))
            found.append(
                Detection(pattern.tag, index[s], index[e - 1] + 1, pattern.detector, confidence)
            )
    return sorted(found, key=lambda d: d.start)


# --------------------------------------------------------------------- results


class ClassificationContext(BaseModel):
    """Server-side context. Tenant and brand come from the verified token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: Slug
    brand: Slug
    workload: Workload
    correlation_id: RequestId | None = None
    baseline: Classification = Classification.INTERNAL
    requested_level: Classification | None = None  # client may only raise
    source_level: Classification | None = None  # propagated (e.g. audio → transcript)
    source_tags: frozenset[DataTag] = frozenset()


class ClassificationResult(BaseModel):
    """Audit-safe result. Contains labels and codes only, never content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Classification
    data_tags: tuple[DataTag, ...]
    privacy_tags: tuple[PrivacyTag, ...]
    tag_counts: dict[DataTag, int]
    tenant: Slug
    brand: Slug
    workload: Workload
    decision: Decision = Decision.ALLOW
    required_controls: tuple[Control, ...] = ()
    reason_codes: tuple[ReasonCode, ...]
    classifier_version: str = CLASSIFIER_VERSION
    policy_version: PolicyVersion | None = None
    classified_at: AwareDatetime
    correlation_id: RequestId | None = None


@dataclass(frozen=True, slots=True)
class Classified:
    """Result plus internal detections (for redaction only)."""

    result: ClassificationResult
    detections: tuple[Detection, ...]
    text: str  # the normalised text the spans refer to


@dataclass(frozen=True, slots=True)
class FieldRule:
    """Schema/field-level sensitivity: a field name pattern and what it implies."""

    pattern: re.Pattern[str]
    level: Classification
    tags: frozenset[DataTag] = frozenset()


DEFAULT_FIELD_RULES: Final[tuple[FieldRule, ...]] = (
    FieldRule(
        re.compile(r"(?i)^(customer|client)_?(name|id|record)$"),
        Classification.CONFIDENTIAL,
        frozenset({DataTag.CUSTOMER_RECORD}),
    ),
    FieldRule(
        re.compile(r"(?i)(national_?id|iqama)"),
        Classification.RESTRICTED,
        frozenset({DataTag.NATIONAL_ID}),
    ),
    FieldRule(
        re.compile(r"(?i)(password|secret|api_?key|token)"),
        Classification.RESTRICTED,
        frozenset({DataTag.CREDENTIAL}),
    ),
    FieldRule(
        re.compile(r"(?i)(phone|mobile)"),
        Classification.CONFIDENTIAL,
        frozenset({DataTag.PHONE_NUMBER}),
    ),
    FieldRule(
        re.compile(r"(?i)^(vin|chassis_?number)$"),
        Classification.CONFIDENTIAL,
        frozenset({DataTag.VEHICLE_ID}),
    ),
    FieldRule(re.compile(r"(?i)(price|discount|margin|sales)"), Classification.CONFIDENTIAL),
)


class Classifier:
    def __init__(self, field_rules: Sequence[FieldRule] = DEFAULT_FIELD_RULES) -> None:
        self._field_rules = tuple(field_rules)

    def classify(self, text: str, ctx: ClassificationContext) -> Classified:
        normalised = normalise(text)
        detections = tuple(detect(normalised))
        return Classified(self._result(ctx, detections, set()), detections, normalised)

    def classify_record(
        self, fields: Mapping[str, str], ctx: ClassificationContext
    ) -> dict[str, Classified]:
        """Field-level classification for structured records."""
        out: dict[str, Classified] = {}
        for name, value in fields.items():
            normalised = normalise(value)
            detections = tuple(detect(normalised))
            extra: set[tuple[Classification, frozenset[DataTag]]] = {
                (rule.level, rule.tags) for rule in self._field_rules if rule.pattern.search(name)
            }
            out[name] = Classified(self._result(ctx, detections, extra), detections, normalised)
        return out

    @staticmethod
    def _result(
        ctx: ClassificationContext,
        detections: Sequence[Detection],
        field_rules: set[tuple[Classification, frozenset[DataTag]]],
    ) -> ClassificationResult:
        reasons: list[ReasonCode] = [ReasonCode.BASELINE_LEVEL]
        levels = [ctx.baseline]
        tags: dict[DataTag, int] = {}
        for det in detections:
            tags[det.tag] = tags.get(det.tag, 0) + 1
            levels.append(TAG_LEVEL[det.tag])
            reasons.append(DETECTION_REASON[det.tag])
        for level, rule_tags in field_rules:
            levels.append(level)
            reasons.append(ReasonCode.FIELD_RULE)
            for tag in rule_tags:
                tags.setdefault(tag, 0)
        for tag in ctx.source_tags:
            tags.setdefault(tag, 0)
            levels.append(TAG_LEVEL[tag])
        if ctx.source_level is not None:
            levels.append(ctx.source_level)
            reasons.append(ReasonCode.PROPAGATED_FROM_SOURCE)
        if ctx.requested_level is not None and ctx.requested_level > Classification.highest(
            *levels
        ):
            reasons.append(ReasonCode.CLIENT_RAISED_LEVEL)
        if ctx.requested_level is not None:
            levels.append(ctx.requested_level)  # can raise, never lower: max() below
        privacy: set[PrivacyTag] = set()
        for tag in tags:
            privacy |= TAG_PRIVACY[tag]
        return ClassificationResult(
            level=Classification.highest(*levels),
            data_tags=tuple(sorted(tags)),
            privacy_tags=tuple(sorted(privacy)),
            tag_counts=dict(sorted(tags.items())),
            tenant=ctx.tenant,
            brand=ctx.brand,
            workload=ctx.workload,
            reason_codes=tuple(dict.fromkeys(reasons)),
            classified_at=datetime.now(UTC),
            correlation_id=ctx.correlation_id,
        )


REDACTION_TOKEN: Final[dict[DataTag, str]] = {tag: f"[{tag.value.upper()}]" for tag in DataTag}


def redact(text: str, detections: Sequence[Detection], tags: frozenset[DataTag] | None) -> str:
    """Replace detected spans (optionally only some tags) with ``[TAG]`` tokens."""
    out: list[str] = []
    cursor = 0
    for det in sorted(detections, key=lambda d: d.start):
        if tags is not None and det.tag not in tags:
            continue
        if det.start < cursor:
            continue
        out.append(text[cursor : det.start])
        out.append(REDACTION_TOKEN[det.tag])
        cursor = det.end
    out.append(text[cursor:])
    return "".join(out)
