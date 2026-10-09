# 0016 — Error contract

- Status: accepted
- Date: 2026-10-09
- Deciders: Lead engineer; product owner (Quantum Seal)

## Context

Error messages often echo input ("invalid value 'xyz'"), which would leak
customer content into client logs and our own. Clients built against OpenAI
APIs expect an `error` object.

## Decision

Every error response has this shape (OpenAI-compatible):

```json
{"error": {"type": "authentication_error", "code": "token_expired",
           "message": "The access token has expired.", "request_id": "…"}}
```

- `code` is an `ErrorCode` enum. `type`, HTTP status and `message` are looked
  up in a fixed catalogue (`triaxis_common.errors`). Messages never interpolate
  request data.
- Pydantic validation failures return `invalid_request` with no field values
  and no field locations (locations can contain client-supplied keys, e.g. an
  unknown field's name). Field-level hints may be added later from a
  schema-derived allow-list.
- Unhandled exceptions return `internal_error`; only the exception class name
  is logged.
- Starlette HTTP exceptions (404, 405, …) are mapped to catalogue codes.

## Consequences

- Clients get less detail on validation errors. The API reference documents
  every limit so clients can validate before sending.
