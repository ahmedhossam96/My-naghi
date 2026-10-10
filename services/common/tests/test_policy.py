from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.support import synthetic as syn
from tests.support.logs import LogCapture
from triaxis_common.classification import Classifier
from triaxis_common.errors import ErrorCode, TriaxisError
from triaxis_common.logging import AllowListLogger
from triaxis_common.policy import (
    Enforced,
    HandlingPolicy,
    PolicyEngine,
    load_policies,
    load_policy,
    summarise,
)
from triaxis_common.taxonomy import (
    Classification,
    Control,
    DataTag,
    Decision,
    Destination,
    ReasonCode,
)
from triaxis_common.types import Workload

REPO = Path(__file__).resolve().parents[3]
POLICIES = load_policies(REPO / "config/policies")


def engine(logger: AllowListLogger | None = None, **overrides: Any) -> PolicyEngine:
    policies = dict(POLICIES)
    if overrides:
        data = POLICIES["default"].model_dump(mode="json")
        data.update(overrides, policy_id="custom")
        policies["custom"] = HandlingPolicy.model_validate(data)
    tenant_map = {"mynaghi": "default", "strictco": "strict", "custom": "custom"}
    return PolicyEngine(policies, tenant_map, logger=logger)


def run(
    text: str,
    *,
    tenant: str = "mynaghi",
    brand: str = "bmw",
    workload: Workload = Workload.LLM,
    destination: Destination = Destination.INFERENCE,
    eng: PolicyEngine | None = None,
    **ctx_kw: Any,
) -> Enforced:
    e = eng or engine()
    ctx = e.context(tenant, brand, workload, **ctx_kw)
    return e.enforce(text, ctx, destination)


def test_repository_policies_load() -> None:
    assert set(POLICIES) == {"default", "strict"}
    assert POLICIES["default"].version_id == "default@2026-10-09.1"


def test_public_input_allowed_unchanged() -> None:
    out = run("When does the Jeddah showroom open?")
    assert out.text == "When does the Jeddah showroom open?"
    assert out.result.decision is Decision.ALLOW
    assert out.result.policy_version == "default@2026-10-09.1"


def test_confidential_allowed_with_controls() -> None:
    out = run("Please email me at sara.test@example.sa")
    assert out.result.level is Classification.CONFIDENTIAL
    assert out.result.decision is Decision.ALLOW
    assert out.text is not None
    assert "sara.test@example.sa" in out.text
    assert Control.APPROVED_INFERENCE_ONLY in out.result.required_controls


def test_restricted_identifiers_redacted_before_inference() -> None:
    ident, iban = syn.saudi_id(), syn.saudi_iban()
    out = run(f"My ID is {ident} and IBAN {iban}, book a service")
    assert out.result.decision is Decision.REDACT
    assert out.text == "My ID is [NATIONAL_ID] and IBAN [IBAN], book a service"
    assert Control.REDACT_BEFORE_INFERENCE in out.result.required_controls


def test_credentials_blocked_before_inference() -> None:
    out = run(f"here is my key {syn.aws_key()}")
    assert out.blocked
    assert out.text is None
    assert out.result.decision is Decision.BLOCK
    assert ReasonCode.POLICY_BLOCK in out.result.reason_codes


def test_restricted_data_never_enters_batch_analytics() -> None:
    out = run(f"card {syn.card()}", workload=Workload.BATCH)
    assert out.blocked
    assert ReasonCode.WORKLOAD_NOT_PERMITTED in out.result.reason_codes


@pytest.mark.parametrize(
    ("destination", "text", "decision"),
    [
        (Destination.PERSISTENCE, "mail a@b.sa", Decision.RESTRICT),
        (Destination.PERSISTENCE, f"card {syn.card()}", Decision.BLOCK),
        (Destination.EXPORT, "mail a@b.sa", Decision.RESTRICT),
        (Destination.BACKUP, "mail a@b.sa", Decision.BLOCK),
        (Destination.BACKUP, "showroom hours", Decision.ALLOW),
        (Destination.CLIENT_RESPONSE, f"id {syn.saudi_id()}", Decision.REDACT),
        (Destination.CLIENT_RESPONSE, f"pwd={'x' * 8}", Decision.REDACT),
    ],
)
def test_destination_rules(destination: Destination, text: str, decision: Decision) -> None:
    assert run(text, destination=destination).result.decision is decision


def test_tenant_policy_difference() -> None:
    text = "call me on 0551234567"
    assert run(text).result.decision is Decision.ALLOW
    strict = run(text, tenant="strictco")
    assert strict.result.decision is Decision.REDACT
    assert strict.text == "call me on [PHONE_NUMBER]"
    assert run(f"id {syn.saudi_id()}", tenant="strictco").blocked


def test_brand_override_difference() -> None:
    eng = engine(brands={"mini": {"tags": {"inference": {"vehicle_id": "block"}}}})
    text = "VIN WBA3A5C51CF256985 needs service"
    assert run(text, tenant="custom", brand="bmw", eng=eng).result.decision is Decision.ALLOW
    assert run(text, tenant="custom", brand="mini", eng=eng).blocked


def test_level_wide_redaction_without_tag_rule() -> None:
    eng = engine(tags={})
    out = run(f"card {syn.card()}", tenant="custom", eng=eng)
    assert out.result.decision is Decision.REDACT
    assert out.text == "card [PAYMENT_CARD]"


