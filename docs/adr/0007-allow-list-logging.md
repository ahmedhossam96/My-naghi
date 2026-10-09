# 0007 — Allow-list logging and telemetry

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Free-text logging is the most common way customer content leaks into storage.
Redaction (deny-listing) fails open; an allow-list fails closed.

## Decision

### Logs

A single Pydantic model, `triaxis_common.logging.LogRecord`, with
`extra="forbid"` and strict types. Allowed fields:

| Field | Type |
|---|---|
| `ts` | UTC timestamp |
| `level` | enum |
| `event` | enum (`Event`), never free text |
| `service`, `site` | slug |
| `source` | dotted logger name (code-defined) |
| `request_id` | `[A-Za-z0-9-]{8,64}` |
| `tenant`, `brand` | slug |
| `workload` | enum (`stt`, `tts`, `llm`, `voice`, `batch`) |
| `lane` | enum (`realtime`, `batch`) |
| `duration_ms` | float ≥ 0 |
| `timings` | map of `Stage` enum → float ms |
| `status` | HTTP status 100–599 |
| `bytes_in`, `bytes_out`, `audio_ms`, `tokens_in`, `tokens_out` | int ≥ 0 |
| `error_class` | Python identifier (exception class name) |
| `error_code` | enum (`ErrorCode`, ADR 0016) |

- Services log only through `AllowListLogger.log(event, **fields)`.
- The standard library and third-party loggers (uvicorn, httpx, …) are routed
  to a handler that **discards the message and arguments** and emits only
  `event=third_party_log`, `level`, `source` and, when an exception is
  attached, `error_class`. Python warnings are captured the same way.
- A record that fails validation is never emitted. In `strict` mode (tests,
  CI) the logger raises; in production it emits a `log_schema_violation`
  record containing no caller-supplied values.
- A test fixture captures every record emitted during the unit test session
  and fails the build if any record has a field outside the schema.

### Traces

`AllowListSpanExporter` wraps the real exporter and, for every span, keeps only
allow-listed attribute keys, drops all event attributes except
`exception.type`, and removes status descriptions (which may carry exception
messages). Span names must be code-defined (route templates, stage names).

### Metrics

Metric label names are restricted to `tenant`, `brand`, `workload`, `lane`,
`site`, `status_class`, `model`, validated by a Pydantic model with
`extra="forbid"`. Values are slugs.

## Consequences

- Debugging relies on request IDs and timings, never content.
- Adding a field is a reviewed change to `LogRecord` and to this ADR.
