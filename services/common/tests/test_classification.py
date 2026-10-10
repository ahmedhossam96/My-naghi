from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.support import synthetic as syn
from triaxis_common.classification import (
    ClassificationContext,
    Classifier,
    detect,
    iban_valid,
    luhn_valid,
    normalise,
    redact,
    saudi_id_checksum_valid,
)
from triaxis_common.taxonomy import Classification, DataTag, PrivacyTag, ReasonCode
from triaxis_common.types import Workload

ZWSP = chr(0x200B)
RLO = chr(0x202E)


def ctx(**kw: object) -> ClassificationContext:
    base: dict[str, object] = {"tenant": "mynaghi", "brand": "bmw", "workload": Workload.LLM}
    base.update(kw)
    return ClassificationContext.model_validate(base)


def tags_in(text: str) -> set[DataTag]:
    return {d.tag for d in detect(normalise(text))}


# --------------------------------------------------------------------- validators


def test_synthetic_values_are_valid() -> None:
    assert luhn_valid(syn.card())
    assert saudi_id_checksum_valid(syn.saudi_id())
    assert saudi_id_checksum_valid(syn.saudi_id("2098765432"))
    assert iban_valid(syn.saudi_iban())


@given(st.from_regex(r"[12][0-9]{8}", fullmatch=True))
def test_property_saudi_id_exactly_one_check_digit(body: str) -> None:
    valid = [d for d in "0123456789" if saudi_id_checksum_valid(body + d)]
    assert len(valid) == 1


@given(st.text(max_size=12))
def test_property_saudi_id_rejects_non_ids(value: str) -> None:
    if not (len(value) == 10 and value.isascii() and value.isdigit() and value[0] in "12"):
        assert not saudi_id_checksum_valid(value)


@given(st.from_regex(r"[0-9]{12,18}", fullmatch=True))
def test_property_luhn_single_digit_error_detected(body: str) -> None:
    number = body + syn.luhn_check_digit(body)
    assert luhn_valid(number)
    pos = len(number) // 2
    wrong = number[:pos] + str((int(number[pos]) + 1) % 10) + number[pos + 1 :]
    assert not luhn_valid(wrong)


@given(st.from_regex(r"[0-9]{2}", fullmatch=True), st.from_regex(r"[0-9]{18}", fullmatch=True))
def test_property_iban_mod97(bank: str, account: str) -> None:
    iban = syn.saudi_iban(bank, account)
    assert iban_valid(iban)
    assert iban_valid(f"{iban[:4]} {iban[4:8]} {iban[8:]}")
    tampered = iban[:-1] + str((int(iban[-1]) + 1) % 10)
    assert not iban_valid(tampered)


@pytest.mark.parametrize("value", ["", "abc", "SA00", "SA03800000000608010167519", "XX" + "1" * 30])
def test_iban_invalid_shapes(value: str) -> None:
    assert not iban_valid(value)


def test_luhn_rejects_non_digits() -> None:
    assert not luhn_valid("")
    assert not luhn_valid("41111111111111a")


# --------------------------------------------------------------------- detectors


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        (f"My ID is {syn.saudi_id()}", DataTag.NATIONAL_ID),
        (f"رقم الهوية {syn.saudi_id()}", DataTag.NATIONAL_ID),
        (f"Iqama {syn.saudi_id('2098765432')}", DataTag.NATIONAL_ID),
        (f"Card {syn.card()}", DataTag.PAYMENT_CARD),
        ("Card 4111 1111 1111 1111", DataTag.PAYMENT_CARD),
        (f"IBAN {syn.saudi_iban()}", DataTag.IBAN),
        ("Call 0551234567", DataTag.PHONE_NUMBER),
        ("call +966 55 123 4567", DataTag.PHONE_NUMBER),
        ("mail ahmed.test@example.sa", DataTag.EMAIL_ADDRESS),
        ("VIN WBA3A5C51CF256985 needs service", DataTag.VEHICLE_ID),
        (f"key {syn.aws_key()}", DataTag.CREDENTIAL),
        (f"token {syn.github_token()}", DataTag.CREDENTIAL),
        (syn.pem_private_key(), DataTag.CREDENTIAL),
        ("password: Tr1axis-demo-only", DataTag.CREDENTIAL),
        ("كلمة المرور: abc12345", DataTag.CREDENTIAL),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", DataTag.CREDENTIAL),
        (syn.jwt_like(), DataTag.CREDENTIAL),
        (syn.openbao_token(), DataTag.CREDENTIAL),
    ],
)
def test_detects(text: str, tag: DataTag) -> None:
    assert tag in tags_in(text)


@pytest.mark.parametrize(
    "text",
    [
        "What time does the Riyadh showroom open on Saturday?",
        "السلام عليكم، متى يفتح المعرض؟",
        "Order 12345 is ready",
        "The 2026 model has 5 seats and 7 airbags",
        "Card 4111 1111 1111 1112",  # Luhn fails
        "SA0380000000608010167518",  # mod-97 fails
        "ABCDEFGHJKLMNPRST",  # 17 letters: not a VIN (no digits)
        "the password policy requires rotation",  # no assignment
    ],
)
def test_no_false_positive(text: str) -> None:
    assert tags_in(text) == set()


def test_unverified_id_checksum_still_flagged_with_lower_confidence() -> None:
    ident = syn.saudi_id()
    bad = ident[:-1] + str((int(ident[-1]) + 1) % 10)
    (det,) = detect(bad)
    assert det.tag is DataTag.NATIONAL_ID
    assert det.confidence < 0.9


