"""Pure embed rendering — no discord.py import, so it is testable offline.

Embeds are built as Discord API dicts and converted with ``discord.Embed.from_dict`` at the edge.
"""
from __future__ import annotations

from typing import Any, Optional
import re

DEFAULT_COLOR = 0x5865F2  # Discord blurple
DEFAULT_FOOTER_TEMPLATE = "{model}"

# Discord limits (https://docs.discord.com/developers/resources/message#embed-object-embed-limits).
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FOOTER_LIMIT = 2048
EMBED_TOTAL_LIMIT = 6000


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


def build_embed_dict(
    description: str, *, color: int, footer: str = "", title: str = "",
) -> dict:
    """Build a reply embed, optionally labelling a page in a multipart response."""
    embed: dict = {"type": "rich", "description": description[:EMBED_DESCRIPTION_LIMIT], "color": color}
    if title:
        embed["title"] = title[:256]
    if footer:
        embed["footer"] = {"text": footer}
    # Discord enforces both a per-field cap and a 6000-character aggregate cap.
    # Keep the builder valid even when callers provide unusually long custom footers.
    available = max(
        0,
        EMBED_TOTAL_LIMIT - len(embed.get("title", "")) - len(embed.get("footer", {}).get("text", "")),
    )
    embed["description"] = embed["description"][:available]
    return embed


def build_report_embed_dict(
    page: str, *, color: int, index: int, total: int, job_id: str = "", footer: str = "",
) -> dict:
    """Build one page embed of a paginated Cronjob report."""
    title = f"Cronjob ({job_id}) · page {index + 1}/{total}" if total > 1 else "Cronjob"
    return build_embed_dict(page, color=color, footer=footer, title=title[:256])


def build_offer_embed_dict(
    title: str, body: str, *, color: int, number: int, footer: str = "", job_id: str = "",
) -> dict:
    """Build a compact job-offer card from one numbered Cronjob Response block."""
    lines = body.splitlines()
    location = deadline = targeting = ""
    for index, line in enumerate(lines):
        match = re.search(r"📍\s*(.*?)\s*\|\s*📅\s*(.*?)\s*\|\s*🎯\s*(.*)", line)
        if match:
            location, deadline, targeting = (part.strip() for part in match.groups())
            del lines[index]
            break

    url = next((line.strip().strip("<> ") for line in lines if re.match(r"https?://\S+", line.strip())), "")
    lines = [line for line in lines if not re.match(r"https?://\S+", line.strip())]
    description = "\n".join(lines).strip()
    embed: dict = {
        "type": "rich",
        "title": title.strip()[:256],
        "description": description or "Offre détectée par la veille emploi.",
        "color": color,
    }
    if url:
        embed["url"] = url[:2048]
    fields = []
    for name, value in (("📍 Localisation", location), ("📅 Échéance", deadline), ("🎯 Ciblage", targeting)):
        if value:
            fields.append({"name": name, "value": value[:1024], "inline": name != "🎯 Ciblage"})
    if fields:
        embed["fields"] = fields
    card_footer = f"Offre {number}"
    if job_id:
        card_footer += f" · job_id: {job_id}"
    if footer:
        card_footer += f" · {footer}"
    card_footer = card_footer[:EMBED_FOOTER_LIMIT]
    field_chars = sum(len(field["name"]) + len(field["value"]) for field in fields)
    available_description = max(
        0,
        EMBED_TOTAL_LIMIT - len(embed["title"]) - field_chars - len(card_footer),
    )
    embed["description"] = description[:min(EMBED_DESCRIPTION_LIMIT, available_description)]
    embed["footer"] = {"text": card_footer}
    return embed
