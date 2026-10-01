"""Structured logging. JSON in containers (machine-parsable), plain text for local reading.

Usage: `log.info("bronze.loaded", extra={"rows": 10})` - the message is an event name and `extra`
carries fields. Trace/run/stage ids are attached automatically from the context."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from medallion.observability import context

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in context.snapshot().items():
            setattr(record, key, value)
        return True


def _fields(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}


_TZ: tzinfo = UTC


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, _TZ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = _fields(record)
        trace = fields.pop("trace_id", None)
        fields.pop("run_id", None)
        stage = fields.pop("stage", None)
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        prefix = f"{datetime.fromtimestamp(record.created, _TZ):%H:%M:%S} {record.levelname:<7}"
        where = f"[{stage}]" if stage else ""
        line = f"{prefix} {where}{record.getMessage()} {kv}".rstrip()
        if trace:
            line += f"  (trace={str(trace)[:8]})"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure(level: str = "INFO", fmt: str = "json", timezone: str = "Asia/Kolkata") -> None:
    global _TZ
    _TZ = ZoneInfo(timezone)  # timestamps in IST by default (+05:30 in JSON logs)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(_ContextFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "httpx2", "openai", "anthropic", "google_genai", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
