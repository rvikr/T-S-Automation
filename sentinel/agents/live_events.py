"""In-process event sink for streaming agent progress to a UI.

The Streamlit app registers a callback for the duration of one run and the
runtime's RunHooks emit through it, so tool calls and handoffs render while
the agents are still working. When no sink is registered (tests, CLI, API,
eval) every emit is a no-op and behavior is byte-identical to a hook-less run.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

_sink: Callable[[str], None] | None = None


@contextmanager
def event_sink(callback: Callable[[str], None]) -> Iterator[None]:
    global _sink
    previous = _sink
    _sink = callback
    try:
        yield
    finally:
        _sink = previous


def sink_active() -> bool:
    return _sink is not None


def emit(event: str) -> None:
    if _sink is None:
        return
    try:
        _sink(event)
    except Exception:
        # A rendering failure must never break a moderation run.
        pass
