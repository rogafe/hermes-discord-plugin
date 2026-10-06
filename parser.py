"""Pure parsers for Hermes reply formats; no discord.py imports or side effects."""
from __future__ import annotations

import logging
import re
from typing import Optional

from .message_model import MessageDocument, Offer, Segment

logger = logging.getLogger(__name__)

_CRON_HEADER = re.compile(
    r"^[ \t]*Cronjob Response:[^\r\n]*\r?\n[ \t]*\(job_id:[ \t]*([A-Za-z0-9_-]+)[ \t]*\)",
    re.I | re.M,
)
_OFFER_HEADING = re.compile(r"^\s*\*\*\[(\d+)\]\s*(.*?)\*\*\s*(?:🆕)?\s*$")
_ACTION_LINE = re.compile(r"^\s*(?:🗑️|👀|📝)\s*(?:Ignore|Ignorer|Follow|Suivre|Va postuler).*$", re.I)
_CONFIRMATION_PREFIX = re.compile(r"^\s*(?:Confirmation requise|Action requise)\s*:?", re.I)
_CONFIRMED_PREFIX = re.compile(r"^\s*✅\s*(?:Confirmé|Validé)\b", re.I)
_CANCELLED_PREFIX = re.compile(r"^\s*❌\s*(?:Annulé|Refusé)\b", re.I)


def parse_message(content: str) -> MessageDocument:
    """Run specific recognizers first; preserve unrecognized messages verbatim."""
    parsed = _parse_cron_offer_report(content)
    if parsed is not None:
        return parsed
    parsed = _parse_notice(content)
    if parsed is not None:
        return parsed
    return MessageDocument(segments=[Segment(content)], source_format="plain")


def _parse_notice(content: str) -> Optional[MessageDocument]:
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if first.startswith("🚨"):
        segment = Segment(content, kind="alert", title="Alerte urgente", color=0xED4245)
        return MessageDocument([segment], source_format="alert")
    if first.startswith(("⚠️", "🔴")):
        segment = Segment(content, kind="alert", title="Avertissement", color=0xF0A020)
        return MessageDocument([segment], source_format="alert")
    if _CONFIRMATION_PREFIX.match(first):
        segment = Segment(content, kind="confirmation", title="Confirmation requise", color=0xF0A020)
        return MessageDocument([segment], source_format="confirmation")
    if _CONFIRMED_PREFIX.match(first):
        segment = Segment(content, kind="confirmation", title="Confirmation", color=0x57F287)
        return MessageDocument([segment], source_format="confirmation")
    if _CANCELLED_PREFIX.match(first):
        segment = Segment(content, kind="confirmation", title="Annulation", color=0xED4245)
        return MessageDocument([segment], source_format="confirmation")
    return None


def _parse_cron_offer_report(content: str) -> Optional[MessageDocument]:
    if not isinstance(content, str):
        return None
    header = _CRON_HEADER.search(content)
    if not header:
        if content.lstrip().lower().startswith("cronjob response:"):
            logger.warning("hermes-discord-plugin: cron response ignored: missing or malformed job_id header")
        return None
    job_id = header.group(1)
    lines = content.splitlines(keepends=True)
    matches: list[tuple[int, re.Match[str]]] = []
    offset = 0
    in_fence = False
    for line in lines:
        if is_fence_boundary(line):
            in_fence = not in_fence
        match = _OFFER_HEADING.match(line.rstrip("\r\n"))
        if match and not in_fence:
            matches.append((offset, match))
        offset += len(line)
    if not matches:
        return None

    segments: list[Segment] = []
    prefix = _clean_report_fragment(content[:matches[0][0]])
    if prefix:
        segments.append(Segment(prefix))
    for index, (start, heading) in enumerate(matches):
        end = matches[index + 1][0] if index + 1 < len(matches) else len(content)
        body = _clean_offer_body(content[start + len(heading.group(0)):end])
        number = int(heading.group(1))
        if number > 999:
            logger.warning("hermes-discord-plugin: cron response ignored: offer number exceeds component limit")
            return None
        segments.append(Segment(
            text="\n".join((heading.group(0), body)).strip(),
            offer=Offer(number=number, title=heading.group(2).strip(), body=body, job_id=job_id),
        ))
    numbers = [segment.offer.number for segment in segments if segment.offer]
    if len(numbers) != len(set(numbers)):
        logger.warning("hermes-discord-plugin: cron response ignored: duplicate offer numbers")
        return None

    tail_start = matches[-1][0]
    tail = content[tail_start:]
    trailer = re.search(
        r"\n\s*(?:⏳|(?:\*\*)?(?:Échéances proches|Non retenues aujourd['’]hui:|Permis B:|Répondez simplement|To stop or manage this job))",
        tail, re.I,
    )
    last = segments[-1]
    if trailer:
        offer_end = matches[-1][0] + trailer.start()
        last_body = _clean_offer_body(content[matches[-1][0] + len(matches[-1][1].group(0)):offer_end])
        last.offer.body = last_body
        last.text = "\n".join((matches[-1][1].group(0), last_body)).strip()
        suffix = _clean_report_fragment(content[offer_end:])
        if suffix:
            segments.append(Segment(suffix))

    # Keep every offer readable while limiting actions to cards that fit in one Discord message.
    for index, segment in enumerate(segments):
        if segment.offer and len(segment.text) > 1800:
            logger.info(
                "hermes-discord-plugin: long cron offer kept readable without buttons (offer=%d)",
                segment.offer.number,
            )
            segments[index] = Segment(segment.text)
    return MessageDocument(segments=segments, source_format="cron_offer_report", job_id=job_id)


def _clean_report_fragment(text: str) -> str:
    return _clean_cron_fragment(text)


def _clean_offer_body(text: str) -> str:
    return _clean_cron_fragment(text, remove_actions=True)


def _clean_cron_fragment(text: str, *, remove_actions: bool = False) -> str:
    """Drop adapter markers outside code while preserving source spacing and Markdown."""
    lines = []
    in_fence = False
    for line in text.splitlines():
        if is_fence_boundary(line):
            lines.append(line)
            in_fence = not in_fence
            continue
        if not in_fence and (_is_page_marker(line) or (remove_actions and _ACTION_LINE.match(line))):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _is_page_marker(line: str) -> bool:
    return bool(re.fullmatch(r"\s*\(?\d+/\d+\)?\s*", line))


def is_fence_boundary(line: str) -> bool:
    stripped = line.strip()
    if not re.match(r"^(?:```|~~~)", stripped):
        return False
    fence = "```" if stripped.startswith("```") else "~~~"
    return stripped == fence or not stripped.endswith(fence)
