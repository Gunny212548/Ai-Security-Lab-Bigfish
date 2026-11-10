"""Tiny, safe Markdown subset for story/lesson text and chat bubbles.

Everything is HTML-escaped FIRST, then a few patterns are turned into tags, so LLM output
(attacker-controlled) can never inject markup. Supported: paragraphs, line breaks, `- ` lists,
`#`..`###` headings, pipe tables, **bold**, *italic*, `code`.
"""

from __future__ import annotations

import json
import re

from markupsafe import Markup, escape

_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_LIST = re.compile(r"^\s*[-*]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(escaped_line: str) -> str:
    # code first so ** inside `code` stays literal
    parts = _CODE.split(escaped_line)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f'<code class="rounded bg-slate-800 px-1">{part}</code>')
        else:
            part = _BOLD.sub(r"<strong>\1</strong>", part)
            out.append(_ITALIC.sub(r"<em>\1</em>", part))
    return "".join(out)


def _cells(escaped_row: str) -> list[str]:
    return [_inline(c.strip()) for c in escaped_row.strip().strip("|").split("|")]


def _table(rows: list[str]) -> str:
    head, body = rows[0], [r for r in rows[2:]]
    th = "".join(f'<th class="border border-slate-600 px-2 py-1 text-left">{c}</th>' for c in _cells(head))
    trs = "".join("<tr>" + "".join(f'<td class="border border-slate-700 px-2 py-1 align-top">{c}</td>' for c in _cells(r))
                  + "</tr>" for r in body)
    return ('<div class="mb-2 overflow-x-auto last:mb-0"><table class="w-full border-collapse text-xs">'
            f"<thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>")


def md_inline(text: str | None) -> Markup:
    """One line of inline formatting, no block tags (safe inside <p>)."""
    return Markup(_inline(str(escape(" ".join((text or "").split())))))


def md(text: str | None) -> Markup:
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            blocks.append('<p class="mb-2 last:mb-0">' + "<br>".join(para) + "</p>")
            para.clear()
        if items:
            blocks.append('<ul class="mb-2 ml-5 list-disc last:mb-0">' + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    lines = [str(escape(raw.rstrip())) for raw in (text or "").splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        # pipe table: header row + separator row (+ body rows)
        if _TABLE_ROW.match(line) and i < len(lines) and _TABLE_SEP.match(lines[i]):
            rows = [line, lines[i]]
            i += 1
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i])
                i += 1
            flush()
            blocks.append(_table(rows))
            continue
        if not line.strip():
            flush()
            continue
        if m := _HEADING.match(line):
            flush()
            blocks.append(f'<p class="mb-1 font-semibold">{_inline(m.group(2))}</p>')
        elif m := _LIST.match(line):
            if para:
                flush()
            items.append(_inline(m.group(1)))
        else:
            if items:
                flush()
            para.append(_inline(line))
    flush()
    return Markup("".join(blocks))


def parse_json(text: str | None) -> dict | list | None:
    """The parsed object/array if `text` is JSON, else None (templates render AI results from it)."""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, (dict, list)) else None


def pretty_json(text: str | None) -> str | None:
    """Indented JSON if `text` parses as an object/array, else None (caller shows the raw text)."""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    return json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, (dict, list)) else None
