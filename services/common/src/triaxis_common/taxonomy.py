"""Data-classification vocabulary (ADR 0020).

Dependency-free enums shared by the classification engine, the policy engine,
the allow-list logger and the backup tooling. Everything here is safe to log:
these are labels and codes, never content.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Classification(StrEnum):
    """Ordered sensitivity levels. Comparisons follow the order below."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        return _RANK[self]

    # StrEnum inherits str's alphabetical comparisons; order by rank instead.
    # All four operators are defined explicitly (functools.total_ordering would
    # keep str's __gt__/__ge__/__le__).
    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Classification):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Classification):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Classification):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Classification):
            return NotImplemented
        return self.rank >= other.rank

    __hash__ = StrEnum.__hash__

    @classmethod
    def highest(cls, *levels: Classification) -> Classification:
        return max(levels, key=lambda lvl: lvl.rank) if levels else cls.PUBLIC


_RANK: Final = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.RESTRICTED: 3,
}


class DataTag(StrEnum):
    """What kind of sensitive data was detected or declared."""

    NATIONAL_ID = "national_id"  # Saudi national ID / Iqama
    IBAN = "iban"
    PAYMENT_CARD = "payment_card"
    PHONE_NUMBER = "phone_number"
    EMAIL_ADDRESS = "email_address"
    VEHICLE_ID = "vehicle_id"  # VIN
    CREDENTIAL = "credential"  # passwords, keys, tokens, private keys
    CUSTOMER_RECORD = "customer_record"  # declared by schema/field rules
    VOICE_RECORDING = "voice_recording"  # audio of a person speaking


class PrivacyTag(StrEnum):
    """Privacy characteristics, kept separate from the sensitivity level."""

    PERSONAL_DATA = "personal_data"
    SENSITIVE_PERSONAL_DATA = "sensitive_personal_data"
    CREDENTIALS_SECRETS = "credentials_secrets"
    REGULATED_FINANCIAL = "regulated_financial"


class Decision(StrEnum):
    """Policy outcome, in increasing order of strictness."""

    ALLOW = "allow"
    REDACT = "redact"
    RESTRICT = "restrict"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return _DECISION_RANK[self]


_DECISION_RANK: Final = {
    Decision.ALLOW: 0,
    Decision.REDACT: 1,
    Decision.RESTRICT: 2,
    Decision.BLOCK: 3,
}


class Destination(StrEnum):
    """Where data is about to go. Policies are evaluated per destination."""

    INFERENCE = "inference"  # a model (LLM/STT/TTS)
    CLIENT_RESPONSE = "client_response"  # back to the authenticated caller
    PERSISTENCE = "persistence"  # an authorised business-record store
    EXPORT = "export"  # leaves Triaxis (download, integration)
    BACKUP = "backup"


class Control(StrEnum):
    """Controls a decision requires downstream components to apply."""

    APPROVED_INFERENCE_ONLY = "approved_inference_only"
    ENCRYPT_AT_REST = "encrypt_at_rest"
    NO_PERSISTENCE = "no_persistence"
    NO_EXPORT = "no_export"
    NO_TELEMETRY_CONTENT = "no_telemetry_content"
    REDACT_BEFORE_INFERENCE = "redact_before_inference"


class ReasonCode(StrEnum):
    """Audit-safe explanation codes. Never contain content."""

    BASELINE_LEVEL = "baseline_level"
    CLIENT_RAISED_LEVEL = "client_raised_level"
    FIELD_RULE = "field_rule"
    DETECTED_NATIONAL_ID = "detected_national_id"
    DETECTED_IBAN = "detected_iban"
    DETECTED_PAYMENT_CARD = "detected_payment_card"
    DETECTED_PHONE_NUMBER = "detected_phone_number"
    DETECTED_EMAIL_ADDRESS = "detected_email_address"
    DETECTED_VEHICLE_ID = "detected_vehicle_id"
    DETECTED_CREDENTIAL = "detected_credential"
    PROPAGATED_FROM_SOURCE = "propagated_from_source"
    POLICY_ALLOW = "policy_allow"
    POLICY_REDACT = "policy_redact"
    POLICY_RESTRICT = "policy_restrict"
    POLICY_BLOCK = "policy_block"
    WORKLOAD_NOT_PERMITTED = "workload_not_permitted"
    DESTINATION_NOT_PERMITTED = "destination_not_permitted"
    POLICY_MISSING = "policy_missing"
    CLASSIFIER_ERROR = "classifier_error"


DETECTION_REASON: Final[dict[DataTag, ReasonCode]] = {
    DataTag.NATIONAL_ID: ReasonCode.DETECTED_NATIONAL_ID,
    DataTag.IBAN: ReasonCode.DETECTED_IBAN,
    DataTag.PAYMENT_CARD: ReasonCode.DETECTED_PAYMENT_CARD,
    DataTag.PHONE_NUMBER: ReasonCode.DETECTED_PHONE_NUMBER,
    DataTag.EMAIL_ADDRESS: ReasonCode.DETECTED_EMAIL_ADDRESS,
    DataTag.VEHICLE_ID: ReasonCode.DETECTED_VEHICLE_ID,
    DataTag.CREDENTIAL: ReasonCode.DETECTED_CREDENTIAL,
}


class InferenceMode(StrEnum):
    """How a service produces model output (ADR 0021)."""

    MOCK = "mock"  # deterministic stand-ins; never allowed in staging/prod
    REAL = "real"  # approved self-hosted model endpoint
