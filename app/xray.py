"""X-Ray: show players exactly what the LLM saw (SPEC §6.6).

Secrets + the flag are masked inside the SYSTEM message only. User/assistant/tool messages
are shown as-is (tool results unmasked: the impact already happened).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from app.state import Segment

MASK = "████████"
Kind = Literal["system", "user", "assistant", "tool", "untrusted"]
KIND_COLOR: dict[str, str] = {
    "system": "brand",
    "user": "green",
    "assistant": "slate",
    "tool": "violet",
    "untrusted": "red",
}
UNTRUSTED_LABEL = {
    "KB": "⚠️ External data: Knowledge Base",
    "TICKETS": "⚠️ External data: Tickets",
    "INBOX": "⚠️ External data: Inbox",
    "ATTACHMENT": "⚠️ External data: Attachment",
    "PLUGINS": "⚠️ Third-party: Installed plugins",
}


@dataclass
class XRayPart:
    kind: Kind
    text: str
    label: str = ""

    @property
    def color(self) -> str:
        return KIND_COLOR[self.kind]


@dataclass
class XRayMessage:
    role: str
    title: str
    parts: list[XRayPart] = field(default_factory=list)


def mask(text: str, secrets: Sequence[str]) -> str:
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = re.sub(re.escape(s), MASK, text, flags=re.IGNORECASE)
    return text


def build_xray(
    messages: Sequence[dict[str, Any]],
    system_segments: Sequence[Segment] | None,
    secrets: Sequence[str],
) -> list[XRayMessage]:
    out: list[XRayMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "system":
            segs = system_segments or [Segment("system", content)]
            out.append(XRayMessage("system", "SYSTEM PROMPT", [
                XRayPart(s.kind, mask(s.text, secrets), UNTRUSTED_LABEL.get(s.source, "") if s.kind == "untrusted" else "")
                for s in segs
            ]))
        elif role == "user":
            out.append(XRayMessage("user", "USER", [XRayPart("user", content)]))
        elif role == "assistant":
            parts = [XRayPart("assistant", content)] if content else []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                parts.append(XRayPart("tool", f"{fn.get('name')}({fn.get('arguments', '')})", "🔧 TOOL CALL"))
            out.append(XRayMessage("assistant", "ASSISTANT", parts))
        elif role == "tool":
            out.append(XRayMessage("tool", "TOOL RESULT", [XRayPart("tool", content)]))
    return out


def xray_to_json(items: Sequence[XRayMessage]) -> str:
    return json.dumps(
        [{"role": m.role, "title": m.title,
          "parts": [{"kind": p.kind, "color": p.color, "label": p.label, "text": p.text} for p in m.parts]}
         for m in items],
        ensure_ascii=False,
    )
