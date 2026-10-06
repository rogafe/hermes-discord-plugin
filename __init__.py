"""hermes-discord-plugin — render Hermes Agent's Discord replies as embeds with the model in the footer.

Settings live under ``plugins.entries.hermes-discord-plugin.settings`` in ``config.yaml`` (see
``plugin.yaml``'s ``config_schema``) and are read on every reply, so no restart is needed.
"""
from __future__ import annotations

import logging
from typing import Any

from .embeds import install
from .models import ModelTracker, current_discord_route
from .state_store import OfferStateStore

logger = logging.getLogger(__name__)

PENDING_COMMAND_NAME = "cron-pending"
PENDING_COMMAND_DESCRIPTION = "List unclaimed cron offer cards in this conversation"
PENDING_COMMAND_LIMIT = 10

RECOVER_COMMAND_NAME = "cron-recover"
RECOVER_COMMAND_DESCRIPTION = "Release offer actions/notes interrupted by a gateway stop (outcome unknown)"


def _session_keys() -> list[str]:
    """Deduplicated chat/thread ids for the conversation running in this context."""
    chat_id, thread_id = current_discord_route()
    # Offer cards are registered under the resolved Discord channel id, which is the
    # thread id inside a thread and the parent channel otherwise; probe both, thread first.
    return list(dict.fromkeys(key for key in (thread_id, chat_id) if key))


def _cron_pending_handler(raw_args: str = "") -> str:
    """Hermes plugin slash command body: recover pending offer numbers from plugin_db.

    Runs through Hermes' dispatch (drain gate, slash-access check, session env scope), so the
    plugin never touches Discord interaction auth itself. Returns None-free plain text that
    Hermes delivers as the command reply in the calling conversation.
    """
    keys = _session_keys()
    if not keys:
        return "No pending offer listing available (not a Discord conversation)."
    store = OfferStateStore()
    for key in keys:
        cards = store.pending_cards(key)
        if cards:
            break
    else:
        return (
            "No pending cron offer cards in this conversation. "
            "Every card already has an action, or no card was rendered recently."
        )
    shown = cards[:PENDING_COMMAND_LIMIT]
    pending_count = f"at least {len(cards)}" if len(cards) > 50 else str(len(cards))
    lines = [f"📋 {pending_count} pending cron offer(s) in this conversation:"]
    for _message_id, job_id, number, has_note in shown:
        flags = []
        if has_note:
            flags.append("note written, no action yet")
        flags_text = f" ({': '.join(flags)})" if flags else ""
        lines.append(f"• **{number}** — job `{job_id}`{flags_text}")
    if len(cards) > len(shown):
        extra = len(cards) - len(shown)
        lines.append(
            f"…and at least {extra} more (the list is capped at 50)."
            if len(cards) > 50 else f"…and {extra} more."
        )
    lines.append("For an unambiguous choice, reply `N 👀 (job_id: …)`.")
    return "\n".join(lines)


def _cron_recover_handler(raw_args: str = "") -> str:
    """Explicit recovery for clicks that never reached a terminal state.

    An action claim is written before the dispatch, and a crash between claim and
    finalize leaves the row pending forever: the card then reports "already sent"
    even though the choice may never have been delivered. This command releases
    only pending rows older than the recovery grace window, and never silently: it
    names each released offer and warns that the original outcome is unknown, so
    re-sending an actually-delivered choice could duplicate the decision.
    """
    from .state_store import RECOVERY_GRACE_SECONDS

    keys = _session_keys()
    if not keys:
        return "Recovery unavailable outside a Discord conversation."
    store = OfferStateStore()
    channel = None
    for key in keys:
        if store.stale_interactions(key):
            channel = key
            break
    if channel is None:
        return (
            "Nothing to recover: no interrupted offer action or note in this "
            "conversation."
        )
    released = store.release_stale_interactions(channel)
    if not released:
        # Everything became terminal between the scan and the release; nothing was erased.
        return "Nothing to recover in this conversation (state changed while checking)."
    minutes = RECOVERY_GRACE_SECONDS // 60
    lines = [
        f"♻️ {len(released)} interrupted offer interaction(s) released; their outcome is *unknown*:",
    ]
    for message_id, job_id, number, kind in released:
        kind_fr = "action slot" if kind == "action" else "note slot"
        lines.append(f"• offer {number} — job `{job_id}` — {kind_fr} freed (card {message_id}).")
    lines.append(
        "Each one may or may not have been sent to Hermes before the stop; if one was, "
        "using the freed card again could duplicate the decision. The slots are now free: "
        f"click again or reply in text. Only items older than ~{minutes} min are affected."
    )
    return "\n".join(lines)


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
    # Official plugin-command seam: Hermes discovers this once, guards built-in name conflicts,
    # then the Discord adapter mirrors it into the native slash picker and the gateway dispatches
    # it through the same auth/drain gates as built-in commands (gateway/run_inbound.py).
    # Uses the upcoming-feature toggle instead of enabled because the command is cron-specific;
    # registration happens once, so changing it requires a gateway restart.
    if callable(getattr(ctx, "register_command", None)) and ctx.get_config(
        "cron_pending_command", True,
    ):
        ctx.register_command(
            PENDING_COMMAND_NAME, _cron_pending_handler,
            description=PENDING_COMMAND_DESCRIPTION,
        )
        # Same seam and same restart caveat: registration is a one-time discovery.
        if ctx.get_config("cron_recover_command", True):
            ctx.register_command(
                RECOVER_COMMAND_NAME, _cron_recover_handler,
                description=RECOVER_COMMAND_DESCRIPTION,
            )
