"""Discord-independent message structures shared by parsers and renderers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Offer:
    number: int
    title: str
    body: str
    job_id: str


@dataclass
class Segment:
    """One ordered unit in a rendered response; unknown segments remain plain text."""
    text: str
    offer: Optional[Offer] = None
    kind: str = "text"
    title: str = ""
    color: Optional[int] = None


@dataclass
class MessageDocument:
    """Parsed response with provenance for rendering and content-free diagnostics."""
    segments: list[Segment]
    source_format: str = "plain"
    job_id: Optional[str] = None
    metadata: dict[str, str] = field(default_factory=dict)
