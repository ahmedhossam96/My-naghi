"""Allow-list tracing and metrics (ADR 0007).

- :class:`AllowListSpanExporter` wraps the real exporter and strips every span
  attribute that is not allow-listed, every event attribute except
  ``exception.type`` and every status description, before anything leaves the
  process.
- :class:`LatencyMetrics` records the latency histograms named in
  ``docs/slo.md`` with labels validated by :class:`MetricLabels`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Final

from opentelemetry import trace
from opentelemetry.metrics import Histogram, Meter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event as SpanEvent
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Status
from opentelemetry.util.types import AttributeValue
from pydantic import BaseModel, ConfigDict

from triaxis_common.config import require_in_cluster_url
from triaxis_common.logging import Stage
from triaxis_common.types import Lane, ModelId, Slug, Workload

ALLOWED_SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "triaxis.request_id",
        "triaxis.tenant",
        "triaxis.brand",
        "triaxis.workload",
        "triaxis.lane",
        "triaxis.site",
        "triaxis.stage",
        "triaxis.model",
        "triaxis.bytes_in",
        "triaxis.bytes_out",
        "triaxis.audio_ms",
        "triaxis.tokens_in",
        "triaxis.tokens_out",
        "triaxis.error_code",
        "error.type",
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "network.protocol.version",
        "rpc.system",
    }
)
ALLOWED_EVENT_ATTRIBUTES: Final[frozenset[str]] = frozenset({"exception.type"})


def filter_attributes(
    attributes: Mapping[str, AttributeValue] | None, allowed: frozenset[str]
) -> dict[str, AttributeValue]:
    if not attributes:
        return {}
    return {k: v for k, v in attributes.items() if k in allowed}


def _sanitise(span: ReadableSpan) -> ReadableSpan:
    events = [
        SpanEvent(
            name=event.name if event.name == "exception" else "event",
            attributes=filter_attributes(event.attributes, ALLOWED_EVENT_ATTRIBUTES),
            timestamp=event.timestamp,
        )
        for event in span.events
    ]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=filter_attributes(span.attributes, ALLOWED_SPAN_ATTRIBUTES),
        events=events,
        links=[],  # link attributes are unconstrained; drop links entirely
        kind=span.kind,
        status=Status(span.status.status_code),  # drop description (may carry a message)
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class AllowListSpanExporter(SpanExporter):
    """Sanitises spans, then delegates to the real exporter."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([_sanitise(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def build_tracer_provider(
    service_name: str,
    *,
    exporter: SpanExporter | None = None,
    otlp_endpoint: str | None = None,
) -> TracerProvider:
    """Create a provider whose only exporter is wrapped by the allow-list.

    ``otlp_endpoint`` must be in-cluster (data residency). With neither an
    exporter nor an endpoint, spans are created (for timings) but not exported.
    """
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is None and otlp_endpoint is not None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=require_in_cluster_url(otlp_endpoint, allow_http=True))
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(AllowListSpanExporter(exporter)))
    return provider


@contextmanager
def stage_span(tracer: trace.Tracer, stage: Stage, **attributes: AttributeValue) -> Iterator[Span]:
    """Span for a pipeline stage; only allow-listed attributes are set."""
    attrs = filter_attributes(
        {f"triaxis.{k}": v for k, v in attributes.items()}, ALLOWED_SPAN_ATTRIBUTES
    )
    attrs["triaxis.stage"] = stage.value
    with tracer.start_as_current_span(stage.value, attributes=attrs) as span:
        yield span


# --------------------------------------------------------------------- metrics


class LatencyMetric(StrEnum):
    """Histograms required by docs/slo.md (milliseconds)."""

    TTFT = "ttft_ms"
    TIME_TO_FIRST_AUDIO = "time_to_first_audio_ms"
    STT_PARTIAL_LATENCY = "stt_partial_latency_ms"
    VOICE_TO_VOICE = "voice_to_voice_ms"
    GATEWAY_OVERHEAD = "gateway_overhead_ms"
    REQUEST_DURATION = "request_duration_ms"


class StatusClass(StrEnum):
    OK = "2xx"
    CLIENT_ERROR = "4xx"
    SERVER_ERROR = "5xx"


class MetricLabels(BaseModel):
    """The only label names a Triaxis metric may carry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant: Slug | None = None
    brand: Slug | None = None
    workload: Workload | None = None
    lane: Lane | None = None
    site: Slug | None = None
    status_class: StatusClass | None = None
    model: ModelId | None = None

    def as_attributes(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.model_dump(exclude_none=True, mode="json").items()}


class LatencyMetrics:
    def __init__(self, meter: Meter) -> None:
        self._histograms: dict[LatencyMetric, Histogram] = {
            metric: meter.create_histogram(metric.value, unit="ms") for metric in LatencyMetric
        }

    def observe(self, metric: LatencyMetric, value_ms: float, labels: MetricLabels) -> None:
        if value_ms < 0:
            raise ValueError("latency must be non-negative")
        self._histograms[metric].record(value_ms, attributes=labels.as_attributes())
