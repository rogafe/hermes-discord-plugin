"""Pure page-packing for paginated cron reports; no discord.py imports.

Numbers and limits live here so the Discord-facing code in ``embeds.py`` only
orchestrates sends, stores, and interactions.
"""
from __future__ import annotations

# A page is first sent as plain message content (2000-char Discord limit), then
# turned into an embed. Stay far enough below 2000 so adapter-side formatting
# cannot push a page over the content limit and split it into two messages.
PAGE_TEXT_BUDGET = 1800
MAX_PAGER_PAGES = 999


def custom_id(job_id: str, page_index: int) -> str:
    """Namespaced component ID handled by the generic interaction router."""
    return f"hermes_pager|{job_id}|{page_index}"


def should_paginate(document, threshold: int) -> bool:
    """Paginate only large multi-offer reports; smaller ones keep their cards.
    Takes a :class:`~.message_model.MessageDocument`."""
    offers = sum(1 for segment in document.segments if segment.offer)
    return bool(document.job_id) and threshold > 0 and offers >= threshold


def build_report_pages(document, *, max_chars: int = PAGE_TEXT_BUDGET) -> list[str] | None:
    """Pack a parsed cron report into balanced pages, or None when pagination must be skipped.

    Pages keep the source text of each segment verbatim so sending preserves the
    adapter's Markdown (buttons re-render one page at a time later). The report
    segments stay in order: leading/trailing plain text rides along with the
    offers instead of being separated into extra messages.
    """
    parts = [segment.text for segment in document.segments if segment.text.strip()]
    if not parts:
        return None
    pages: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        part_len = len(part) + (2 if current else 0)
        if current and current_len + part_len > max_chars:
            pages.append("\n\n".join(current))
            current, current_len = [part], len(part)
            continue
        current.append(part)
        current_len += part_len
    if current:
        pages.append("\n\n".join(current))
    # A page that alone exceeds the budget cannot be split here without risking
    # mid-fence cuts; fall back to the unpaginated delivery instead.
    if any(len(page) > max_chars for page in pages) or len(pages) > MAX_PAGER_PAGES:
        return None
    if len(pages) < 2:
        return None
    return pages
