"""Markdown-aware splitting of long replies; pure, no discord.py imports or side effects.

``split_markdown`` cuts on paragraph > line > sentence > word boundaries, keeps inline Markdown
spans (links, code, bold, italic, strikethrough, bare URLs) whole, and never leaves a fenced code
block open: a fence that has to be cut is closed at the chunk end and reopened (same info string)
in the next chunk. ``split_markdown_parts`` exposes those synthetic lines separately so the
bodies always concatenate back to the input.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .parser import is_fence_boundary

# A higher-priority boundary smaller than this fraction of the limit loses to a lower-priority one.
MIN_FILL_RATIO = 4
_SENTENCE_END = ".!?…。！？"
# Inline spans that must not be cut. Matched per paragraph (no blank line inside), outside fences.
_INLINE_SPANS = [
    re.compile(r"!?\[[^\]\n]+\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+\"[^\"\n]*\")?\)"),
    re.compile(r"https?://[^\s<>]+"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"\*\*(?=\S)(?:(?!\n\n).)+?(?<=\S)\*\*", re.S),
    re.compile(r"__(?=\S)(?:(?!\n\n).)+?(?<=\S)__", re.S),
    re.compile(r"~~(?=\S)(?:(?!\n\n).)+?(?<=\S)~~", re.S),
    re.compile(r"(?<![*\w])\*(?![\s*])(?:(?!\n\n)[^*])+?(?<![\s*])\*(?![*\w])"),
    re.compile(r"(?<!\w)_(?![\s_])[^_\n]+?(?<![\s_])_(?!\w)"),
]


@dataclass(frozen=True)
class _Fence:
    start: int         # first char of the opening line
    open_end: int      # just after the opening line (including its newline)
    close_start: int   # first char of the closing line (len(text) when unclosed)
    end: int           # just after the closing line
    opener: str        # "```lang" with indentation stripped
    marker: str        # "```" or "~~~"


def split_markdown(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters on clean Markdown boundaries.

    A chunk can only exceed ``limit`` when ``limit`` is too small to hold a fence's own
    opening and closing lines.
    """
    return ["".join(parts) for parts in split_markdown_parts(text, limit)]


