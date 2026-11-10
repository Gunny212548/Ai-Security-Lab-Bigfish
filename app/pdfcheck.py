"""I3 hidden-text detection (SPEC §6.5) + a small PDF builder used by tests and calibrate.py."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 5
HIDDEN_CHANNEL_MIN = 0xF0  # every RGB channel >= this -> invisible on white paper
HIDDEN_MAX_SIZE = 2.0  # font size below this (pt) -> invisible

# English words match as whole words so ordinary text ("email", "maintain", "somchai")
# does not trip the "ai" rule. Thai has no word spacing, so Thai words match as substrings.
SUSPICIOUS_EN = ("ai", "approve", "approved", "manager", "ignore", "reviewer", "set approved")
SUSPICIOUS_TH = ("อนุมัติ",)  # Thai for "approve"; kept in case a player writes Thai
_SUSPICIOUS_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in sorted(SUSPICIOUS_EN, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


class PdfRejected(ValueError):
    """Upload refused; message is player-facing."""


@dataclass(frozen=True)
class PdfCheck:
    ai_text: str
    human_text: str
    caught_by_human: bool
    hidden_spans_count: int
    pages: int


def _is_hidden(span: dict) -> bool:
    color = int(span.get("color", 0))
    r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    too_light = min(r, g, b) >= HIDDEN_CHANNEL_MIN
    too_small = float(span.get("size", 12)) < HIDDEN_MAX_SIZE
    return too_light or too_small


def find_suspicious(text: str) -> list[str]:
    hits = [m.group(0).lower() for m in _SUSPICIOUS_RE.finditer(text)]
    hits += [w for w in SUSPICIOUS_TH if w in text]
    return hits


def check_pdf(data: bytes) -> PdfCheck:
    if len(data) > MAX_BYTES:
        raise PdfRejected("File is larger than 2 MB")
    if not data.startswith(b"%PDF"):
        raise PdfRejected("Only PDF files are supported")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise PdfRejected("Could not open the PDF file") from e
    with doc:
        if doc.page_count > MAX_PAGES:
            raise PdfRejected(f"PDF must be at most {MAX_PAGES} pages")
        ai_lines: list[str] = []
        human_lines: list[str] = []
        hidden = 0
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    ai_parts, human_parts = [], []
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if not text:
                            continue
                        ai_parts.append(text)
                        if _is_hidden(span):
                            if text.strip():
                                hidden += 1
                        else:
                            human_parts.append(text)
                    if ai_parts:
                        ai_lines.append("".join(ai_parts))
                    if human_parts:
                        human_lines.append("".join(human_parts))
        pages = doc.page_count

    human_text = "\n".join(human_lines).strip()
    return PdfCheck(
        ai_text="\n".join(ai_lines).strip(),
        human_text=human_text,
        caught_by_human=bool(find_suspicious(human_text)),
        hidden_spans_count=hidden,
        pages=pages,
    )


# --- builder (tests + calibration; also handy for organisers) --------------------

_THAI_RE = re.compile(r"[฀-๿]+")
_THAI_SCRIPT = 19  # MuPDF/UCDN script code for Thai -> built-in Noto Serif Thai


def _runs(text: str) -> list[tuple[bool, str]]:
    """Split into (is_thai, chunk) runs. Noto Serif Thai has no Latin glyphs, Helvetica no Thai."""
    out, pos = [], 0
    for m in _THAI_RE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def build_pdf(
    visible_text: str,
    hidden_text: str = "",
    *,
    hidden_color: tuple[float, float, float] = (1, 1, 1),
    hidden_size: float = 1.0,
    visible_size: float = 12.0,
) -> bytes:
    """One A4 page: visible black lines, then optional hidden lines (white / tiny by default)."""
    fonts = {True: pymupdf.Font(script=_THAI_SCRIPT), False: pymupdf.Font("helv")}
    doc = pymupdf.open()
    page = doc.new_page()
    left, right = 56.0, page.rect.width - 56.0
    y = 72.0

    def write(text: str, size: float, color) -> None:
        nonlocal y
        tw = pymupdf.TextWriter(page.rect)
        for raw_line in text.splitlines() or [""]:
            x = left
            for is_thai, chunk in _runs(raw_line):
                font = fonts[is_thai]
                for ch in chunk:  # char-level wrap keeps this simple and works for Thai
                    w = font.text_length(ch, fontsize=size)
                    if x + w > right:
                        x, y = left, y + size * 1.4
                    tw.append((x, y), ch, font=font, fontsize=size)
                    x += w
            y += size * 1.4
        tw.write_text(page, color=color)

    write(visible_text, visible_size, (0, 0, 0))
    if hidden_text:
        y += visible_size
        write(hidden_text, hidden_size, hidden_color)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data
