"""Optional Langfuse instrumentation.

Provides:
  * :func:`init`                     — call once at process startup
  * :func:`traced`                   — decorator: trace this function as a Langfuse span
  * :func:`set_trace_io`             — attach input / output to the current trace
  * :func:`update_span_metadata`     — attach key/value metadata to the current span
  * :func:`update_trace_metadata`    — attach session_id + key/value metadata to the trace

When ``LANGFUSE_PUBLIC_KEY`` is not set, every entry point becomes a no-op so the
rest of the system runs unchanged. This keeps the Langfuse dependency optional
at runtime even though it's a hard dependency at install time.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

_initialised = False


def is_active() -> bool:
    """True when Langfuse has been initialised with valid env vars."""
    return _initialised


def init() -> bool:
    """Initialise the Langfuse SDK if ``LANGFUSE_PUBLIC_KEY`` is configured.

    Returns True when active, False when running in no-op mode. Safe to call
    repeatedly; subsequent calls are no-ops.
    """
    global _initialised
    if _initialised:
        return True
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        logger.info("[observability] LANGFUSE_PUBLIC_KEY unset — running without tracing")
        return False
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("[observability] langfuse package not installed — tracing disabled")
        return False
    try:
        Langfuse()  # singleton; reads PUBLIC_KEY / SECRET_KEY / HOST from env
    except Exception as e:  # noqa: BLE001 — bad keys/host shouldn't kill the process
        logger.warning("[observability] Langfuse init failed: %s — tracing disabled", e)
        return False
    _initialised = True
    logger.info("[observability] Langfuse tracing enabled")
    return True


def traced(
    name: str | None = None,
    *,
    observation_type: str | None = None,
) -> Callable[[F], F]:
    """Decorator: trace this function via Langfuse if active, otherwise no-op.

    ``observation_type`` maps to Langfuse's typed observations
    (e.g. ``"tool"``, ``"generation"``); ``None`` falls back to a plain span.
    """

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _initialised:
                return fn(*args, **kwargs)
            from langfuse import observe

            decorated = (
                observe(name=name or fn.__name__, as_type=observation_type)(fn)
                if observation_type
                else observe(name=name or fn.__name__)(fn)
            )
            return decorated(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def set_trace_io(*, input: Any | None = None, output: Any | None = None) -> None:
    """Attach input/output to the current trace. No-op when inactive."""
    if not _initialised:
        return
    from langfuse import get_client

    client = get_client()
    if client is None:
        return
    try:
        client.set_current_trace_io(input=input, output=output)
    except Exception as e:  # noqa: BLE001 — tracing must never break the request
        logger.debug("[observability] set_trace_io failed: %s", e)


def update_trace_metadata(
    *,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Attach session_id + arbitrary metadata to the current trace."""
    if not _initialised:
        return
    from langfuse import get_client

    client = get_client()
    if client is None:
        return
    try:
        payload: dict[str, Any] = {}
        if session_id is not None:
            payload["session_id"] = session_id
        if metadata:
            payload["metadata"] = metadata
        if payload:
            client.update_current_span(**payload)
    except Exception as e:  # noqa: BLE001
        logger.debug("[observability] update_trace_metadata failed: %s", e)


@contextmanager
def span(
    name: str,
    *,
    observation_type: str | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
) -> Iterator[Any]:
    """Context manager: open a nested Langfuse observation with ``name``.

    Yields the underlying observation handle (or ``None`` when tracing is
    inactive). Call ``handle.update(output=..., metadata={...})`` inside the
    ``with`` block to attach the result. Tracing failures are silently
    swallowed — the wrapped work always runs.
    """
    if not _initialised:
        yield None
        return
    from langfuse import get_client

    client = get_client()
    if client is None:
        yield None
        return
    kwargs: dict[str, Any] = {"name": name}
    if observation_type:
        kwargs["as_type"] = observation_type
    if input is not None:
        kwargs["input"] = input
    if metadata:
        kwargs["metadata"] = metadata
    if model is not None:
        kwargs["model"] = model
    try:
        with client.start_as_current_observation(**kwargs) as obs:
            yield obs
    except Exception as e:  # noqa: BLE001
        logger.debug("[observability] span(%s) failed: %s", name, e)
        yield None


def update_span_metadata(**kwargs: Any) -> None:
    """Attach key/value metadata to the current span."""
    if not _initialised:
        return
    from langfuse import get_client

    client = get_client()
    if client is None:
        return
    try:
        client.update_current_span(metadata=kwargs)
    except Exception as e:  # noqa: BLE001
        logger.debug("[observability] update_span_metadata failed: %s", e)
