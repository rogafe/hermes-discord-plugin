"""Pure Components V2 layouts — no discord.py import, so it is testable offline.

Layouts are Discord API component dicts (https://docs.discord.com/developers/components/reference).
A message carrying them must set the ``IS_COMPONENTS_V2`` flag and have no content or embeds.
Unlike embeds, Text Display components render ``#``/``##``/``###`` headings and ``-#`` subtext.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

IS_COMPONENTS_V2 = 1 << 15

ACTION_ROW, BUTTON, SECTION, TEXT_DISPLAY, THUMBNAIL, MEDIA_GALLERY, SEPARATOR, CONTAINER = 1, 2, 9, 10, 11, 12, 14, 17
LINK_STYLE = 5

TEXT_LIMIT = 4000          # all Text Display content of one message, combined
COMPONENT_LIMIT = 40       # all components of one message, nested ones included
BUTTON_LABEL_LIMIT = 80
MAX_LINK_BUTTONS = 5       # one action row


# --- primitives -------------------------------------------------------------------------------

def text(content: str) -> dict:
    return {"type": TEXT_DISPLAY, "content": content}


def separator(*, divider: bool = True, large: bool = False) -> dict:
    return {"type": SEPARATOR, "divider": divider, "spacing": 2 if large else 1}


def link_button(label: str, url: str) -> dict:
    return {"type": BUTTON, "style": LINK_STYLE, "label": label[:BUTTON_LABEL_LIMIT], "url": url}


def action_row(*items: dict) -> dict:
    return {"type": ACTION_ROW, "components": list(items)}


def container(*items: dict, color: Optional[int] = None) -> dict:
    box: dict = {"type": CONTAINER, "components": [item for item in items if item]}
    if color is not None:
        box["accent_color"] = color
    return box


def text_length(components: list[dict]) -> int:
    total = 0
    for component in components:
        if component.get("type") == TEXT_DISPLAY:
            total += len(component.get("content", ""))
        total += text_length(component.get("components", []))
    return total


def component_count(components: list[dict]) -> int:
    return sum(1 + component_count(c.get("components", [])) for c in components)


def fits(components: list[dict]) -> bool:
    return text_length(components) <= TEXT_LIMIT and component_count(components) <= COMPONENT_LIMIT


def edit_payload(components: list[dict], current_flags: int = 0) -> dict:
    """PATCH body turning an existing message into a Components V2 message.

    Discord requires the old content and embeds to be cleared in the same edit, and the flag
    bitfield must keep every flag already set. The flag can never be removed afterwards.
    """
    return {
        "content": None,
        "embeds": [],
        "flags": (current_flags or 0) | IS_COMPONENTS_V2,
        "components": components,
    }


# --- link buttons -----------------------------------------------------------------------------

_LINK_LINE = re.compile(
    r"^\s*(?:[-•*]\s*)?(?:(?P<label>[^\s<>:][^<>:\n]{0,60}?)\s*:\s*)?<?(?P<url>https?://[^\s<>]+?)>?\s*$"
)


def extract_link_buttons(body: str) -> tuple[str, list[dict]]:
    """Move standalone ``Label : <url>`` lines into link buttons; keep everything else as text."""
    kept: list[str] = []
    buttons: list[dict] = []
    in_fence = False
    for line in body.splitlines():
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_fence = not in_fence
        match = None if in_fence else _LINK_LINE.match(line)
        if match and len(buttons) < MAX_LINK_BUTTONS:
            buttons.append(link_button((match.group("label") or link_label(match.group("url"))).strip(),
                                       match.group("url")))
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n"), buttons


def link_label(url: str) -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if host.endswith("facebook.com"):
        return "Voir sur Facebook"
    if host.endswith("panneaupocket.com"):
        return "Voir le panneau"
    if host in {"discord.com", "discordapp.com"}:
        return "Ouvrir dans Discord"
    return f"Ouvrir · {host}" if host else "Ouvrir"


# --- ordinary replies -------------------------------------------------------------------------

_BOLD_TITLE = re.compile(r"^\s*\*\*([^*\n]{1,120})\*\*\s*$")


def promote_bold_title(body: str) -> str:
    """A first line that is only ``**Title**`` becomes a real heading (embeds could not show one)."""
    lines = body.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return body
    match = _BOLD_TITLE.match(lines[first])
    if match:
        lines[first] = f"## {match.group(1).strip()}"
    return "\n".join(lines)


def normalize_bullets(body: str) -> str:
    """``• item`` lines (common in model and cron output) become real Markdown list items."""
    lines, in_fence = [], False
    for line in body.split("\n"):
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            in_fence = not in_fence
        if not in_fence:
            line = re.sub(r"^(\s*)[•▪◦●]\s+", r"\1- ", line)
        lines.append(line)
    return "\n".join(lines)


def build_reply_layout(
    body: str, *, color: int, footer: str = "", label: str = "", title: str = "",
) -> list[dict]:
    """One reply (or one part of a multipart reply) in an accent-colored container."""
    items: list[dict] = []
    if label:
        items.append(text(f"-# {label}"))
    if title:
        items.append(text(f"### {title}"))
    items.append(text(body.strip() or "​"))
    if footer:
        items.extend([separator(), text(f"-# {footer}")])
    return [container(*items, color=color)]


# --- cron reports -----------------------------------------------------------------------------

_CRON_WRAPPER = re.compile(
    r"\A\s*Cronjob Response:[ \t]*(?P<name>[^\r\n]*)\r?\n[ \t]*\(job_id:[ \t]*(?P<job>[A-Za-z0-9_-]+)[ \t]*\)"
    r"[ \t]*\r?\n[ \t]*-{3,}[ \t]*\r?\n?",
)
_CRON_TRAILER = re.compile(
    r"\n*\s*To stop or manage this job, send me a new message \(e\.g\. \"stop reminder [^\n]*\"\)\.\s*\Z",
)


@dataclass
class CronWrapper:
    name: str
    job_id: str
    body: str


def strip_cron_wrapper(content: str) -> Optional[CronWrapper]:
    """Split Hermes' ``Cronjob Response`` header and its English stop hint from the report body."""
    match = _CRON_WRAPPER.match(content)
    if not match:
        return None
    body = _CRON_TRAILER.sub("", content[match.end():]).strip("\n")
    return CronWrapper(name=match.group("name").strip(), job_id=match.group("job"), body=body)


