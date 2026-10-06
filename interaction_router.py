"""Small, generic dispatcher for Discord component interactions.

The router deliberately depends only on the ``interaction.data.custom_id`` shape
provided by discord.py. Feature modules register their own namespaced IDs and
remain responsible for authorization, acknowledgement, and business logic.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

InteractionHandler = Callable[[Any], Awaitable[None]]


class InteractionRouter:
    """Dispatch component interactions to handlers registered by custom-ID prefix."""

    def __init__(self) -> None:
        self._handlers: dict[str, InteractionHandler] = {}
        self._attached_bots: set[int] = set()

    def register(self, prefix: str, handler: InteractionHandler) -> None:
        """Register a handler for a namespaced custom ID prefix.

        Longer prefixes win, which allows a feature to provide a general
        namespace handler and override a narrower sub-namespace when needed.
        Re-registering a prefix replaces its handler, making plugin reconnects
        idempotent.
        """
        if not prefix or not isinstance(prefix, str):
            raise ValueError("interaction custom_id prefix must be a non-empty string")
        self._handlers[prefix] = handler

    def attach(self, bot: Any) -> bool:
        """Attach the router to a bot once. Return whether attachment succeeded."""
        key = id(bot)
        if key in self._attached_bots:
            return True
        add_listener = getattr(bot, "add_listener", None)
        if not callable(add_listener):
            return False
        try:
            add_listener(self.on_interaction, "on_interaction")
        except Exception:
            logger.warning("hermes-discord-plugin: could not attach interaction router", exc_info=True)
            return False
        self._attached_bots.add(key)
        return True

    async def on_interaction(self, interaction: Any) -> None:
        data = getattr(interaction, "data", None)
        custom_id = data.get("custom_id") if isinstance(data, dict) else None
        if not isinstance(custom_id, str):
            return
        handler = next(
            (self._handlers[prefix] for prefix in sorted(self._handlers, key=len, reverse=True)
             if custom_id.startswith(prefix)),
            None,
        )
        if handler is None:
            return
        try:
            await handler(interaction)
        except Exception:
            logger.exception("hermes-discord-plugin: unhandled component interaction for %s", custom_id[:80])
