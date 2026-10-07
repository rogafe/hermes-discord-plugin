"""Keep personal-account MCP posts from becoming the gateway's next user turn."""
from __future__ import annotations

import re
from typing import Any

from .models import current_discord_route


# Hermes uses mcp__server__tool; older releases used mcp_server_tool.
# Account-qualified Discord servers share the same send_message contract.
_DISCORD_SEND = re.compile(
    r"(?:mcp__discord(?:_[a-zA-Z0-9_]+)?__send_message|"
    r"mcp_discord(?:_[a-zA-Z0-9_]+)?_send_message)\Z"
)


def guard_current_conversation_send(
    *, tool_name: str = "", args: Any = None, **_kwargs: Any,
) -> dict[str, str] | None:
    """Veto only Discord MCP sends addressed to this turn's delivery destination.

    Read the task-local route at dispatch, never cache it by session id: delegated
    children inherit the origin route but have their own durable session ids.
    A thread's parent is a different destination and remains available for sends.
    """
    if not _DISCORD_SEND.fullmatch(tool_name) or not isinstance(args, dict):
        return None
    chat_id, thread_id = current_discord_route()
    destination = thread_id or chat_id
    channel_id = args.get("channel_id")
    if not destination or channel_id is None or str(channel_id).strip() != str(destination):
        return None
    return {
        "action": "block",
        "message": (
            "Do not send a Discord MCP message into the current gateway conversation: "
            "it posts as a personal account and Hermes would ingest it as a new user turn. "
            "Put your response in the normal final reply; the gateway will deliver it as the bot. "
            "If you are a delegated agent, return the response to your parent agent."
        ),
    }
