"""Input blocklist + output redaction (SPEC §6.2). Both are intentionally naive."""

from __future__ import annotations

import re

from app.challenges import BlocklistFilter, RedactFilter

BLOCKED_INPUT_MESSAGE = "🚫 [BLOCKED by Input Filter]"

# zero-width space/joiners, word joiner, BOM, soft hyphen
_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿­]")


def normalize_input(text: str) -> str:
    return _ZERO_WIDTH_RE.sub("", text).lower()


def is_input_blocked(flt: BlocklistFilter | None, text: str) -> bool:
    """True if any blocklisted word is a substring of the normalized input.

    INTENTIONALLY VULNERABLE (challenge D4): keyword matching does not understand intent,
    synonyms, other languages or role-play. Caller must not reveal which word matched.
    """
    if flt is None:
        return False
    norm = normalize_input(text)
    return any(normalize_input(w) in norm for w in flt.words if w)


def apply_output_filter(flt: RedactFilter | None, content: str) -> tuple[str, bool]:
    """Return (content_to_show, was_redacted).

    INTENTIONALLY VULNERABLE (challenge D3): case-insensitive exact substring match only,
    no normalization - spacing, reversing or encoding the secret bypasses it.
    """
    if flt is None or not content:
        return content, False
    if flt.needle.lower() in content.lower():
        return flt.message, True
    return content, False