def strip_cron_trailer(content: str) -> str:
    return _CRON_TRAILER.sub("", content).rstrip()


def build_cron_layout(
    body: str, *, color: int, name: str = "", job_id: str = "", footer: str = "",
    timestamp: Optional[int] = None, label: str = "",
) -> list[dict]:
    """A scheduled report: job name as kicker, the report's own title promoted, links as buttons."""
    body, buttons = extract_link_buttons(normalize_bullets(promote_bold_title(body)))
    items: list[dict] = []
    kicker = " · ".join(bit for bit in (f"🕒 {name}" if name else "", label) if bit)
    if kicker:
        items.append(text(f"-# {kicker}"))
    items.append(text(body.strip() or "​"))
    meta = [bit for bit in (
        f"job `{job_id}`" if job_id else "",
        footer,
        f"<t:{timestamp}:f>" if timestamp else "",
    ) if bit]
    if buttons or meta:
        items.append(separator())
    if buttons:
        items.append(action_row(*buttons))
    if meta:
        items.append(text("-# " + " · ".join(meta)))
    return [container(*items, color=color)]


# --- webhook notifications (deliver_only routes) ----------------------------------------------

_NOTIFICATION_HEAD = re.compile(
    r"\A📌\s*\*\*(?P<source>[^*\n]+)\*\*\s*—\s*(?P<title>.*?)"
    r"(?:\s*\((?P<date>\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2})?)\))?\s*\Z"
)
_URL_LINE = re.compile(r"^\s*<?(https?://\S+?)>?\s*$")
_PIPE_TITLE = re.compile(r"\A\s*\|\s*(?P<title>[^|\n]{3,160}?)\s*\|\s*")
_TRAILING_TAGS = re.compile(r"(?:\s+#[\wÀ-ɏ]+){2,}\s*#?\s*…?\s*\Z")

