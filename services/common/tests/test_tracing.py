from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, StatusCode
from pydantic import ValidationError

from triaxis_common.logging import Stage
from triaxis_common.tracing import (
    ALLOWED_SPAN_ATTRIBUTES,
    AllowListSpanExporter,
    LatencyMetric,
    LatencyMetrics,
    MetricLabels,
    StatusClass,
    build_tracer_provider,
    filter_attributes,
    stage_span,
)
from triaxis_common.types import Lane, Workload

TAG = "ZR-TAG-span-1234"


def _provider() -> tuple[InMemorySpanExporter, object]:
    inner = InMemorySpanExporter()
    provider = build_tracer_provider("stt")
    provider.add_span_processor(SimpleSpanProcessor(AllowListSpanExporter(inner)))
    return inner, provider


def test_disallowed_attributes_events_and_status_are_stripped() -> None:
    inner, provider = _provider()
    tracer = provider.get_tracer("test")  # type: ignore[attr-defined]
    with tracer.start_as_current_span("other") as other:
        link_ctx = other.get_span_context()
    with tracer.start_as_current_span(
        "POST /v1/audio/transcriptions",
        attributes={
            "triaxis.tenant": "mynaghi",
            "http.route": "/v1/audio/transcriptions",
            "url.full": f"https://gw/v1/x?q={TAG}",
            "transcript": TAG,
        },
        links=[Link(link_ctx, {"note": TAG})],
    ) as span:
        span.add_event("partial", {"text": TAG})
        span.record_exception(ValueError(TAG))
        span.set_status(StatusCode.ERROR, f"failed: {TAG}")
    exported = inner.get_finished_spans()[-1]
    assert dict(exported.attributes or {}) == {
        "triaxis.tenant": "mynaghi",
        "http.route": "/v1/audio/transcriptions",
    }
    assert [e.name for e in exported.events] == ["event", "exception"]
    assert dict(exported.events[1].attributes or {}) == {"exception.type": "ValueError"}
    assert exported.status.status_code is StatusCode.ERROR
    assert exported.status.description is None
    assert not exported.links
    assert TAG not in repr(exported.to_json())


def test_uncaught_exception_in_span_is_sanitised() -> None:
    inner, provider = _provider()
    tracer = provider.get_tracer("test")  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError), tracer.start_as_current_span("llm"):
        raise RuntimeError(TAG)
    assert TAG not in inner.get_finished_spans()[0].to_json()


def test_stage_span_filters_attributes() -> None:
    inner, provider = _provider()
    tracer = provider.get_tracer("test")  # type: ignore[attr-defined]
    with stage_span(tracer, Stage.TTFT, tenant="mynaghi", prompt=TAG):
        pass
    span = inner.get_finished_spans()[0]
    assert span.name == "ttft"
    assert dict(span.attributes or {}) == {"triaxis.tenant": "mynaghi", "triaxis.stage": "ttft"}


def test_exporter_delegates_flush_and_shutdown() -> None:
    inner = InMemorySpanExporter()
    exporter = AllowListSpanExporter(inner)
    assert exporter.force_flush()
    exporter.shutdown()


def test_build_provider_with_exporter_and_otlp() -> None:
    build_tracer_provider("stt", exporter=InMemorySpanExporter()).shutdown()
    build_tracer_provider("stt", otlp_endpoint="http://otel-collector:4318/v1/traces").shutdown()
    with pytest.raises(ValueError, match="cluster"):
        build_tracer_provider("stt", otlp_endpoint="https://otel.vendor.com/v1/traces")


def test_filter_attributes_empty() -> None:
    assert filter_attributes(None, ALLOWED_SPAN_ATTRIBUTES) == {}


def test_latency_histograms_with_allow_listed_labels() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    metrics = LatencyMetrics(meter)
    labels = MetricLabels(
        tenant="mynaghi",
        brand="toyota",
        workload=Workload.VOICE,
        lane=Lane.REALTIME,
        status_class=StatusClass.OK,
        model="qwen3.5-35b-a3b",
    )
    metrics.observe(LatencyMetric.VOICE_TO_VOICE, 820.0, labels)
    metrics.observe(LatencyMetric.TTFT, 210.0, labels)
    data = reader.get_metrics_data()
    names = {
        m.name
        for rm in data.resource_metrics  # type: ignore[union-attr]
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert {"voice_to_voice_ms", "ttft_ms"} <= names
    with pytest.raises(ValueError, match="non-negative"):
        metrics.observe(LatencyMetric.TTFT, -1, labels)


def test_metric_labels_reject_unknown_names_and_free_text() -> None:
    with pytest.raises(ValidationError):
        MetricLabels(prompt=TAG)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        MetricLabels(tenant=TAG)
    assert MetricLabels(site="jeddah").as_attributes() == {"site": "jeddah"}
