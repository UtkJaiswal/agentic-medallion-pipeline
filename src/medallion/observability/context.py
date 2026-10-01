"""Request/run correlation. A trace id follows a unit of work from the API, through the job queue,
into every log line, LLM call record, lineage row and emitted event."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_stage: ContextVar[str | None] = ContextVar("stage", default=None)

# W3C traceparent: version-traceid-parentid-flags
_TRACEPARENT = re.compile(r"^[\da-f]{2}-([\da-f]{32})-[\da-f]{16}-[\da-f]{2}$")
_TRACE_ID = re.compile(r"^[A-Za-z0-9\-_.]{8,128}$")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def trace_id_from_headers(headers: dict[str, str]) -> str:
    """Accept an inbound `traceparent` or `X-Trace-Id`; otherwise mint one. Never trust malformed input."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if m := _TRACEPARENT.match(lowered.get("traceparent", "").strip()):
        return m.group(1)
    candidate = lowered.get("x-trace-id", "").strip()
    return candidate if _TRACE_ID.match(candidate) else new_trace_id()


def current_trace_id() -> str:
    tid = _trace_id.get()
    if tid is None:
        tid = new_trace_id()
        _trace_id.set(tid)
    return tid


def current_run_id() -> str | None:
    return _run_id.get()


def snapshot() -> dict[str, str]:
    return {k: v for k, v in (("trace_id", _trace_id.get()), ("run_id", _run_id.get()),
                              ("stage", _stage.get())) if v}


@contextmanager
def bind(*, trace_id: str | None = None, run_id: str | None = None, stage: str | None = None) -> Iterator[None]:
    tokens = []
    for var, value in ((_trace_id, trace_id), (_run_id, run_id), (_stage, stage)):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
