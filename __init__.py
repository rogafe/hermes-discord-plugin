"""hermes-discord-plugin — render Hermes Agent's Discord replies as embeds with the model in the footer.

Settings live under ``plugins.entries.hermes-discord-plugin.settings`` in ``config.yaml`` (see
``plugin.yaml``'s ``config_schema``) and are read on every reply, so no restart is needed.
"""
from __future__ import annotations

import logging
from typing import Any

from .embeds import install
from .models import ModelTracker, current_discord_route

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    tracker = ModelTracker()

    def on_post_llm_call(*, model: str = "", platform: str = "", **_kwargs: Any) -> None:
        if platform != "discord" or not model:
            return
        chat_id, thread_id = current_discord_route()
        tracker.record((thread_id, chat_id), model)

    def wire_discord(bot: Any, adapter: Any) -> None:
        install(bot, adapter, tracker=tracker, get_setting=ctx.get_config)

    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_platform_handler("discord", wire_discord)
