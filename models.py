"""Remember which model answered in which Discord channel, until the reply is delivered.

``post_llm_call`` fires in the agent thread right before the gateway delivers the final reply. It
knows the model but not the Discord channel, so the channel is read from Hermes' per-turn session
context (the same contextvars tools use to route deliveries). The adapter wrapper then takes the
entry when it sends that reply.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# A reply is delivered seconds after post_llm_call; anything older is a turn whose reply never went
# out (intentional silence, interrupt) and must not leak onto an unrelated later message.
ENTRY_TTL_SECONDS = 300.0


class _Entry:
    __slots__ = ("model", "created")

    def __init__(self, model: str, created: float):
        self.model = model
        self.created = created


class ModelTracker:
    """Thread-safe ``channel id -> model`` map with take-once semantics."""

    def __init__(self, ttl: float = ENTRY_TTL_SECONDS, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def record(self, keys: Iterable[str], model: str) -> None:
        """Record *model* under every non-empty key (thread id and parent channel id)."""
        keys = [k for k in keys if k]
        if not keys or not model:
            return
        entry = _Entry(model, self._clock())
        with self._lock:
            self._prune()
            for key in keys:
                self._entries[key] = entry

    def take(self, keys: Iterable[str]) -> Optional[str]:
        """Return and forget the model recorded for the first matching key."""
        with self._lock:
            self._prune()
            for key in keys:
                entry = self._entries.get(key) if key else None
                if entry is None:
                    continue
                for other in [k for k, v in self._entries.items() if v is entry]:
                    del self._entries[other]
                return entry.model
        return None

    def _prune(self) -> None:
        cutoff = self._clock() - self._ttl
        for key in [k for k, v in self._entries.items() if v.created < cutoff]:
            del self._entries[key]


def current_discord_route() -> tuple[str, str]:
    """``(chat_id, thread_id)`` of the turn running in this context, or ``("", "")``."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return "", ""
    if get_session_env("HERMES_SESSION_PLATFORM") != "discord":
        return "", ""
    return get_session_env("HERMES_SESSION_CHAT_ID"), get_session_env("HERMES_SESSION_THREAD_ID")
