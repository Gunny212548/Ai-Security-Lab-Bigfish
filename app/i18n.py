"""UI language switch (English / Thai). The chat itself is never translated: chat bubbles, AI replies, system prompts,
tool lines and X-Ray contents stay as they are.

The English text is the key: `t("Submit flag")` (Python) and `{{ _("Submit flag") }}` (templates) return the Thai
text when the viewer picked Thai and a translation exists, else the English. Challenge content (title, story,
objective, hints, lesson) is translated in the YAML with `*_th` fields; see `loc()`.

The language comes from the `lang` cookie, set by GET /lang/<code>; LanguageMiddleware puts it in a context variable
for the length of each request.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from markupsafe import Markup, escape
from starlette.requests import HTTPConnection

LANGS = {"en": "EN", "th": "ไทย"}
DEFAULT_LANG = "en"
COOKIE = "lang"
COOKIE_MAX_AGE = 365 * 24 * 3600

_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT_LANG)


def normalize(code: str | None) -> str:
    return code if code in LANGS else DEFAULT_LANG


def get_lang() -> str:
    return _lang.get()


def _lookup(text: str) -> str:
    return TH.get(text, text) if get_lang() == "th" else text


def t(text: str, **kw: Any) -> str:
    """Plain text (server messages, toasts)."""
    s = _lookup(text)
    return s.format(**kw) if kw else s


def gettext_markup(text: str, **kw: Any) -> Markup:
    """For templates: the translation is escaped, Markup arguments (links, <strong>) are kept, others escaped."""
    s = Markup(escape(_lookup(text)))
    return s.format(**kw) if kw else s


def loc(obj: Any, field: str) -> Any:
    """A challenge field in the viewer's language: `<field>_th` when Thai is on and the YAML has it."""
    get = obj.get if isinstance(obj, dict) else lambda k, d=None: getattr(obj, k, d)
    if get_lang() == "th":
        th = get(f"{field}_th")
        if th:
            return th
    return get(field)


class LanguageMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware), so the context variable reaches the endpoint and its templates."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = _lang.set(normalize(HTTPConnection(scope).cookies.get(COOKIE)))
        try:
            await self.app(scope, receive, send)
        finally:
            _lang.reset(token)


# --- Thai -------------------------------------------------------------------------------------------------------------

