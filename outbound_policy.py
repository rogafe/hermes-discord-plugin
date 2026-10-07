"""Keep personal-account MCP writes out of the gateway's own conversation."""
from __future__ import annotations

import re
from typing import Any

from .models import current_discord_route


# Hermes uses mcp__server__tool; older releases used mcp_server_tool.
# Account-qualified Discord servers share the same send_message/add_reaction contract.
_DISCORD_WRITE = re.compile(
    r"(?:mcp__discord(?:_[a-zA-Z0-9_]+)?__|mcp_discord(?:_[a-zA-Z0-9_]+)?_)"
    r"(send_message|add_reaction)\Z"
)

_BLOCK_MESSAGES = {
    "send_message": (
        "Do not send a Discord MCP message into the current gateway conversation: "
        "it posts as a personal account and Hermes would ingest it as a new user turn. "
        "Put your response in the normal final reply; the gateway will deliver it as the bot. "
        "If you are a delegated agent, return the response to your parent agent."
    ),
    "add_reaction": (
        "Do not react through the Discord MCP in the current gateway conversation: "
        "the reaction comes from the user's own account, and the gateway already "
        "acknowledges their messages. Just answer in the normal final reply."
    ),
}


def _current_destination() -> str:
    chat_id, thread_id = current_discord_route()
    return thread_id or chat_id


def guard_current_conversation_send(
    *, tool_name: str = "", args: Any = None, **_kwargs: Any,
) -> dict[str, str] | None:
    """Veto Discord MCP sends and reactions addressed to this turn's delivery destination.

    Read the task-local route at dispatch, never cache it by session id: delegated
    children inherit the origin route but have their own durable session ids.
    A thread's parent is a different destination and remains available for writes.
    """
    match = _DISCORD_WRITE.fullmatch(tool_name)
    if not match or not isinstance(args, dict):
        return None
    destination = _current_destination()
    channel_id = args.get("channel_id")
    if not destination or channel_id is None or str(channel_id).strip() != str(destination):
        return None
    return {"action": "block", "message": _BLOCK_MESSAGES[match.group(1)]}


def current_conversation_reminder(*, platform: str = "", **_kwargs: Any) -> dict[str, str] | None:
    """Per-turn delivery rule for Discord turns.

    Hermes freezes a session's system prompt at its first turn, so SOUL.md or skill fixes
    never reach conversations opened earlier, and their history can still show the agent
    posting through the MCP. ``pre_llm_call`` context rides each user message instead.
    """
    if platform != "discord":
        return None
    destination = _current_destination()
    if not destination:
        return None
    return {"context": (
        f"[Discord delivery] Answer this conversation (channel {destination}) only through your "
        "normal final reply; the gateway posts it as the bot. Never call a Discord MCP "
        "send_message or add_reaction on this channel: those act as the user's personal account "
        "and a sent message comes back as a new user turn. If earlier turns did so, do not repeat it."
    )}