HOST_COLORS = {
    "facebook.com": 0x1877F2,
    "panneaupocket.com": 0xE8590C,
}
DEFAULT_NOTIFICATION_COLOR = 0x5865F2


@dataclass
class Notification:
    source: str
    channel: str      # e.g. "Facebook", "groupe FB"
    title: str
    body: str
    date: str
    link: str
    tags: list[str]
    author: str = ""


def parse_notification(content: str) -> Optional[Notification]:
    """Recognize the ``📌 **{source}** — {title} ({date})\\n{summary}\\n{link}`` webhook template."""
    if not isinstance(content, str):
        return None
    lines = content.strip().split("\n")
    head = _NOTIFICATION_HEAD.match(lines[0].strip()) if lines else None
    if not head:
        return None
    rest = lines[1:]
    link = ""
    if rest and _URL_LINE.match(rest[-1]):
        link = _URL_LINE.match(rest[-1]).group(1)
        rest = rest[:-1]
    summary = "\n".join(rest).strip()
    source = head.group("source").strip()
    channel = ""
    paren = re.match(r"\A(.*?)\s*\(([^()]+)\)\s*\Z", source)
    if paren:
        source, channel = paren.group(1).strip(), paren.group(2).strip()
    title = head.group("title").strip()
    title, summary, author = _dedupe_title(title, summary)
    tags: list[str] = []
    trailing = _TRAILING_TAGS.search(summary)
    if trailing:
        tags = re.findall(r"#[\wÀ-ɏ]+", trailing.group(0))
        summary = summary[:trailing.start()].rstrip()
    return Notification(source=source, channel=channel, title=title, body=summary,
                        date=(head.group("date") or "").strip(), link=link, tags=tags,
                        author=author)


def _dedupe_title(title: str, summary: str) -> tuple[str, str, str]:
    """Producers often send ``title = summary[:120]``; show the text once, with a real title."""
    author = ""
    bare = title
    prefixed = re.match(r"\A(?P<author>[^:\n]{2,60}?) : (?P<rest>.*)\Z", title)
    if prefixed and summary.startswith(prefixed.group("rest")[:40]):
        author, bare = prefixed.group("author"), prefixed.group("rest")
    probe = bare.rstrip("… ").strip()
    if probe and summary.startswith(probe):
        pipe = _PIPE_TITLE.match(summary)
        if pipe:
            return pipe.group("title").strip(), summary[pipe.end():].strip(), author
        return "", summary, author
    if summary.strip() == title.strip():
        return title, "", ""
    return title, summary, ""


def notification_timestamp(date: str, tz: str = "Europe/Paris") -> Optional[tuple[int, bool]]:
    """``dd/mm/YYYY[ HH:MM]`` in local time → (unix seconds, has_time)."""
    from zoneinfo import ZoneInfo

    for fmt, has_time in (("%d/%m/%Y %H:%M", True), ("%d/%m/%Y", False)):
        try:
            parsed = datetime.strptime(date, fmt).replace(tzinfo=ZoneInfo(tz))
        except (ValueError, TypeError):
            continue
        return int(parsed.timestamp()), has_time
    return None


def build_notification_layout(note: Notification, *, max_tags: int = 8) -> list[dict]:
    host = (urlparse(note.link).hostname or "").removeprefix("www.") if note.link else ""
    color = next((value for suffix, value in HOST_COLORS.items() if host.endswith(suffix)),
                 DEFAULT_NOTIFICATION_COLOR)
    kicker = [f"📌 **{note.source}**"]
    if note.channel:
        kicker.append(note.channel)
    if note.author:
        kicker.append(f"✍️ {note.author}")
    stamp = notification_timestamp(note.date) if note.date else None
    if stamp:
        kicker.append(f"<t:{stamp[0]}:{'R' if stamp[1] else 'D'}>")
    elif note.date:
        kicker.append(note.date)
    items: list[dict] = [text("-# " + " · ".join(kicker))]
    if note.title:
        items.append(text(f"### {note.title}"))
    if note.body:
        items.append(text(note.body))
    if note.tags:
        items.append(text("-# " + " ".join(note.tags[:max_tags])))
    if note.link:
        items.extend([separator(), action_row(link_button(link_label(note.link), note.link))])
    return [container(*items, color=color)]
