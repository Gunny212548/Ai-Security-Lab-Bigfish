"""EN | ไทย UI switch (app/i18n.py). Only the UI is translated; the chat never is."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import i18n
from app.challenges import Challenge
from app.main import app

from .fakellm import call, text
from .test_app import new_team, set_indirect, use_llm

APP = Path(__file__).resolve().parent.parent / "app"
# _("...") / t("...") with a literal first argument, in templates and Python
_LITERAL_RE = re.compile(r"""\b(?:_|t)\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def thai(c: TestClient) -> None:
    r = c.get("/lang/th", follow_redirects=False)
    assert r.status_code == 303 and c.cookies.get(i18n.COOKIE) == "th"


def test_every_ui_string_has_a_thai_translation():
    missing = []
    files = [*APP.glob("templates/*.html"), *APP.glob("*.py")]
    for f in files:
        if f.name in ("i18n.py",):
            continue
        for m in _LITERAL_RE.finditer(f.read_text(encoding="utf-8")):
            key = (m.group(1) if m.group(1) is not None else m.group(2)).replace('\\"', '"').replace("\\'", "'")
            if key not in i18n.TH:
                missing.append(f"{f.name}: {key}")
    assert not missing, "\n".join(missing)


def test_translations_keep_their_placeholders():
    for en, th in i18n.TH.items():
        assert set(re.findall(r"{(\w+)}", en)) == set(re.findall(r"{(\w+)}", th)), en


def test_lang_route_sets_cookie_and_only_redirects_inside_the_site(client):
    r = client.get("/lang/th?next=/board", follow_redirects=False)
    assert r.headers["location"] == "/board" and client.cookies.get("lang") == "th"
    for bad in ("//evil.lab/x", "https://evil.lab", "evil"):
        assert client.get(f"/lang/en?next={bad}", follow_redirects=False).headers["location"] == "/"
    client.get("/lang/xx", follow_redirects=False)
    assert client.cookies.get("lang") == "en"  # unknown code falls back to English


def test_english_is_the_default(client):
    new_team(client)
    html = client.get("/board").text
    assert '<html lang="en"' in html and "Challenges" in html and "The CEO&#39;s Ticket" in html
    assert 'data-lang="th"' in html and 'data-lang="en"' in html


def test_thai_board_and_challenge_page(client):
    new_team(client)
    thai(client)
    board = client.get("/board").text
    assert '<html lang="th"' in board and "โจทย์" in board and "ทิกเก็ตของ CEO" in board
    assert "หารหัสผ่านชั่วคราวที่ IT ให้ CEO" in board  # objective_th
    page = client.get("/c/d5").text
    assert "ทิกเก็ตเครื่องพิมพ์ของคุณคือ" in page and "ส่ง flag" in page and "เปิดคำใบ้ข้อ 1" in page
    hint = client.post("/c/d5/hint/0").text
    assert "ทิกเก็ตของคุณคือ #2002" in hint  # hints_th


def test_thai_does_not_touch_the_chat(client):
    new_team(client)
    thai(client)
    page = client.get("/c/d5").text
    # the chat window keeps its English UI
    assert "Message BigfishDesk…" in page and "I'm BigfishDesk" in page
    use_llm(client, call("get_ticket", {"ticket_id": "2002"}), text("Your ticket #2002 is open."))
    html = client.post("/c/d5/chat", data={"message": "check my ticket"}).text
    assert "Your ticket #2002 is open." in html and "BigfishDesk used" in html and "AI assistant" in html
    # the model got the English system prompt, whatever the UI language
    assert "You are BigfishDesk" in html  # X-Ray shows the prompt as-is


def test_thai_indirect_panels(client):
    new_team(client)
    thai(client)
    set_indirect(True)
    try:
        page = client.get("/c/i1").text
        assert "ฝั่งคุณ" in page and "บันทึกหน้า" in page and "ให้ Nok ใช้ AI" in page and "KB อาบยาพิษ" in page
        r = client.post("/c/i1/kb", data={"article_id": "nope", "body": "x"})
        assert "ไม่พบบทความนี้ใน KB" in r.text  # server message translated
    finally:
        set_indirect(False)


def test_thai_scoreboard_and_errors(client):
    thai(client)
    assert "กระดานคะแนน" in client.get("/scoreboard").text
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "ไม่มีหน้านี้" in r.text and "กลับไปหน้าโจทย์" in r.text
    r = client.post("/team/join", data={"code": "ZZZZZZ"})
    assert "ไม่มีทีมที่ใช้โค้ดนี้" in r.text


def test_toast_strings_are_plain_json_not_html(client):
    import json
    thai(client)
    html = client.get("/").text
    assert json.dumps("ติดต่อเซิร์ฟเวอร์ไม่ได้ ตรวจสอบ Wi-Fi แล้วลองใหม่") in html  # JSON string, JS decodes it
    client.get("/lang/en")
    html = client.get("/").text
    assert json.dumps("Can't reach the server.")[1:-1].replace("'", "\\u0027") in html  # Jinja tojson escape
    assert "Can&#39;t reach" not in html  # not HTML-escaped inside the script


def test_hints_th_must_match_hints(challenges):
    data = challenges["d5"].model_dump()
    data["hints_th"] = ["only one"] * (len(data["hints"]) + 1)
    with pytest.raises(ValueError, match="hints_th must have one entry per hint"):
        Challenge.model_validate(data)


def test_loc_falls_back_to_english(challenges):
    ch = challenges["d5"].model_copy(update={"story_th": None})
    token = i18n._lang.set("th")
    try:
        assert i18n.loc(ch, "story") == ch.story and i18n.loc(ch, "title") == ch.title_th
        assert i18n.t("Not a known string") == "Not a known string"
    finally:
        i18n._lang.reset(token)
