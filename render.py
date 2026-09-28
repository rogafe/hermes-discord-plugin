"""Pure embed rendering — no discord.py import, so it is testable offline.

Embeds are built as Discord API dicts and converted with ``discord.Embed.from_dict`` at the edge.
"""
from __future__ import annotations

from typing import Any, Optional

DEFAULT_COLOR = 0x5865F2  # Discord blurple
DEFAULT_FOOTER_TEMPLATE = "{model}"

# Discord limits (https://docs.discord.com/developers/resources/message#embed-object-embed-limits).
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FOOTER_LIMIT = 2048


def model_short(model: Optional[str]) -> str:
    """Drop the ``vendor/`` prefix (``anthropic/claude-opus-5-5`` → ``claude-opus-5-5``)."""
    return model.rsplit("/", 1)[-1] if model else ""


def parse_color(value: Any) -> int:
    """Accept ``#5865F2`` / ``0x5865F2`` / ``5865F2`` / an int; fall back to blurple."""
    if isinstance(value, bool):
        return DEFAULT_COLOR
    if isinstance(value, int):
        return value if 0 <= value <= 0xFFFFFF else DEFAULT_COLOR
    if isinstance(value, str):
        text = value.strip().lower().removeprefix("#").removeprefix("0x")
        try:
            parsed = int(text, 16)
        except ValueError:
            return DEFAULT_COLOR
        return parsed if 0 <= parsed <= 0xFFFFFF else DEFAULT_COLOR
    return DEFAULT_COLOR


def footer_text(model: Optional[str], template: Optional[str] = None) -> str:
    """Render the footer line, or "" when there is no model."""
    if not model:
        return ""
    template = template or DEFAULT_FOOTER_TEMPLATE
    try:
        text = template.format(model=model_short(model), model_full=model)
    except (KeyError, IndexError, ValueError):
        text = model_short(model)
    return text[:EMBED_FOOTER_LIMIT]


def build_embed_dict(description: str, *, color: int, footer: str = "") -> dict:
    """One embed carrying *description*; footer only when non-empty."""
    embed: dict = {"type": "rich", "description": description[:EMBED_DESCRIPTION_LIMIT], "color": color}
    if footer:
        embed["footer"] = {"text": footer}
    return embed