def split_markdown_parts(text: str, limit: int) -> list[tuple[str, str, str]]:
    """Like ``split_markdown`` but returns ``(prefix, body, suffix)`` per chunk.

    ``prefix`` and ``suffix`` are the synthetic fence reopen/close lines; the ``body`` parts
    concatenate back to ``text`` exactly.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [("", text, "")]
    fences = _find_fences(text)
    forbidden = _forbidden_positions(text, fences, limit)
    in_code = _code_body_positions(text, fences)
    chunks: list[tuple[str, str, str]] = []
    start = 0
    while start < len(text):
        inside = _fence_containing(fences, start)
        prefix = f"{inside.opener}\n" if inside else ""
        avail = max(1, limit - len(prefix))
        if len(text) - start <= avail:
            chunks.append((prefix, text[start:], ""))
            break
        cut = _choose_cut(text, start, start + avail, forbidden, in_code)
        fence = _fence_containing(fences, cut)
        if fence is not None and fence.start > start and fence.end - fence.start + 1 <= limit:
            # The block fits in a chunk of its own: keep it whole rather than splitting it.
            cut, fence = fence.start, None
        elif fence is not None:
            # Leave room for the synthetic closing line, then re-evaluate the cut.
            room = start + avail - len(fence.marker) - 1
            cut = _choose_cut(text, start, max(start + 1, room), forbidden, in_code)
            fence = _fence_containing(fences, cut)
        suffix = ""
        if fence is not None:
            suffix = fence.marker if text[cut - 1] == "\n" else f"\n{fence.marker}"
        chunks.append((prefix, text[start:cut], suffix))
        start = cut
    return chunks


def _find_fences(text: str) -> list[_Fence]:
    fences: list[_Fence] = []
    offset = 0
    opened: tuple[int, int, str] | None = None
    for line in text.splitlines(keepends=True):
        line_end = offset + len(line)
        if is_fence_boundary(line) or (opened and line.strip() in {"```", "~~~"}):
            if opened is None:
                opened = (offset, line_end, line.strip())
            else:
                start, open_end, opener = opened
                fences.append(_Fence(start, open_end, offset, line_end, opener, opener[:3]))
                opened = None
        offset = line_end
    if opened is not None:
        start, open_end, opener = opened
        fences.append(_Fence(start, open_end, len(text), len(text), opener, opener[:3]))
    return fences


def _fence_containing(fences: list[_Fence], position: int) -> _Fence | None:
    """The fence whose body a cut at ``position`` would fall into (not its boundary lines)."""
    for fence in fences:
        if fence.open_end < position < fence.close_start:
            return fence
    return None


def _code_body_positions(text: str, fences: list[_Fence]) -> bytearray:
    """Flag positions strictly inside a fence body (where only line/word/hard cuts apply)."""
    flags = bytearray(len(text) + 1)
    for fence in fences:
        for index in range(fence.open_end + 1, fence.close_start):
            flags[index] = 1
    return flags


def _forbidden_positions(text: str, fences: list[_Fence], limit: int) -> bytearray:
    """Mark every cut position that would land inside a protected span."""
    forbidden = bytearray(len(text) + 1)
    masked = list(text)
    for fence in fences:
        for index in range(fence.start + 1, fence.open_end + 1):
            forbidden[index] = 1
        for index in range(fence.close_start, max(fence.close_start, fence.end - 1) + 1):
            if index < fence.end:
                forbidden[index] = 1
        for index in range(fence.start, fence.end):
            masked[index] = "\x00"  # inline syntax inside code is not Markdown
    masked_text = "".join(masked)
    for pattern in _INLINE_SPANS:
        for match in pattern.finditer(masked_text):
            if match.end() - match.start() > limit:
                continue  # cannot be kept whole anyway; do not forbid every boundary inside
            for index in range(match.start() + 1, match.end()):
                forbidden[index] = 1
    return forbidden


def _choose_cut(text: str, start: int, high: int, forbidden: bytearray, in_code: bytearray) -> int:
    """Largest-first search for the best boundary in ``(start, high]``."""
    high = min(high, len(text))
    min_fill = start + max(1, (high - start) // MIN_FILL_RATIO)
    best = 0
    for level in range(4):
        for position in range(high, start, -1):
            if forbidden[position] or not _is_boundary(text, start, position, level, in_code):
                continue
            if position >= min_fill:
                return position
            best = max(best, position)
            break
    if best:
        return best
    return _hard_cut(text, start, high, forbidden)


def _is_boundary(text: str, start: int, position: int, level: int, in_code: bytearray) -> bool:
    before = text[position - 1]
    code = bool(in_code[position])
    if level == 0:
        return not code and position - 2 >= start and text[position - 2:position] == "\n\n"
    if level == 1:
        return before == "\n"
    if level == 2:
        return (
            not code and before.isspace() and position - 2 >= start
            and text[position - 2] in _SENTENCE_END
        )
    return before.isspace()


def _hard_cut(text: str, start: int, high: int, forbidden: bytearray) -> int:
    """Cut at ``high``, stepping back so emoji sequences and combining marks stay intact."""
    position = high
    while position > start + 1 and (forbidden[position] or _glues(text, position)):
        position -= 1
    if position <= start + 1 and (forbidden[position] or _glues(text, position)):
        position = high  # nothing safe: honour the size limit
    return position


def _glues(text: str, position: int) -> bool:
    """True when a cut at ``position`` would split a grapheme (ZWJ sequence, VS16, modifier, mark)."""
    if position >= len(text):
        return False
    after, before = text[position], text[position - 1]
    return (
        before == "‍"
        or after in "‍️︎⃣"
        or "\U0001f3fb" <= after <= "\U0001f3ff"
        or unicodedata.category(after).startswith("M")
        or ("\U0001f1e6" <= before <= "\U0001f1ff" and "\U0001f1e6" <= after <= "\U0001f1ff")
    )
