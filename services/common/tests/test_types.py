from __future__ import annotations

import pytest

from triaxis_common import context
from triaxis_common.types import Environment, Lane, Workload, is_request_id, is_slug


@pytest.mark.parametrize("value", ["a", "mynaghi", "brand-a", "a1", "x" * 32])
def test_valid_slugs(value: str) -> None:
    assert is_slug(value)


@pytest.mark.parametrize(
    "value", ["", "-a", "a-", "A", "a_b", "a b", "x" * 33, "١", "toyota\n", "ميتسوبيشي"]
)
def test_invalid_slugs(value: str) -> None:
    assert not is_slug(value)


def test_request_ids() -> None:
    assert is_request_id("0123456789abcdef")
    assert not is_request_id("short")
    assert not is_request_id("x" * 65)


def test_production_like_environments() -> None:
    assert Environment.PROD.is_production_like
    assert Environment.STAGING.is_production_like
    assert not Environment.DEV.is_production_like
    assert not Environment.CI.is_production_like


def test_context_update_and_fields() -> None:
    token = context.set_context(context.RequestContext(request_id="req-00000042"))
    try:
        ctx = context.update_context(lane=Lane.REALTIME, workload=Workload.VOICE)
        assert ctx.as_fields() == {
            "request_id": "req-00000042",
            "workload": Workload.VOICE,
            "lane": Lane.REALTIME,
        }
        assert context.update_context() == ctx
    finally:
        context.reset_context(token)
    assert context.current().as_fields() == {}
