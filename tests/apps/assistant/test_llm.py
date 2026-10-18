"""Web assistant with Gemini function calling (fake model only) and browser voice (fake
transcriber only). No test here reaches a network or a real model."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from farmcore.apps.assistant import llm
from farmcore.apps.assistant import routes as assistant_routes
from farmcore.apps.chat.transcribe import FakeTranscriber, Transcript
from farmcore.core.models.canonical import LlmAudit
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


class FakeChooser:
    """Records what would be sent; returns a fixed function call (or raises)."""

    def __init__(self, choice=None, error: Exception | None = None):
        self.choice = choice
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    def choose(self, question, tools):
        self.calls.append((question, [t.name for t in tools]))
        if self.error:
            raise self.error
        return self.choice


@pytest.fixture
def farm(session, settings):
    farm = ensure_default_farm(session, settings)
    session.commit()
    return farm


def _ask(client, q, **params):
    return client.get("/assistant/ask", params={"q": q, **params})


def test_model_choice_runs_the_tool_and_renders_only_its_facts_and_links(
    client, session, seeded, step
):
    from datetime import datetime

    from farmcore.core.models.canonical import Farm, Log, Parcel

    seeded("B")
    step("2024-04-10 06:00")
    farm = session.scalar(select(Farm).order_by(Farm.id))
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P5"))
    session.add(
        Log(
            farm_id=farm.id,
            type="spray",
            status="done",
            name="Demo data spray",
            ts_start=datetime.fromisoformat("2024-04-09T05:00:00+02:00"),
            parcels=[parcel],
        )
    )
    session.commit()
    fake = FakeChooser(
        llm.Choice(
            "operations_last_spray",
            {"parcel": "P5"},
            text="P5 was sprayed 7 times, last on 3 April with 9 kg/ha.",
        )
    )
    client.app.state.assistant_chooser = fake

    body = _ask(client, "Tell me about the most recent treatment on P5").json()

    assert body["via"] == "model"
    assert body["tool"] == "operations_last_spray"
    assert "P5" in body["text"] and "09 Apr 05:00" in body["text"]
    assert "7 times" not in body["text"] and "9 kg/ha" not in body["text"]
    assert body["facts"]["date"].startswith("2024-04-09")
    assert body["links"] and all("farm=" in path for _, path in body["links"])
    question, names = fake.calls[0]
    assert question == "Tell me about the most recent treatment on P5"
    assert "operations_last_spray" in names


def test_model_choice_reads_through_the_assistant_actor(client, session, farm):
    from farmcore.core.models.canonical import AccessLog

    client.app.state.assistant_chooser = FakeChooser(llm.Choice("fields_list_parcels", {}))

    body = _ask(client, "what vines do I have").json()

    assert (body["via"], body["tool"]) == ("model", "fields_list_parcels")
    reads = session.scalars(select(AccessLog).where(AccessLog.actor == "web:assistant")).all()
    assert reads and all(r.purpose == "assistant" for r in reads)


def test_no_credentials_uses_the_keyword_router_without_building_a_model(client, farm, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no model without credentials")

    monkeypatch.setattr(llm, "VertexToolChooser", boom)
    assert client.app.state.settings.extractor == "fake"

    body = _ask(client, "Which parcels do I have? list my parcels").json()

    assert body["via"] == "keywords"


def test_tour_mode_never_calls_the_model(client, farm):
    fake = FakeChooser(llm.Choice("fields_list_parcels", {}))
    client.app.state.assistant_chooser = FakeChooser(llm.Choice("fields_list_parcels", {}))
    assert _ask(client, "what vines do I have").json()["via"] == "model"  # clock row now exists
    client.app.state.assistant_chooser = fake
    client.cookies.set("farmcore_tour", "1")

    body = _ask(client, "what vines do I have").json()

    assert fake.calls == []
    assert body["via"] == "keywords"


@pytest.mark.parametrize(
    "fake",
    [
        FakeChooser(error=llm.ChooserError("HTTP 500")),
        FakeChooser(error=TimeoutError()),
        FakeChooser(llm.Choice("operations_log_spray", {"parcel": "P1"})),
        FakeChooser(llm.Choice("assistant_plan", {"busy_days": ["2024-04-11"]}, text="")),
        FakeChooser(None),
    ],
    ids=["error", "timeout", "unknown-tool", "hidden-tool", "no-call"],
)
def test_model_failures_fall_back_to_the_keyword_router(client, session, farm, fake):
    from farmcore.core.models.kernel import AppModule

    session.get(AppModule, "forecast").state = "uninstalled"
    session.commit()
    client.app.state.assistant_chooser = fake

    response = _ask(client, "Do I need to spray anything?")

    assert response.status_code == 200
    body = response.json()
    assert body["via"] == "keywords"
    assert body["tool"] == "advisor_today"


def test_model_arguments_are_limited_to_declared_string_params(client, farm):
    client.app.state.assistant_chooser = FakeChooser(
        llm.Choice("fields_list_parcels", {"sql": "select * from log"})
    )

    body = _ask(client, "what vines do I have").json()

    assert (body["via"], body["tool"]) == ("model", "fields_list_parcels")


# --- the Vertex adapter, over a mock transport -------------------------------------------------


def _vertex_settings(settings):
    return settings.model_copy(
        update={
            "extractor": "vertex",
            "vertex_auth": "token",
            "vertex_access_token": SecretStr("test-token"),
        }
    )


def _tools():
    from farmcore.apps.fields.tools import TOOLS as FIELD_TOOLS
    from farmcore.apps.operations.tools import TOOLS as OPS_TOOLS

    return [*FIELD_TOOLS, *OPS_TOOLS]


def test_vertex_chooser_sends_only_the_question_and_tool_schemas_and_audits(settings):
    sent, records = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Sure, P5 had 12 sprays."},
                                {
                                    "functionCall": {
                                        "name": "operations_last_spray",
                                        "args": {"parcel": "P5"},
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )

    chooser = llm.VertexToolChooser(
        _vertex_settings(settings),
        records.append,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    choice = chooser.choose("When was P5 last sprayed?", _tools())

    assert choice == llm.Choice(
        "operations_last_spray", {"parcel": "P5"}, text="Sure, P5 had 12 sprays."
    )
    body = sent[0]
    assert body["contents"] == [{"role": "user", "parts": [{"text": "When was P5 last sprayed?"}]}]
    declared = body["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in declared} == {t.name for t in _tools()}
    last = next(d for d in declared if d["name"] == "operations_last_spray")
    assert last["parameters"]["properties"] == {"parcel": {"type": "STRING"}}
    assert set(body) <= {"systemInstruction", "contents", "tools", "toolConfig", "generationConfig"}
    assert len(records) == 1 and records[0].adapter == llm.ADAPTER and records[0].error is None
    assert records[0].request_body == body


def test_vertex_chooser_error_raises_and_is_audited(settings):
    records = []
    chooser = llm.VertexToolChooser(
        _vertex_settings(settings),
        records.append,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )

    with pytest.raises(llm.ChooserError):
        chooser.choose("When was P5 last sprayed?", _tools())

    assert records[0].status == 500 and records[0].error


def test_vertex_settings_build_an_audited_chooser_in_the_app(settings, session_factory, session):
    ensure_default_farm(session, settings)
    session.commit()
    app = create_app(_vertex_settings(settings), session_factory=session_factory)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"functionCall": {"name": "fields_list_parcels"}}]}}
                ]
            },
        )

    with TestClient(app) as c:
        chooser = assistant_routes.chooser_for(c.app)
        assert isinstance(chooser, llm.VertexToolChooser)
        chooser._client = httpx.Client(transport=httpx.MockTransport(handler))
        body = c.get("/assistant/ask", params={"q": "what vines do I have"}).json()

    assert (body["via"], body["tool"]) == ("model", "fields_list_parcels")
    session.expire_all()
    audits = session.scalars(select(LlmAudit).where(LlmAudit.adapter == llm.ADAPTER)).all()
    assert len(audits) == 1 and audits[0].ref.startswith("web:assistant")


# --- voice -------------------------------------------------------------------------------------

OGG = b"OggS" + b"\0" * 200


def _post_audio(client, data=OGG, content_type="audio/webm", **kw):
    return client.post(
        "/assistant/transcribe", files={"audio": ("q.webm", data, content_type)}, **kw
    )


def test_transcribe_returns_editable_text(client, farm):
    fake = FakeTranscriber(Transcript("When was P5 last sprayed?", "en", 3.0))
    client.app.state.assistant_transcriber = fake

    response = _post_audio(client)

    assert response.status_code == 200
    assert response.json() == {"text": "When was P5 last sprayed?"}
    assert len(fake.calls) == 1


def test_transcribe_refuses_large_long_or_non_audio_uploads(client, farm):
    fake = FakeTranscriber(Transcript("hello", "en", 3.0))
    client.app.state.assistant_transcriber = fake

    too_big = _post_audio(client, data=b"x" * (assistant_routes.MAX_AUDIO_BYTES + 1))
    not_audio = _post_audio(client, content_type="text/plain")
    empty = _post_audio(client, data=b"")
    assert (too_big.status_code, not_audio.status_code, empty.status_code) == (413, 415, 422)
    assert fake.calls == []

    fake.transcript = Transcript("a very long ramble", "en", assistant_routes.MAX_AUDIO_SECONDS + 1)
    long = _post_audio(client)
    assert long.status_code == 413 and "text" not in long.json()


def test_transcribe_without_a_speech_service_says_so(client, farm):
    class Broken:
        def transcribe(self, path, hints):
            raise RuntimeError("model files missing")

    client.app.state.assistant_transcriber = Broken()

    response = _post_audio(client)

    assert response.status_code == 503
    assert response.json()["detail"] == "Voice needs the speech service."


def test_unreadable_audio_is_a_422_not_a_missing_service(client, farm):
    import av

    class BadAudio:
        def transcribe(self, path, hints):
            raise av.error.InvalidDataError(1094995529, "Invalid data")

    client.app.state.assistant_transcriber = BadAudio()

    response = _post_audio(client)

    assert response.status_code == 422
    assert response.json()["detail"] == "Could not read the recording."


def test_tour_mode_hides_the_mic_and_blocks_transcription(client, farm):
    fake = FakeTranscriber(Transcript("hello", "en", 1.0))
    client.app.state.assistant_transcriber = fake
    assert client.get("/assistant/voice").json()["voice"] is True

    client.cookies.set("farmcore_tour", "1")

    assert client.get("/assistant/voice").json()["voice"] is False
    assert _post_audio(client).status_code == 403
    assert fake.calls == []


def test_voice_is_admin_only_when_an_admin_token_is_set(settings, session_factory, session):
    ensure_default_farm(session, settings)
    session.commit()
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr("t0ken")}),
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        c.app.state.assistant_transcriber = FakeTranscriber(Transcript("hi", "en", 1.0))
        assert c.get("/assistant/voice").json()["voice"] is False
        assert _post_audio(c).status_code == 403
        c.cookies.set("admin_token", "t0ken")
        assert c.get("/assistant/voice").json()["voice"] is True
        assert _post_audio(c).status_code == 200


def test_panel_script_records_with_media_recorder_and_fills_the_question():
    from pathlib import Path

    js = (Path(assistant_routes.__file__).parent / "static" / "assistant.js").read_text()
    assert "MediaRecorder" in js and "/assistant/transcribe" in js and "/assistant/voice" in js
    assert "Voice needs the speech service" in js


# --- admin-only model, mode hint, upload caps --------------------------------------------------


def _token_app(settings, session_factory, **update):
    return create_app(
        settings.model_copy(update={"admin_token": SecretStr("t0ken"), **update}),
        session_factory=session_factory,
    )


def test_anonymous_visitors_never_reach_the_model_admins_do(settings, session_factory, farm):
    fake = FakeChooser(llm.Choice("fields_list_parcels", {}))
    with TestClient(_token_app(settings, session_factory)) as c:
        c.app.state.assistant_chooser = fake
        anonymous = c.get("/assistant/ask", params={"q": "what vines do I have"}).json()
        assert anonymous["via"] == "keywords" and fake.calls == []
        c.cookies.set("admin_token", "t0ken")
        admin = c.get("/assistant/ask", params={"q": "what vines do I have"}).json()
    assert admin["via"] == "model" and len(fake.calls) == 1


def _subtitle(page: str) -> str:
    return page.split('id="assistant-mode"', 1)[1].split("</p>", 1)[0]


def test_panel_subtitle_names_the_mode_actually_used(settings, session_factory, farm):
    vertex = {
        "extractor": "vertex",
        "vertex_auth": "token",
        "vertex_access_token": SecretStr("test-token"),
    }
    with TestClient(_token_app(settings, session_factory, **vertex)) as c:
        anonymous = c.get("/apps").text
        c.cookies.set("admin_token", "t0ken")
        admin = c.get("/apps").text
        c.cookies.set("farmcore_tour", "1")
        tour = c.get("/apps").text
    with TestClient(_token_app(settings, session_factory)) as c:
        c.cookies.set("admin_token", "t0ken")
        no_creds = c.get("/apps").text
    assert "From your farm records" in anonymous
    assert "keyword-only" not in anonymous
    assert "AI-assisted" in _subtitle(admin)
    for page in (anonymous, tour, no_creds):
        assert "Keyword" in _subtitle(page) and "AI-assisted" not in _subtitle(page)


def test_oversized_upload_is_refused_from_content_length_before_parsing(client, farm, monkeypatch):
    from starlette.requests import Request

    async def no_parse(self, *a, **k):
        raise AssertionError("body parsed")

    monkeypatch.setattr(Request, "form", no_parse)
    fake = FakeTranscriber(Transcript("hi", "en", 1.0))
    client.app.state.assistant_transcriber = fake

    response = _post_audio(client, data=b"x" * (assistant_routes.MAX_BODY_BYTES + 1))

    assert response.status_code == 413
    assert fake.calls == []


def test_chunked_upload_without_length_is_capped_while_streaming(client, farm):
    fake = FakeTranscriber(Transcript("hi", "en", 1.0))
    client.app.state.assistant_transcriber = fake
    chunk = b"x" * 65536

    def body():
        for _ in range(assistant_routes.MAX_BODY_BYTES // len(chunk) + 2):
            yield chunk

    response = client.post(
        "/assistant/transcribe",
        content=body(),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )

    assert response.status_code == 413
    assert fake.calls == []


# --- mic only when the local speech model is loadable ------------------------------------------


def test_mic_is_hidden_when_no_local_speech_model_is_present(client, farm, monkeypatch):
    calls = []

    def missing(settings):
        calls.append(settings.whisper_model)
        return False

    monkeypatch.setattr(assistant_routes, "local_model_available", missing)

    first = client.get("/assistant/voice").json()
    second = client.get("/assistant/voice").json()

    assert first["voice"] is False and second["voice"] is False
    assert first["unavailable"] == "Voice needs the speech service."
    assert len(calls) == 1  # checked once per process, then cached


def test_mic_shows_when_the_local_speech_model_is_present(client, farm, monkeypatch):
    monkeypatch.setattr(assistant_routes, "local_model_available", lambda settings: True)

    assert client.get("/assistant/voice").json()["voice"] is True


def test_local_model_check_reads_only_the_cache(settings, tmp_path, monkeypatch):
    from faster_whisper import utils

    seen = []

    def fake_download(name, local_files_only=False, **kw):
        seen.append((name, local_files_only))
        raise FileNotFoundError(name)

    monkeypatch.setattr(utils, "download_model", fake_download)

    assert assistant_routes.local_model_available(settings) is False
    assert seen == [(settings.whisper_model, True)]
    model_dir = tmp_path / "whisper"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"x")
    local = settings.model_copy(update={"whisper_model": str(model_dir)})
    assert assistant_routes.local_model_available(local) is True


@pytest.mark.parametrize(
    "auth",
    [
        {"vertex_auth": "token", "vertex_access_token": None},
        {"vertex_auth": "adc"},
        {"vertex_auth": "gcloud"},
    ],
    ids=["token-unset", "adc-no-file", "gcloud-missing"],
)
def test_vertex_without_usable_credentials_says_keyword_and_builds_no_model(
    settings, session_factory, farm, monkeypatch, auth
):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(assistant_routes.shutil, "which", lambda name: None)

    def boom(*a, **k):
        raise AssertionError("no model without credentials")

    monkeypatch.setattr(llm, "VertexToolChooser", boom)
    with TestClient(_token_app(settings, session_factory, extractor="vertex", **auth)) as c:
        c.cookies.set("admin_token", "t0ken")
        page = c.get("/apps").text
        body = c.get("/assistant/ask", params={"q": "what vines do I have"}).json()

    assert "Keyword" in _subtitle(page) and "AI-assisted" not in _subtitle(page)
    assert body["via"] == "keywords"


def test_adc_credentials_file_counts_as_configured(settings, session_factory, farm, tmp_path,
                                                   monkeypatch):  # fmt: skip
    key = tmp_path / "key.json"
    key.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    with TestClient(
        _token_app(settings, session_factory, extractor="vertex", vertex_auth="adc")
    ) as c:
        c.cookies.set("admin_token", "t0ken")
        assert "AI-assisted" in _subtitle(c.get("/apps").text)
