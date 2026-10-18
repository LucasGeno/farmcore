"""`GET /chat/`: the LLM payload page, read through the scoped repository (`audit:read`).

Synthetic parcels and products only.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from farmcore.apps.chat.handlers import on_message
from farmcore.apps.chat.manifest import MANIFEST
from farmcore.apps.chat.routes import _masked_chat
from farmcore.core.models.canonical import AccessLog, LlmAudit
from farmcore.main import create_app
from tests.apps.chat.test_audit import gemini, vertex_extractor
from tests.apps.chat.test_handlers import SPEC_SENTENCE, deps, seed_farm, voice

DRAFT = {
    "type": "spray",
    "parcels": ["P5", "P6"],
    "products": ["Copper Shield 20 WP"],
    "dose_value": 2,
    "dose_unit": "kg/ha",
    "ts_start": None,
    "confidence": 0.9,
    "notes": [],
}


def send(session_factory, settings, status: int = 200) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if status == 200:
            return httpx.Response(200, json=gemini(DRAFT))
        return httpx.Response(status, text="unavailable")

    extractor = vertex_extractor(session_factory, reply)
    on_message(deps(session_factory, settings, extractor), voice(SPEC_SENTENCE))


def test_manifest_declares_the_chat_app():
    assert MANIFEST.name == "chat"
    assert MANIFEST.depends == ("fields", "operations", "advisor")
    assert MANIFEST.scopes == ("logs:write",)
    assert MANIFEST.nav == ("AI log", "/chat/")


def test_chat_page_shows_the_exact_payload_the_model_saw(
    client, session_factory, session, settings
):
    seed_farm(session, settings)
    send(session_factory, settings)

    response = client.get("/chat/")

    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "Raw audio never leaves this server. This is everything the model saw." in page
    assert f'"transcript": "{SPEC_SENTENCE}"' in page
    assert '"date": "2024-04-10"' in page
    assert '"alias": "P5"' in page
    assert "gemini-2.5-flash" in page
    assert "vertex" in page


def test_chat_page_marks_a_failed_call_as_regex_fallback(
    client, session_factory, session, settings
):
    seed_farm(session, settings)
    send(session_factory, settings, status=503)

    page = html.unescape(client.get("/chat/").text)

    assert "regex fallback" in page
    assert "HTTP 503" in page


def test_chat_page_without_calls_says_so(client, session, settings):
    seed_farm(session, settings)

    page = client.get("/chat/").text

    assert "No model calls yet." in page


def test_chat_page_writes_an_audit_read_access_log_row(client, session, settings):
    seed_farm(session, settings)

    assert client.get("/chat/").status_code == 200

    rows = list(session.scalars(select(AccessLog).where(AccessLog.resource == "llm_audit")))
    assert [(r.scope, r.purpose) for r in rows] == [("audit:read", "web")]


# --- Sanitised demo view for non-admins ----------------------------------------

TOKEN = "tok-chat"
CHAT_REF = "tg:123458777:5"
TRANSCRIPT = "copper on P5"  # 12 characters
USER_PART = {
    "transcript": TRANSCRIPT,
    "parcels": [{"alias": "P5", "name": "Clos Secret"}],
    "products": [{"name": "Cuprum Hidden", "active_substance": "copper"}],
    "date": "2024-04-10",
}


@pytest.fixture
def guarded(settings, session_factory) -> Iterator[TestClient]:
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr(TOKEN)}),
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        yield c


def seed_audit_row(session, settings) -> None:
    seed_farm(session, settings)
    session.add(
        LlmAudit(
            adapter="vertex",
            model="gemini-2.5-flash",
            endpoint="https://example.invalid/v1:generateContent",
            request_body={
                "contents": [{"role": "user", "parts": [{"text": json.dumps(USER_PART)}]}]
            },
            response_body={"candidates": [{"content": {"parts": [{"text": "Cuprum Hidden"}]}}]},
            status="200",
            error=None,
            latency_ms=321,
            ref=CHAT_REF,
        )
    )
    session.commit()


def test_anonymous_chat_page_is_a_sanitised_demo_view(guarded, session, settings):
    seed_audit_row(session, settings)

    response = guarded.get("/chat/")

    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "gemini-2.5-flash" in page
    assert "chat …8777" in page
    assert "tg:" not in page and "123458777" not in page
    assert TRANSCRIPT not in page
    assert "12 characters" in page
    assert 'data-fields="transcript,parcels,products,date"' in page
    for value in ("Clos Secret", "Cuprum Hidden", "2024-04-10", "example.invalid"):
        assert value not in page


def test_anonymous_chat_page_is_in_plain_words_with_real_times_labelled(guarded, session, settings):
    """Live QA 17: no "vertex · status 200 · 321 ms"; the call times are real, not the replay."""
    seed_audit_row(session, settings)

    page = html.unescape(guarded.get("/chat/").text)

    call = page[page.index('data-call="demo"') :]
    assert "AI reader (Gemini)" in call
    assert "answered in 0.3 s" in call
    assert "status 200" not in call and "321 ms" not in call
    assert "real time" in call
    assert "your words (12 characters)" in call
    assert "parcel names" in call and "product list" in call
    assert "real time, not the replayed farm date" in page


@pytest.mark.parametrize(
    ("ref", "shown"),
    [
        ("tg:123458777:42", "chat …8777"),
        ("tg:8777:42", "chat …"),
        ("tg:77:1", "chat …"),
        ("cli:123458777", "chat …"),
        ("123458777", "chat …"),
        (None, None),
    ],
)
def test_masked_chat_never_shows_a_short_or_non_telegram_ref_in_full(ref, shown):
    assert _masked_chat(ref) == shown


def test_admin_chat_page_shows_the_full_audit_view(guarded, session, settings):
    seed_audit_row(session, settings)
    guarded.cookies.set("admin_token", TOKEN)

    page = html.unescape(guarded.get("/chat/").text)

    assert f'"transcript": "{TRANSCRIPT}"' in page
    assert "Clos Secret" in page
    assert CHAT_REF in page
