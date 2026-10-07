"""Prepare Markdown before Hermes turns table row labels into bold headings."""
from __future__ import annotations

import re

_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$")


def _cells(line: str) -> list[tuple[int, int]]:
    """Find cell bounds without treating escaped or inline-code pipes as separators."""
    bounds = []
    start = 0
    code_ticks = 0
    index = 0
    while index < len(line):
        if line[index] == "\\":
            index += 2
            continue
        if line[index] == "`":
            end = index + 1
            while end < len(line) and line[end] == "`":
                end += 1
            ticks = end - index
            if not code_ticks:
                code_ticks = ticks
            elif ticks == code_ticks:
                code_ticks = 0
            index = end
            continue
        if line[index] == "|" and not code_ticks:
            bounds.append((start, index))
            start = index + 1
        index += 1
    bounds.append((start, len(line)))
    if len(bounds) > 1 and not line[bounds[0][0]:bounds[0][1]].strip():
        bounds = bounds[1:]
    if len(bounds) > 1 and not line[bounds[-1][0]:bounds[-1][1]].strip():
        bounds = bounds[:-1]
    return bounds


def prepare_adapter_markdown(content: str) -> str:
    """Unwrap whole-cell strong emphasis only where Hermes adds it itself.

    Leave prose, headers, non-label cells, inline code and fences untouched.
    Avoid repairing arbitrary malformed emphasis after the adapter has run.
    """
    lines = content.splitlines(keepends=True)
    fence = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not line[marker.end():].strip():
                fence = None
            index += 1
            continue
        if fence is not None or index + 2 >= len(lines) or not _SEPARATOR.match(lines[index + 1]):
            index += 1
            continue
        headers = _cells(line)
        if len(headers) < 2:
            index += 1
            continue
        index += 2
        first_cells = _cells(lines[index])
        has_row_label = len(first_cells) == len(headers) + 1
        while index < len(lines) and "|" in lines[index]:
            row = lines[index]
            cells = _cells(row)
            heading = cells[0] if has_row_label and cells else next(
                ((start, end) for start, end in cells if row[start:end].strip()), None,
            )
            if heading:
                start, end = heading
                value = row[start:end]
                stripped = value.strip()
                for delimiter in ("**", "__"):
                    if (stripped.startswith(delimiter) and stripped.endswith(delimiter)
                            and len(stripped) > 4
                            and not stripped.startswith(delimiter + delimiter[0])
                            and not stripped.endswith(delimiter[0] + delimiter)
                            and delimiter not in stripped[2:-2]):
                        leading = len(value) - len(value.lstrip())
                        trailing = len(value) - len(value.rstrip())
                        value = value[:leading] + stripped[2:-2] + (value[-trailing:] if trailing else "")
                        lines[index] = row[:start] + value + row[end:]
                        break
            index += 1
    return "".join(lines)
