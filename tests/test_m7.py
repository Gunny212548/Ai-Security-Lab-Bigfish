"""M7: friendly error pages, HTMX error text, queue indicator, pre-warm script."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app import state
from app.main import app
from scripts import prewarm

from .conftest import ACTIVE_IDS


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/team/create", data={"name": f"m7-{uuid.uuid4().hex[:8]}"})
        yield c


def test_404_is_a_friendly_page_not_json(client):
    r = client.get("/no/such/page")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert 'data-error-page="404"' in r.text and "That page doesn" in r.text and '"detail"' not in r.text


def test_htmx_errors_are_short_plain_text(client):
    r = client.post("/c/nope/submit", data={"flag": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/plain") and "<" not in r.text
    r = client.post("/c/d5/kb", data={"article_id": "kb-pw", "body": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 400 and "d5 has no kb_edit surface" in r.text


def test_bad_parameters_give_400_page(client):
    r = client.post("/c/d5/hint/not-a-number")
    assert r.status_code == 400 and 'data-error-page="400"' in r.text


def test_redirects_still_work(client):
    client.cookies.clear()
    r = client.get("/board", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.post("/c/d5/submit", data={"flag": "x"}, headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/"


def test_crash_shows_500_page_without_traceback(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internals bigfish{x}")

    monkeypatch.setattr(state, "get_transcript", boom)
    with TestClient(app, raise_server_exceptions=False, cookies=client.cookies) as c:
        r = c.get("/c/d5")
        assert r.status_code == 500 and 'data-error-page="500"' in r.text
        assert "secret internals" not in r.text and "Traceback" not in r.text
        r = c.get("/c/d5", headers={"HX-Request": "true"})
        assert r.status_code == 500 and "Something broke" in r.text and "secret" not in r.text


def test_queue_indicator(client):
    assert client.get("/queue").text == ""
    client.app.state.limiter.in_flight = 1  # only your own request: nothing to say
    assert client.get("/queue").text == ""
    client.app.state.limiter.in_flight = 3
    try:
        assert client.get("/queue").text == "· 3 AI requests in progress"
    finally:
        client.app.state.limiter.in_flight = 0


def test_challenge_pages_poll_queue_while_thinking(client):
    html = client.get("/c/d5").text
    assert 'id="thinking"' in html and "data-queue" in html and 'fetch("/queue")' in html


def test_prewarm_targets_and_missing_key(monkeypatch, capsys):
    from app.settings import get_settings, load_providers

    s = get_settings()
    providers = load_providers(s.providers_file, s.default_provider)
    used = prewarm.targets(s, providers, every=False)
    assert sorted(c for cids in used.values() for c in cids) == sorted(ACTIVE_IDS)
    assert len(prewarm.targets(s, providers, every=True)) >= len(providers)

    async def fake_ping(provider, model, settings, api_key=None):
        return True, 0.1, "OK"

    monkeypatch.setattr(prewarm, "ping", fake_ping)
    monkeypatch.setattr(prewarm, "get_settings", lambda: s.model_copy(update={
        "groq_api_key": ""}))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    code = asyncio.run(prewarm.main(["--all"]))
    out = capsys.readouterr().out
    assert code == 1 and "GROQ_API_KEY is not set" in out and "FAIL" in out
    assert "local" in out and "OK" in out


def test_prewarm_pings_every_key(monkeypatch, capsys):
    from app.settings import get_settings

    pinged = []

    async def fake_ping(provider, model, settings, api_key=None):
        pinged.append((provider.name, api_key))
        return api_key != "k2", 0.1, "OK" if api_key != "k2" else "RateLimitError"

    s = get_settings().model_copy(update={"default_provider": "cloud", "groq_api_key": "k1,k2,k3"})
    monkeypatch.setattr(prewarm, "ping", fake_ping)
    monkeypatch.setattr(prewarm, "get_settings", lambda: s)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    code = asyncio.run(prewarm.main([]))
    out = capsys.readouterr().out
    assert [k for n, k in pinged if n == "cloud"] == ["k1", "k2", "k3"]
    assert code == 1 and "cloud#2" in out and "k1" not in out  # labels, never the keys


def test_queue_poll_does_not_inherit_the_chat_form(client):
    """Regression: the poll span sat inside the hx-post chat form and inherited hx-target="#chat-log",
    so every poll wiped the chat, and hx-indicator="#thinking", so it kept itself polling forever."""
    html = client.get("/c/d5").text
    form = html.split('hx-post="/c/d5/chat"')[1].split(">")[0]
    assert 'hx-disinherit="*"' in form
    span = html.split("data-queue")[1].split(">")[0]
    assert "hx-" not in span  # polled by plain JS, so nothing to inherit


def test_theme_toggle_on_every_page(client):
    for path in ("/board", "/scoreboard", "/admin", "/c/d5"):
        html = client.get(path).text
        assert "data-theme-toggle" in html and "ndToggleTheme()" in html, path
        # palette must be configured before Tailwind's CDN script runs
        assert html.index("ndTailwindColors = colors") < html.index("cdn.tailwindcss.com")
        assert "tailwind.config = { theme: { extend: { colors: window.ndTailwindColors } } }" in html
        assert "hover:text-white" not in html  # would vanish on the light theme


def test_site_is_branded_bigfish(client):
    for path in ("/", "/board", "/c/d5", "/scoreboard", "/admin", "/nope"):
        html = client.get(path).text
        assert "Bigfish AI Security Training</title>" in html and "data-brand" in html, path
        assert "Nexus" not in html, path  # old fictional names are gone
    assert "BigfishDesk" in client.get("/c/d5").text  # the fictional helpdesk is BigfishDesk