def test_adversarial_arabic_indic_digits() -> None:
    assert DataTag.NATIONAL_ID in tags_in(f"هويتي {syn.to_arabic_indic(syn.saudi_id())}")
    assert DataTag.PAYMENT_CARD in tags_in(syn.to_arabic_indic(syn.card()))


def test_adversarial_zero_width_and_bidi_insertion() -> None:
    ident = syn.saudi_id()
    split = ZWSP.join(ident)
    assert DataTag.NATIONAL_ID in tags_in(f"id {split}")
    assert DataTag.PAYMENT_CARD in tags_in(RLO + syn.card())


def test_fullwidth_digits_normalised() -> None:
    fullwidth = "".join(chr(0xFF10 + int(d)) for d in syn.saudi_id())
    assert DataTag.NATIONAL_ID in tags_in(fullwidth)


# --------------------------------------------------------------------- results


def test_public_input_stays_at_baseline() -> None:
    res = Classifier().classify("When does the showroom open?", ctx()).result
    assert res.level is Classification.INTERNAL
    assert res.data_tags == ()
    assert res.reason_codes == (ReasonCode.BASELINE_LEVEL,)


def test_mixed_sensitivity_takes_highest_level_and_counts() -> None:
    text = f"Email me at a@b.sa or a2@b.sa, card {syn.card()}"
    res = Classifier().classify(text, ctx()).result
    assert res.level is Classification.RESTRICTED
    assert res.tag_counts == {DataTag.EMAIL_ADDRESS: 2, DataTag.PAYMENT_CARD: 1}
    assert PrivacyTag.REGULATED_FINANCIAL in res.privacy_tags
    assert ReasonCode.DETECTED_PAYMENT_CARD in res.reason_codes


def test_client_can_raise_but_never_lower() -> None:
    raised = Classifier().classify("hello", ctx(requested_level=Classification.CONFIDENTIAL))
    assert raised.result.level is Classification.CONFIDENTIAL
    assert ReasonCode.CLIENT_RAISED_LEVEL in raised.result.reason_codes
    lowered = Classifier().classify(
        f"card {syn.card()}", ctx(requested_level=Classification.PUBLIC)
    )
    assert lowered.result.level is Classification.RESTRICTED
    assert ReasonCode.CLIENT_RAISED_LEVEL not in lowered.result.reason_codes


def test_source_propagation() -> None:
    res = (
        Classifier()
        .classify(
            "plain words",
            ctx(
                source_level=Classification.CONFIDENTIAL,
                source_tags=frozenset({DataTag.VOICE_RECORDING}),
            ),
        )
        .result
    )
    assert res.level is Classification.CONFIDENTIAL
    assert DataTag.VOICE_RECORDING in res.data_tags
    assert ReasonCode.PROPAGATED_FROM_SOURCE in res.reason_codes


def test_record_field_rules() -> None:
    out = Classifier().classify_record(
        {"customer_name": "Sara", "national_id": "unknown", "notes": "ok", "price": "120000"},
        ctx(),
    )
    assert out["customer_name"].result.level is Classification.CONFIDENTIAL
    assert DataTag.CUSTOMER_RECORD in out["customer_name"].result.data_tags
    assert out["national_id"].result.level is Classification.RESTRICTED
    assert out["notes"].result.level is Classification.INTERNAL
    assert out["price"].result.level is Classification.CONFIDENTIAL


def test_result_never_contains_content() -> None:
    secret = syn.card()
    classified = Classifier().classify(f"pay with {secret} please", ctx())
    dumped = classified.result.model_dump_json()
    assert secret not in dumped
    assert "pay with" not in dumped


def test_classification_order_is_by_rank_not_alphabet() -> None:
    levels = list(Classification)
    for low, high in itertools.pairwise(levels):
        assert low < high
        assert low <= high
        assert high > low
        assert high >= low
    # Alphabetically "confidential" < "internal" < "public" < "restricted" would differ.
    assert Classification.INTERNAL < Classification.CONFIDENTIAL
    assert Classification.highest() is Classification.PUBLIC
    for op in ("__lt__", "__le__", "__gt__", "__ge__"):
        assert getattr(Classification.PUBLIC, op)(3) is NotImplemented


# --------------------------------------------------------------------- redaction


def test_redact_selected_tags_only() -> None:
    text = f"id {syn.saudi_id()} mail a@b.sa"
    detections = detect(text)
    out = redact(text, detections, frozenset({DataTag.NATIONAL_ID}))
    assert out == "id [NATIONAL_ID] mail a@b.sa"
    assert redact(text, detections, None) == "id [NATIONAL_ID] mail [EMAIL_ADDRESS]"


def test_redaction_maps_back_through_hidden_characters() -> None:
    text = f"id {ZWSP.join(syn.saudi_id())} end"
    out = redact(text, detect(text), None)
    assert out == "id [NATIONAL_ID] end"


@settings(max_examples=150, deadline=None)
@given(
    prefix=st.text(alphabet=st.characters(categories=("L", "Zs")), max_size=30),
    suffix=st.text(alphabet=st.characters(categories=("L", "Zs")), max_size=30),
    arabic=st.booleans(),
)
def test_property_redaction_removes_identifiers(prefix: str, suffix: str, arabic: bool) -> None:
    ident = syn.saudi_id()
    shown = syn.to_arabic_indic(ident) if arabic else ident
    text = normalise(f"{prefix} {shown} {suffix}")
    out = redact(text, detect(text), None)
    assert shown not in out
    assert ident not in out
