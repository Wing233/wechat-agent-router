"""Request-scoped trace ids for logs and LLM calls.

Trace ids live in contextvars, so a traced request stays isolated even when it
hops through ``asyncio.to_thread`` (each task copies the current context).
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import secrets

TRACE_DEFAULT = "-"
FORMAT = "%(asctime)s %(levelname)s [%(trace_id)s] %(name)s %(message)s"

_TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default=TRACE_DEFAULT)
_ROLE: contextvars.ContextVar[str] = contextvars.ContextVar("trace_role", default="owner")


def new_trace_id() -> str:
    """Return a short id that is easy to grep in container logs."""
    return secrets.token_hex(6)


def get_trace_id() -> str:
    return _TRACE_ID.get()


def get_role() -> str:
    return _ROLE.get()


def set_trace(trace_id: str | None = None) -> contextvars.Token:
    return _TRACE_ID.set(str(trace_id) if trace_id else new_trace_id())


def set_role(role: str | None) -> contextvars.Token:
    return _ROLE.set(str(role or "owner"))


def reset_trace(token: contextvars.Token | None) -> None:
    if token is not None:
        _TRACE_ID.reset(token)


def reset_role(token: contextvars.Token | None) -> None:
    if token is not None:
        _ROLE.reset(token)


@contextlib.contextmanager
def request(trace_id: str | None = None, role: str | None = None):
    """Bind a trace id and role for the duration of one inbound message."""
    trace_token = _TRACE_ID.set(str(trace_id) if trace_id else new_trace_id())
    role_token = _ROLE.set(str(role or "owner"))
    try:
        yield _TRACE_ID.get()
    finally:
        _ROLE.reset(role_token)
        _TRACE_ID.reset(trace_token)


class TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "trace_id", None):
            record.trace_id = _TRACE_ID.get()
        return True


def install_logging() -> None:
    """Add trace ids to every root handler while keeping existing formats."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, TraceFilter) for item in handler.filters):
            handler.addFilter(TraceFilter())
        formatter = handler.formatter
        if formatter is None:
            handler.setFormatter(logging.Formatter("[%(trace_id)s] %(message)s"))
            continue
        current = getattr(formatter, "_fmt", "") or ""
        if current and "%(trace_id)s" not in current:
            handler.setFormatter(logging.Formatter("[%(trace_id)s] " + current))