def test_client_cannot_downgrade() -> None:
    out = run(f"card {syn.card()}", requested_level=Classification.PUBLIC)
    assert out.result.level is Classification.RESTRICTED
    assert out.result.decision is Decision.REDACT


def test_audio_to_transcript_to_analytics_propagation() -> None:
    eng = engine()
    audio = eng.enforce("", eng.context("mynaghi", "bmw", Workload.STT), Destination.INFERENCE)
    assert audio.result.level is Classification.CONFIDENTIAL  # speech baseline
    transcript = eng.enforce(
        f"my card is {syn.card()}",
        eng.context("mynaghi", "bmw", Workload.STT, source=audio.result),
        Destination.CLIENT_RESPONSE,
    )
    assert transcript.result.level is Classification.RESTRICTED
    analytics = eng.enforce(
        transcript.text or "",
        eng.context("mynaghi", "bmw", Workload.BATCH, source=transcript.result),
        Destination.INFERENCE,
    )
    # The transcript was redacted for the response, but its restricted origin
    # still applies: restricted data is not permitted into batch analytics.
    assert analytics.blocked
    assert ReasonCode.PROPAGATED_FROM_SOURCE in analytics.result.reason_codes


def test_missing_policy_fails_closed() -> None:
    eng = PolicyEngine(POLICIES, {"mynaghi": "does-not-exist"})
    with pytest.raises(TriaxisError) as excinfo:
        eng.context("mynaghi", "bmw", Workload.LLM)
    assert excinfo.value.code is ErrorCode.CLASSIFICATION_UNAVAILABLE
    with pytest.raises(TriaxisError):
        PolicyEngine(POLICIES, {}).policy_for("unknown-tenant")


def test_engine_error_fails_closed_and_is_audited(logs: LogCapture) -> None:
    class Broken(Classifier):
        def classify(self, text: str, ctx: Any) -> Any:  # noqa: ARG002
            raise ValueError("boom")

    eng = PolicyEngine(POLICIES, {"mynaghi": "default"}, Broken(), AllowListLogger("guardrails"))
    with pytest.raises(TriaxisError) as excinfo:
        eng.enforce("x", eng.context("mynaghi", "bmw", Workload.LLM), Destination.INFERENCE)
    assert excinfo.value.code is ErrorCode.CLASSIFICATION_UNAVAILABLE
    (record,) = logs.records
    assert record["reason_codes"] == ["classifier_error"]
    assert record["decision"] == "block"


def test_policy_error_during_decide_is_audited(logs: LogCapture) -> None:
    eng = PolicyEngine(POLICIES, {"mynaghi": "default"}, logger=AllowListLogger("guardrails"))
    ctx = eng.context("mynaghi", "bmw", Workload.LLM)
    eng._tenant_policy.clear()  # policy disappears between context and decision
    with pytest.raises(TriaxisError):
        eng.enforce("x", ctx, Destination.INFERENCE)
    assert logs.records[-1]["error_code"] == "classification_unavailable"


def test_audit_record_is_metadata_only(logs: LogCapture) -> None:
    secret_text = f"SENTINEL-7781 my card {syn.card()}"
    run(secret_text, eng=engine(AllowListLogger("guardrails")))
    (record,) = logs.records
    assert record["event"] == "classification_decision"
    assert record["classification"] == "restricted"
    assert record["decision"] == "redact"
    assert record["data_tags"] == ["payment_card"]
    assert "SENTINEL-7781" not in logs.text()
    assert syn.card() not in logs.text()


def test_summarise_has_no_content() -> None:
    out = run(f"SENTINEL-55 id {syn.saudi_id()}")
    view = summarise(out.result)
    assert view["decision"] == "redact"
    assert "SENTINEL-55" not in str(view)


# --------------------------------------------------------------------- policy files


def test_policy_must_define_every_level_and_workload(tmp_path: Path) -> None:
    data = POLICIES["default"].model_dump(mode="json")
    del data["levels"]["inference"]["restricted"]
    with pytest.raises(ValidationError, match=r"levels\.inference"):
        HandlingPolicy.model_validate(data)
    data = POLICIES["default"].model_dump(mode="json")
    del data["baseline"]["voice"]
    with pytest.raises(ValidationError, match="baseline"):
        HandlingPolicy.model_validate(data)
    data = POLICIES["default"].model_dump(mode="json")
    data["levels"]["inference"]["restricted"] = "maybe"
    with pytest.raises(ValidationError):
        HandlingPolicy.model_validate(data)


def test_policy_file_name_must_match(tmp_path: Path) -> None:
    data = POLICIES["default"].model_dump(mode="json")
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(data), "utf-8")
    assert load_policy(tmp_path / "other.yaml").policy_id == "default"
    with pytest.raises(ValueError, match="file name"):
        load_policies(tmp_path)


def test_tag_rule_unknown_tag_rejected() -> None:
    data = POLICIES["default"].model_dump(mode="json")
    data["tags"]["inference"]["secret_sauce"] = "block"
    with pytest.raises(ValidationError):
        HandlingPolicy.model_validate(data)


def test_data_tag_enum_covers_tag_levels() -> None:
    from triaxis_common.classification import TAG_LEVEL, TAG_PRIVACY

    assert set(TAG_LEVEL) == set(DataTag) == set(TAG_PRIVACY)
