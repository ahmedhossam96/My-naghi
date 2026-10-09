"""Per-request context carried in a ContextVar.

Holds only allow-listed identifiers (never content). The logger and tracer read
it so callers do not have to pass request IDs around.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, replace

from triaxis_common.types import Lane, Workload


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str | None = None
    tenant: str | None = None
    brand: str | None = None
    workload: Workload | None = None
    lane: Lane | None = None

    def as_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "request_id": self.request_id,
            "tenant": self.tenant,
            "brand": self.brand,
            "workload": self.workload,
            "lane": self.lane,
        }
        return {k: v for k, v in fields.items() if v is not None}


_current: ContextVar[RequestContext] = ContextVar("triaxis_request_context")
_EMPTY = RequestContext()


def current() -> RequestContext:
    return _current.get(_EMPTY)


def set_context(ctx: RequestContext) -> Token[RequestContext]:
    return _current.set(ctx)


def update_context(
    *,
    tenant: str | None = None,
    brand: str | None = None,
    workload: Workload | None = None,
    lane: Lane | None = None,
) -> RequestContext:
    """Add identifiers to the current context (e.g. after authentication)."""
    ctx = current()
    changes: dict[str, object] = {}
    if tenant is not None:
        changes["tenant"] = tenant
    if brand is not None:
        changes["brand"] = brand
    if workload is not None:
        changes["workload"] = workload
    if lane is not None:
        changes["lane"] = lane
    new = replace(ctx, **changes)  # type: ignore[arg-type]
    _current.set(new)
    return new


def reset_context(token: Token[RequestContext]) -> None:
    _current.reset(token)
