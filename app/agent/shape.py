"""Reshape a web-surface answer for WhatsApp.

R2D2 writes for a screen that renders markdown and scrolls. WhatsApp renders
almost none of it, and the house style is six lines before the reader has to
scroll (spec §1.4). Left alone, a good answer arrives as a wall of asterisks.

Pure functions, no network, so the rules are testable and the failure mode is a
visibly wrong string rather than a silent truncation mid-number.
"""

from __future__ import annotations

import re

MAX_LINES = 8
MAX_CHARS = 900

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S)
_CODE_FENCE = re.compile(r"```[\w-]*\n?")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.M)

# Never appears in real text; stripped before the string leaves this module.
_BOLD_MARK = "\x00"


def shape(text: str, *, max_lines: int = MAX_LINES, max_chars: int = MAX_CHARS) -> str:
    """Markdown to plain text, then trimmed to something readable on a phone."""
    if not text:
        return ""

    out = _CODE_FENCE.sub("", text)
    out = _HEADING.sub("", out)
    out = _LINK.sub(r"\1", out)
    # WhatsApp marks bold with single asterisks, so ** becomes * rather than
    # being stripped: the emphasis the author intended still lands. Parked on a
    # sentinel first, because a bare * written straight out is indistinguishable
    # from markdown italics and the next rule would eat it.
    out = _BOLD.sub(f"{_BOLD_MARK}\\1{_BOLD_MARK}", out)
    out = _ITALIC.sub(r"_\1_", out)
    out = out.replace(_BOLD_MARK, "*")
    out = _BULLET.sub("• ", out)
    out = _TRAILING_WS.sub("", out)
    out = _BLANK_RUN.sub("\n\n", out).strip()

    lines = [ln for ln in out.split("\n")]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    out = "\n".join(lines).strip()

    return _truncate(out, max_chars)


def _truncate(text: str, limit: int) -> str:
    """Cut at a sentence boundary when there is one, never mid-number.

    A figure sliced in half is the one truncation this system must not produce:
    "₹29,08" is not a shortened number, it is a wrong one.
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    for stop in (". ", ".\n", "! ", "?\n", "? "):
        cut = window.rfind(stop)
        if cut > limit * 0.5:
            return window[: cut + 1].strip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip(" ,.-") + "…"
