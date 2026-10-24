"""Thread-safe fan-out of live events (transcript segments, proactive cards, indexing progress)."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def _deliver(message: str) -> None:
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        if q.full():
            try:
                q.get_nowait()  # drop the oldest event rather than stall the producer
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(message)


def publish(kind: str, **data: Any) -> None:
    """Callable from any thread."""
    if _loop is None or _loop.is_closed():
        return
    message = json.dumps({"type": kind, **data}, ensure_ascii=False, default=str)
    try:
        _loop.call_soon_threadsafe(_deliver, message)
    except RuntimeError:
        log.debug("event loop closed; dropped %s", kind)
