"""`llm_audit` sink: every model call is stored with its exact request body; and through the
handler, a Vertex call over httpx MockTransport writes exactly one row.

Synthetic parcels and products only.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from farmcore.apps.chat.audit import DbAuditSink, audit_ref
from farmcore.apps.chat.extractor import (
    AuditRecord,
    FallbackExtractor,
    RegexExtractor,
    VertexExtractor,
)
from farmcore.apps.chat.handlers import on_message
from farmcore.core.models.canonical import LlmAudit, Log
from farmcore.settings import Settings
from tests.apps.chat.test_handlers import SPEC_SENTENCE, deps, seed_farm, voice

RECORD = AuditRecord(
    adapter="vertex",
    model="gemini-2.5-flash",
    endpoint="https://europe-west4-aiplatform.googleapis.com/v1beta1/x:generateContent",
    request_body={"contents": [{"role": "user", "parts": [{"text": "{}"}]}]},
    response_body={"candidates": []},
    status=200,
    error=None,
    latency_ms=321,
)


def rows(session) -> list[LlmAudit]:
    return list(session.scalars(select(LlmAudit).order_by(LlmAudit.id)))


def test_sink_stores_the_exact_request_body_response_status_and_latency(session_factory, session):
    sink = DbAuditSink(session_factory)

    with audit_ref("tg:1:2"):
        sink(RECORD)

    [row] = rows(session)
    assert row.request_body == RECORD.request_body
    assert row.response_body == {"candidates": []}
    assert (row.adapter, row.model, row.endpoint) == (RECORD.adapter, RECORD.model, RECORD.endpoint)
    assert (row.status, row.error, row.latency_ms, row.ref) == ("200", None, 321, "tg:1:2")


def test_sink_stores_a_call_that_was_never_sent(session_factory, session):
    DbAuditSink(session_factory)(
        AuditRecord(**{**RECORD.__dict__, "status": None, "error": "not sent: no token"})
    )

    [row] = rows(session)
    assert (row.status, row.error, row.ref) == ("not sent", "not sent: no token", None)


# --- Vertex through the handler -------------------------------------------------------------


def gemini(draft: dict) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(draft)}]}}]}


def vertex_extractor(session_factory, handler) -> FallbackExtractor:
    settings = Settings(_env_file=None, vertex_auth="token", vertex_access_token="test-token")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return FallbackExtractor(
        VertexExtractor(settings, DbAuditSink(session_factory), client=client), RegexExtractor()
    )


def user_payload(row: LlmAudit) -> dict:
    [content] = row.request_body["contents"]
    [part] = content["parts"]
    return json.loads(part["text"])


def walk(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_vertex_call_through_the_handler_writes_one_audit_row_with_only_the_payload(
    session_factory, session, settings
):
    seed_farm(session, settings)
    draft = {
        "type": "spray",
        "parcels": ["P5", "P6"],
        "products": ["Copper Shield 20 WP", "Sulfo Jet 80 WG"],
        "dose_value": 2,
        "dose_unit": "kg/ha",
        "ts_start": "2024-04-10T08:00",
        "confidence": 0.9,
        "notes": [],
    }
    extractor = vertex_extractor(
        session_factory, lambda request: httpx.Response(200, json=gemini(draft))
    )

    on_message(deps(session_factory, settings, extractor), voice(SPEC_SENTENCE, message_id=9))

    [row] = rows(session)
    assert (row.adapter, row.status, row.error, row.ref) == ("vertex", "200", None, "tg:4242:9")
    payload = user_payload(row)
    assert set(payload) == {"transcript", "parcels", "products", "date"}
    assert payload["transcript"] == SPEC_SENTENCE
    assert payload["date"] == "2024-04-10"
    assert payload["parcels"] == [
        {"alias": "P1", "name": "Le Plateau"},
        {"alias": "P5", "name": "Les Granges"},
        {"alias": "P6", "name": "Peyrot"},
    ]
    assert {p["name"] for p in payload["products"]} == {"Copper Shield 20 WP", "Sulfo Jet 80 WG"}
    assert not {"inlineData", "inline_data", "fileData", "audio"} & set(walk(row.request_body))
    [log] = list(session.scalars(select(Log)))
    assert log.sources[0].raw_payload["adapter"] == "vertex"


def test_vertex_failure_is_audited_and_the_regex_fallback_still_logs(
    session_factory, session, settings
):
    seed_farm(session, settings)
    extractor = vertex_extractor(session_factory, lambda request: httpx.Response(500, text="boom"))

    out = on_message(deps(session_factory, settings, extractor), voice(SPEC_SENTENCE))

    [row] = rows(session)
    assert (row.status, row.error) == ("500", "HTTP 500")
    [log] = list(session.scalars(select(Log)))
    assert log.sources[0].raw_payload["adapter"] == "regex"
    assert "fallback: regex (HTTP 500)" in log.sources[0].raw_payload["notes"]
    assert "I used the simple one. Please check the details." in out.text