TH: dict[str, str] = {
    # header / nav
    "Challenges": "โจทย์",
    "🏆 Scoreboard": "🏆 กระดานคะแนน",
    "Share this code with your teammates": "แชร์โค้ดนี้ให้เพื่อนร่วมทีม",
    "Join code": "โค้ดเข้าทีม",
    "{n} pts": "{n} แต้ม",
    "{n} pts earned": "ได้ {n} แต้ม",
    "Leave": "ออกจากทีม",
    "Switch light / dark theme": "สลับธีมสว่าง / มืด",
    "Language": "ภาษา",
    "Something went wrong ({status}). Try again.": "เกิดข้อผิดพลาด ({status}) ลองใหม่อีกครั้ง",
    "Can't reach the server. Check your Wi-Fi and try again.": "ติดต่อเซิร์ฟเวอร์ไม่ได้ ตรวจสอบ Wi-Fi แล้วลองใหม่",
    "The server took too long to answer. Try again.": "เซิร์ฟเวอร์ตอบช้าเกินไป ลองใหม่อีกครั้ง",
    # join
    "Join": "เข้าร่วม",
    "A hands-on prompt injection CTF. Trick {bot}, the company's AI helpdesk, into leaking secrets and taking actions it shouldn't.":
        "CTF ฝึกโจมตีแบบ prompt injection หลอก {bot} AI helpdesk ของบริษัท ให้เผยความลับและทำสิ่งที่ไม่ควรทำ",
    "Create a team": "สร้างทีม",
    "One person creates the team, then shares the join code.": "ให้หนึ่งคนสร้างทีม แล้วแชร์โค้ดเข้าทีมให้เพื่อน",
    "Team name": "ชื่อทีม",
    "Create team": "สร้างทีม",
    "Join a team": "เข้าร่วมทีม",
    "Enter the 6-character code your teammate sees in the header.": "ใส่โค้ด 6 ตัวที่เพื่อนร่วมทีมเห็นบนแถบด้านบน",
    "Join team": "เข้าทีม",
    "Team name must be 1–{n} characters.": "ชื่อทีมต้องยาว 1–{n} ตัวอักษร",
    "That team name is taken. Ask your teammates for the join code.": "ชื่อทีมนี้มีคนใช้แล้ว ขอโค้ดเข้าทีมจากเพื่อนร่วมทีม",
    "No team has that join code.": "ไม่มีทีมที่ใช้โค้ดนี้",
    # board
    "{total} challenges covering the {owasp} — find flags in the format {fmt} and submit them to score points":
        "โจทย์ {total} ข้อ ครอบคลุม {owasp} — หา flag รูปแบบ {fmt} แล้วส่งเพื่อเก็บแต้ม",
    "OWASP Top 10 for LLM Applications (2025)": "OWASP Top 10 for LLM Applications (2025)",
    "🎯 Direct Prompt Injection": "🎯 Direct Prompt Injection (โจมตีตรง)",
    "Talk to BigfishDesk directly and trick it into doing what it shouldn't.":
        "คุยกับ BigfishDesk โดยตรง แล้วหลอกให้มันทำสิ่งที่ไม่ควรทำ",
    "🕵️ Indirect Prompt Injection": "🕵️ Indirect Prompt Injection (โจมตีทางอ้อม)",
    "Hide instructions in data the AI reads, then wait for an employee to use it.":
        "ซ่อนคำสั่งไว้ในข้อมูลที่ AI อ่าน แล้วรอให้พนักงานใช้งาน AI",
    "🔒 Waiting for the organizers to unlock": "🔒 รอผู้จัดปลดล็อก",
    "Difficulty {n}": "ความยาก {n}",
    # challenge page
    "← Challenges": "← โจทย์ทั้งหมด",
    "✅ Solved": "✅ แก้ได้แล้ว",
    "🎯 Objective:": "🎯 เป้าหมาย:",
    "Submit flag": "ส่ง flag",
    "Submit": "ส่ง",
    "🗨 New chat": "🗨 เริ่มแชทใหม่",
    "Starts a fresh conversation with BigfishDesk for your whole team. Score and hints are kept.":
        "เริ่มบทสนทนาใหม่กับ BigfishDesk ให้ทั้งทีม แต้มและคำใบ้ยังอยู่ครบ",
    "Hints": "คำใบ้",
    "Solved: hints are free now.": "แก้ได้แล้ว: ดูคำใบ้ได้ฟรี",
    "Each hint lowers what this challenge is worth to your team. Open them in order.":
        "คำใบ้แต่ละข้อจะลดแต้มของโจทย์นี้สำหรับทีมคุณ ต้องเปิดตามลำดับ",
    "Hint {n} costs {cost} points on this challenge ({worth} → {after} pts). Show it?":
        "คำใบ้ข้อ {n} หัก {cost} แต้มจากโจทย์นี้ ({worth} → {after} แต้ม) ต้องการเปิดไหม?",
    "💡 Show hint {n}": "💡 เปิดคำใบ้ข้อ {n}",
    "−{n} pts": "−{n} แต้ม",
    "free": "ฟรี",
    "🔒 Hint {n}": "🔒 คำใบ้ข้อ {n}",
    "open hint {n} first": "เปิดคำใบ้ข้อ {n} ก่อน",
    "Open hint {n} first": "เปิดคำใบ้ข้อ {n} ก่อน",
    "📘 Lesson": "📘 บทเรียน",
    "📚 OWASP Top 10 for LLM 2025:": "📚 OWASP Top 10 for LLM 2025:",
    "✅ Correct! +{n} pts": "✅ ถูกต้อง! +{n} แต้ม",
    "(of {n}, after hints)": "(จาก {n} หลังหักคำใบ้)",
    "✅ Correct, and your team already solved this one.": "✅ ถูกต้อง และทีมคุณแก้ข้อนี้ไปแล้ว",
    "That flag only exists for AI agents. The organizers can see this submission.":
        "flag นี้มีไว้สำหรับ AI agent เท่านั้น ผู้จัดจะเห็นการส่งครั้งนี้",
    "❌ That's not the flag. Keep trying!": "❌ ยังไม่ใช่ flag ลองต่อนะ!",
    "Paste a flag first.": "วาง flag ก่อน",
    # X-Ray chrome (its contents are not translated)
    "— exactly what the AI received on your last message": "— สิ่งที่ AI ได้รับจริงในข้อความล่าสุดของคุณ",
    "— what the AI received in the victim's session": "— สิ่งที่ AI ได้รับในเซสชันของเหยื่อ",
    "untrusted data": "ข้อมูลที่เชื่อถือไม่ได้",
    "secrets shown as ████████": "ความลับแสดงเป็น ████████",
    "Send a message to see the full prompt BigfishDesk works with.":
        "ส่งข้อความเพื่อดู prompt ทั้งหมดที่ BigfishDesk ใช้งาน",
    "Run the employee to see the full prompt, including your planted data.":
        "ให้พนักงานใช้ AI เพื่อดู prompt ทั้งหมด รวมถึงข้อมูลที่คุณฝังไว้",
    # indirect page
    "🕵️ Your side": "🕵️ ฝั่งคุณ",
    "— plant your payload in the data BigfishDesk reads": "— ฝัง payload ไว้ในข้อมูลที่ BigfishDesk อ่าน",
    "You": "คุณ",
    "{name} is about to ask BigfishDesk:": "{name} กำลังจะถาม BigfishDesk ว่า:",
    "▶ Let {name} use the AI": "▶ ให้ {name} ใช้ AI",
    "BigfishDesk is working": "BigfishDesk กำลังทำงาน",
    "🖥 Victim POV": "🖥 มุมมองเหยื่อ",
    "— {name}'s screen": "— หน้าจอของ {name}",
    "Nothing yet. Plant your payload, then run the employee.": "ยังไม่มีอะไร ฝัง payload ก่อน แล้วให้พนักงานใช้ AI",
    "The AI was not called on this run.": "รอบนี้ไม่ได้เรียก AI",
    # attacker: wiki
    "✏️ Anyone at Bigfish can edit": "✏️ ทุกคนใน Bigfish แก้ไขได้",
    "IT Help · visible to all staff": "IT Help · พนักงานทุกคนเห็น",
    "Article text": "เนื้อหาบทความ",
    "Changes go live immediately": "การแก้ไขมีผลทันที",
    "Save page": "บันทึกหน้า",
    "Saved “{title}” ✅": "บันทึก “{title}” แล้ว ✅",
    "Article is longer than {n} characters": "บทความยาวเกิน {n} ตัวอักษร",
    "Article not found in the KB": "ไม่พบบทความนี้ใน KB",
    # attacker: tickets
    "Open tickets · waiting for triage": "ทิกเก็ตที่เปิดอยู่ · รอจัดลำดับ",
    "ID": "ID",
    "Subject": "หัวข้อ",
    "Requester": "ผู้แจ้ง",
    "(yours)": "(ของคุณ)",
    "Update your request": "แก้ไขคำขอของคุณ",
    "New request": "คำขอใหม่",
    "Category: Hardware › Mouse & keyboard": "หมวด: ฮาร์ดแวร์ › เมาส์และคีย์บอร์ด",
    "Priority: set by AI Triage": "ความสำคัญ: AI Triage เป็นผู้กำหนด",
    "Subject (must be about your mouse)": "หัวข้อ (ต้องเกี่ยวกับเมาส์ของคุณ)",
    "Description": "รายละเอียด",
    "Submit request": "ส่งคำขอ",
    "Ticket submitted ✅": "ส่งทิกเก็ตแล้ว ✅",
    "Title max {title} and description max {desc} characters": "หัวข้อยาวได้ไม่เกิน {title} และรายละเอียดไม่เกิน {desc} ตัวอักษร",
    "The ticket title must be about a mouse (include the word 'mouse')": "หัวข้อทิกเก็ตต้องเกี่ยวกับเมาส์ (มีคำว่า 'mouse')",
    # attacker: PDF
    "Request type": "ประเภทคำขอ",
    "Local Admin rights (this laptop)": "สิทธิ์ Local Admin (แล็ปท็อปเครื่องนี้)",
    "Requested by": "ผู้ขอ",
    "Approval": "การอนุมัติ",
    "① IT Staff opens the PDF → ② AI reviewer approves or rejects": "① เจ้าหน้าที่ IT เปิดดู PDF → ② AI ผู้ตรวจอนุมัติหรือปฏิเสธ",
    "Status": "สถานะ",
    "Waiting for review": "รอตรวจ",
    "Draft · attachment missing": "ฉบับร่าง · ยังไม่มีไฟล์แนบ",
    "Current attachment:": "ไฟล์แนบปัจจุบัน:",
    "Attach your Access Request (PDF)": "แนบคำขอสิทธิ์ของคุณ (PDF)",
    "Max {mb} MB, 5 pages": "ไม่เกิน {mb} MB, 5 หน้า",
    "Upload PDF": "อัปโหลด PDF",
    "Attached “{name}” ✅ IT Staff will review it.": "แนบ “{name}” แล้ว ✅ เจ้าหน้าที่ IT จะตรวจดู",
    "File is larger than 2 MB": "ไฟล์ใหญ่เกิน 2 MB",
    "Only PDF files are supported": "รองรับเฉพาะไฟล์ PDF",
    "Could not open the PDF file": "เปิดไฟล์ PDF ไม่ได้",
    "No attachment yet — upload a PDF before IT Staff can review it": "ยังไม่มีไฟล์แนบ — อัปโหลด PDF ก่อนเจ้าหน้าที่ IT จึงจะตรวจได้",
    "❌ IT Staff opened the file and spotted something odd — request rejected": "❌ เจ้าหน้าที่ IT เปิดไฟล์แล้วเห็นสิ่งผิดปกติ — คำขอถูกปฏิเสธ",
    # attacker: plugin
    "Installed at": "ติดตั้งที่",
    "⟳ Auto-update on": "⟳ อัปเดตอัตโนมัติเปิดอยู่",
    "by {vendor}": "โดย {vendor}",
    "your plugin": "ปลั๊กอินของคุณ",
    "Usage notes (shipped in the next update)": "คำอธิบายการใช้งาน (ส่งไปกับอัปเดตครั้งถัดไป)",
    "Customers on auto-update get it right away": "ลูกค้าที่เปิดอัปเดตอัตโนมัติจะได้รับทันที",
    "Publish update": "เผยแพร่อัปเดต",
    "🔒 not yours": "🔒 ไม่ใช่ของคุณ",
    "Update to “{title}” published ✅ It auto-installs at Siam Bigfish Corp.":
        "เผยแพร่อัปเดต “{title}” แล้ว ✅ จะติดตั้งอัตโนมัติที่ Siam Bigfish Corp",
    "Usage notes are longer than {n} characters": "คำอธิบายการใช้งานยาวเกิน {n} ตัวอักษร",
    "Plugin not found in the Plugin Hub": "ไม่พบปลั๊กอินนี้ใน Plugin Hub",
    # victim result chrome (the AI's own text is not translated)
    "Queue after AI Triage": "คิวหลัง AI Triage จัดลำดับ",
    "applied automatically": "ใช้ผลอัตโนมัติ",
    "✅ Approved": "✅ อนุมัติ",
    "❌ Rejected": "❌ ปฏิเสธ",
    "AI reviewer · Local Admin rights for Somchai": "AI ผู้ตรวจ · สิทธิ์ Local Admin ของ Somchai",
    "AI result": "ผลจาก AI",
    "View raw JSON · BigfishDesk returned JSON:": "ดู JSON ดิบ · BigfishDesk ส่ง JSON กลับมา:",
    "🎉 Flag unlocked!": "🎉 ได้ flag แล้ว!",
    "Submit this flag to score:": "ส่ง flag นี้เพื่อรับแต้ม:",
    "🎯 Impact detected!": "🎯 โจมตีสำเร็จ!",
    "The employee's AI did what you wanted. Find the flag, then submit it.":
        "AI ของพนักงานทำตามที่คุณต้องการแล้ว หา flag แล้วส่งเลย",
    "👁 What IT Staff sees": "👁 สิ่งที่เจ้าหน้าที่ IT เห็น",
    "(empty page)": "(หน้าว่าง)",
    "🤖 What the AI sees": "🤖 สิ่งที่ AI เห็น",
    "(no text)": "(ไม่มีข้อความ)",
    # scoreboard
    "Scoreboard": "กระดานคะแนน",
    "Ties go to the team that reached the score first. 🩸 = first team to solve that challenge.":
        "ถ้าแต้มเท่ากัน ทีมที่ทำแต้มถึงก่อนได้อันดับดีกว่า 🩸 = ทีมแรกที่แก้ข้อนั้นได้",
    "Updates every {n}s": "อัปเดตทุก {n} วินาที",
    "No teams yet. Create one on the {link}.": "ยังไม่มีทีม สร้างทีมได้ที่{link}",
    "join page": "หน้าเข้าร่วม",
    "Team": "ทีม",
    "Score": "แต้ม",
    "(you)": "(คุณ)",
    "First blood": "ทีมแรกที่แก้ได้",
    "Solved": "แก้ได้แล้ว",
    # errors
    "Back to challenges": "กลับไปหน้าโจทย์",
    "Join page": "หน้าเข้าร่วม",
    "That request didn't make sense to the server.": "เซิร์ฟเวอร์ไม่เข้าใจคำขอนี้",
    "Your session ended. Join your team again.": "เซสชันหมดอายุ เข้าทีมใหม่อีกครั้ง",
    "That page doesn't exist.": "ไม่มีหน้านี้",
    "That action isn't allowed here.": "ทำแบบนี้ที่หน้านี้ไม่ได้",
    "That upload is too large.": "ไฟล์ที่อัปโหลดใหญ่เกินไป",
    "Something broke on our side. Try again; if it keeps happening, tell an organizer.":
        "ระบบฝั่งเราขัดข้อง ลองใหม่อีกครั้ง ถ้ายังเป็นอยู่ให้แจ้งผู้จัด",
}
