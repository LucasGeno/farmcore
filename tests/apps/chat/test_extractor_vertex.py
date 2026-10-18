"""Vertex adapter over httpx MockTransport: regional endpoint, payload boundary, audit, fallback."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import google.auth.exceptions
import httpx
import pytest
from pydantic import ValidationError

from farmcore.apps.chat.extractor import (
    SYSTEM_INSTRUCTION,
    AuditRecord,
    DraftOperation,
    ExtractorError,
    ExtractRequest,
    FakeExtractor,
    FallbackExtractor,
    RegexExtractor,
    VertexExtractor,
    get_extractor,
)
from farmcore.settings import Settings

PARIS = ZoneInfo("Europe/Paris")
TOKEN = "ya29.secret-oauth-token"
API_KEY = "AIza-secret-api-key"

COPPER = "Copper Shield 20 WP"
SULPHUR = "Sulfo Jet 80 WG"


def make_request(transcript: str, now_local: datetime | None = None) -> ExtractRequest:
    now_local = now_local or datetime(2024, 4, 10, 10, 0, tzinfo=PARIS)
    return ExtractRequest(
        transcript=transcript,
        parcels=[
            {"alias": "P1", "name": "Le Plateau"},
            {"alias": "P5", "name": "Les Granges"},
            {"alias": "P6", "name": "Peyrot"},
        ],
        products=[
            {
                "name": COPPER,
                "active_substance": "copper",
                "product_type": "contact",
                "targets": ["downy"],
            },
            {
                "name": SULPHUR,
                "active_substance": "sulphur",
                "product_type": "contact",
                "targets": ["powdery"],
            },
        ],
        today=now_local.date(),
        now_local=now_local,
    )


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "vertex_auth": "token",
        "vertex_access_token": TOKEN,
        "vertex_project": "example-project",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def gemini_response(draft: dict) -> dict:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": json.dumps(draft)}]}},
        ],
    }


MODEL_DRAFT = {
    "type": "spray",
    "parcels": ["P5", "P6"],
    "products": [COPPER, SULPHUR],
    "dose_value": 2,
    "dose_unit": "kg/ha",
    "ts_start": "2024-04-10T08:00",
    "confidence": 0.9,
    "notes": [],
}


class Recorder:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


def vertex(recorder: Recorder, audit: list[AuditRecord], **overrides: object) -> VertexExtractor:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return VertexExtractor(settings(**overrides), audit.append, client=client)


def walk(node: object):
    """Yield every dict key in a nested JSON value."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_vertex_posts_only_the_payload_to_the_regional_endpoint_and_parses_the_draft():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    audit: list[AuditRecord] = []
    req = make_request("Sprayed Les Granges and Peyrot with copper and sulphur, 2 kilos")

    draft = vertex(recorder, audit).extract(req)

    (sent,) = recorder.requests
    assert sent.method == "POST"
    assert sent.url.host == "europe-west4-aiplatform.googleapis.com"
    assert sent.url.path == (
        "/v1beta1/projects/example-project/locations/europe-west4"
        "/publishers/google/models/gemini-2.5-flash:generateContent"
    )
    body = json.loads(sent.content)
    (content,) = body["contents"]
    assert content["role"] == "user"
    (part,) = content["parts"]
    assert set(part) == {"text"}
    payload = json.loads(part["text"])
    assert set(payload) == {"transcript", "parcels", "products", "date"}
    assert payload["transcript"] == req.transcript
    assert payload["date"] == "2024-04-10"
    assert payload["parcels"][1] == {"alias": "P5", "name": "Les Granges"}
    assert body["systemInstruction"]["parts"] == [{"text": SYSTEM_INSTRUCTION}]
    keys = set(walk(body))
    assert not keys & {"inlineData", "inline_data", "fileData", "file_data"}
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["temperature"] == 0
    assert config["responseSchema"]["type"] == "OBJECT"
    # Plain extraction needs no thinking; it only adds latency on stage.
    assert config["thinkingConfig"] == {"thinkingBudget": 0}

    assert draft == DraftOperation(
        type="spray",
        parcels=["P5", "P6"],
        products=[COPPER, SULPHUR],
        dose_value=2.0,
        dose_unit="kg/ha",
        ts_start=datetime(2024, 4, 10, 8, 0, tzinfo=PARIS),
        confidence=0.9,
        adapter="vertex",
        notes=[],
    )

    (record,) = audit
    assert record.adapter == "vertex"
    assert record.model == "gemini-2.5-flash"
    assert record.endpoint == str(sent.url)
    assert record.request_body == body
    assert record.status == 200
    assert record.error is None
    assert record.response_body == gemini_response(MODEL_DRAFT)


def test_vertex_drops_products_not_on_the_shortlist_and_clamps_time_to_now():
    model_draft = dict(MODEL_DRAFT, products=[COPPER, "Mystery Blend"])
    recorder = Recorder(httpx.Response(200, json=gemini_response(model_draft)))
    req = make_request("whatever", now_local=datetime(2024, 4, 10, 6, 0, tzinfo=PARIS))

    draft = vertex(recorder, []).extract(req)

    assert draft.products == [COPPER]
    assert draft.ts_start == datetime(2024, 4, 10, 6, 0, tzinfo=PARIS)
    assert "dropped unknown product: Mystery Blend" in draft.notes
    assert "time clamped to now" in draft.notes


def test_auth_goes_in_a_header_and_never_into_the_audit_record():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    audit: list[AuditRecord] = []

    vertex(recorder, audit, gemini_api_key=API_KEY).extract(make_request("Sprayed P1"))

    (sent,) = recorder.requests
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    # The regional host rejects API keys; Vertex never sends one, even when one is configured.
    assert "x-goog-api-key" not in sent.headers
    assert TOKEN not in str(sent.url)
    (record,) = audit
    assert TOKEN not in repr(record)
    assert TOKEN not in json.dumps(record.request_body)


def test_vertex_auth_does_not_offer_api_key():
    with pytest.raises(ValidationError, match="vertex_auth"):
        Settings(_env_file=None, vertex_auth="api_key")


def test_gcloud_auth_runs_gcloud_once_and_caches_the_token():
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> str:
        calls.append(cmd)
        return TOKEN + "\n"

    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    extractor = VertexExtractor(
        Settings(_env_file=None, vertex_auth="gcloud"), lambda r: None, client=client, gcloud=runner
    )

    extractor.extract(make_request("Sprayed P1"))
    extractor.extract(make_request("Sprayed P1"))

    assert calls == [["gcloud", "auth", "print-access-token"]]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in recorder.requests)


FR_SENTENCE = "Traité Les Granges et Peyrot au cuivre et soufre, 2 kilos, ce matin"


@pytest.mark.parametrize(
    "response, reason",
    [
        (httpx.Response(500, json={"error": {"message": "boom"}}), "HTTP 500"),
        (
            httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{nope"}]}}]}),
            "invalid JSON",
        ),
    ],
)
def test_fallback_uses_regex_when_vertex_fails(response, reason):
    audit: list[AuditRecord] = []
    primary = vertex(Recorder(response), audit)
    req = make_request(FR_SENTENCE)

    draft = FallbackExtractor(primary, RegexExtractor()).extract(req)

    expected = RegexExtractor().extract(req)
    assert draft.adapter == "regex"
    assert draft.parcels == expected.parcels == ["P5", "P6"]
    assert draft.products == expected.products
    assert draft.notes == [*expected.notes, f"fallback: regex ({reason})"]
    (record,) = audit
    assert record.error is not None


def test_fallback_on_transport_error():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(explode))
    primary = VertexExtractor(settings(), lambda r: None, client=client)

    draft = FallbackExtractor(primary, RegexExtractor()).extract(make_request(FR_SENTENCE))

    assert draft.adapter == "regex"
    assert draft.notes[-1].startswith("fallback: regex (")


def test_auth_failure_falls_back_and_is_audited_as_not_sent():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    audit: list[AuditRecord] = []
    primary = VertexExtractor(
        Settings(_env_file=None, vertex_auth="token"), audit.append, client=client
    )
    req = make_request(FR_SENTENCE)

    draft = FallbackExtractor(primary, RegexExtractor()).extract(req)

    # Nothing left the host, but the payload view still shows why Vertex was not used.
    assert recorder.requests == []
    (record,) = audit
    assert record.status is None
    assert record.response_body is None
    assert record.error == "not sent: no VERTEX_ACCESS_TOKEN set"
    assert record.request_body == primary.request_body(req)
    assert draft.notes[-1] == "fallback: regex (no VERTEX_ACCESS_TOKEN set)"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_gcloud_token_is_dropped_from_the_cache(status):
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> str:
        calls.append(cmd)
        return TOKEN

    responses = iter(
        [
            httpx.Response(status, json={"error": {"message": "expired"}}),
            httpx.Response(200, json=gemini_response(MODEL_DRAFT)),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    extractor = VertexExtractor(
        Settings(_env_file=None, vertex_auth="gcloud"), lambda r: None, client=client, gcloud=runner
    )

    with pytest.raises(ExtractorError, match=f"HTTP {status}"):
        extractor.extract(make_request("Sprayed P1"))
    extractor.extract(make_request("Sprayed P1"))

    assert len(calls) == 2


def test_vertex_without_a_project_is_the_regex_extractor_and_calls_nothing():
    picked = get_extractor(settings(extractor="vertex", vertex_project=""), lambda r: None)
    assert isinstance(picked, RegexExtractor)


def test_get_extractor_picks_the_configured_adapter():
    audit = lambda r: None  # noqa: E731
    picked = get_extractor(settings(extractor="vertex"), audit)
    assert isinstance(picked, FallbackExtractor)
    assert isinstance(picked.primary, VertexExtractor)
    assert isinstance(picked.fallback, RegexExtractor)
    assert isinstance(get_extractor(settings(extractor="regex"), audit), RegexExtractor)
    assert isinstance(get_extractor(settings(extractor="fake"), audit), FakeExtractor)


def test_fake_returns_its_configured_draft_and_records_requests():
    configured = DraftOperation(parcels=["P1"], products=[COPPER], adapter="fake")
    fake = FakeExtractor(configured)
    req = make_request("anything")

    assert fake.extract(req) == configured
    assert fake.requests == [req]


class FakeCredentials:
    """Stands in for google-auth credentials: `valid`, `token`, `refresh(request)`."""

    def __init__(self, token: str | None = None, *, valid: bool = True) -> None:
        self.token = token
        self.valid = valid
        self.refreshes = 0

    def refresh(self, request: object) -> None:
        self.refreshes += 1
        self.token = f"{TOKEN}-{self.refreshes}"
        self.valid = True


def adc_vertex(recorder, audit, credentials, **overrides) -> VertexExtractor:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    loads: list[int] = []

    def adc():
        loads.append(1)
        return credentials

    extractor = VertexExtractor(
        Settings(_env_file=None, vertex_auth="adc", **overrides),
        audit.append,
        client=client,
        adc=adc,
    )
    extractor.adc_loads = loads  # type: ignore[attr-defined]
    return extractor


def test_adc_sends_a_bearer_token_and_never_an_api_key():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    audit: list[AuditRecord] = []
    extractor = adc_vertex(recorder, audit, FakeCredentials(TOKEN), gemini_api_key=API_KEY)

    extractor.extract(make_request("Sprayed P1"))

    (sent,) = recorder.requests
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-goog-api-key" not in sent.headers
    assert API_KEY not in str(sent.headers)
    (record,) = audit
    assert TOKEN not in repr(record)


def test_adc_refreshes_an_expired_credential_once_and_caches_it():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    creds = FakeCredentials(None, valid=False)
    extractor = adc_vertex(recorder, [], creds)

    extractor.extract(make_request("Sprayed P1"))
    extractor.extract(make_request("Sprayed P1"))

    assert creds.refreshes == 1
    assert extractor.adc_loads == [1]  # type: ignore[attr-defined]
    assert [r.headers["authorization"] for r in recorder.requests] == [f"Bearer {TOKEN}-1"] * 2


class RefusingCredentials(FakeCredentials):
    def refresh(self, request: object) -> None:
        raise google.auth.exceptions.RefreshError("invalid_grant: secret detail")


def test_adc_without_credentials_falls_back_and_is_audited_as_not_sent():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    audit: list[AuditRecord] = []

    def missing():
        raise google.auth.exceptions.DefaultCredentialsError("no ADC found")

    primary = VertexExtractor(
        Settings(_env_file=None, vertex_auth="adc"), audit.append, client=client, adc=missing
    )
    draft = FallbackExtractor(primary, RegexExtractor()).extract(make_request(FR_SENTENCE))

    assert recorder.requests == []
    (record,) = audit
    assert record.status is None
    assert record.error == "not sent: ADC credentials failed (DefaultCredentialsError)"
    assert draft.notes[-1] == "fallback: regex (ADC credentials failed (DefaultCredentialsError))"


def test_adc_refresh_failure_is_not_sent_and_hides_the_provider_detail():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    audit: list[AuditRecord] = []
    extractor = adc_vertex(recorder, audit, RefusingCredentials(None, valid=False))

    with pytest.raises(ExtractorError, match=r"^ADC credentials failed \(RefreshError\)$"):
        extractor.extract(make_request("Sprayed P1"))

    assert recorder.requests == []
    (record,) = audit
    assert "secret detail" not in repr(record)


def test_a_rejected_adc_token_is_refreshed_before_the_next_call():
    responses = iter(
        [
            httpx.Response(401, json={"error": {"message": "expired"}}),
            httpx.Response(200, json=gemini_response(MODEL_DRAFT)),
        ]
    )
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return next(responses)

    creds = FakeCredentials(TOKEN)
    client = httpx.Client(transport=httpx.MockTransport(transport))
    extractor = VertexExtractor(
        Settings(_env_file=None, vertex_auth="adc"),
        lambda r: None,
        client=client,
        adc=lambda: creds,
    )

    with pytest.raises(ExtractorError, match="HTTP 401"):
        extractor.extract(make_request("Sprayed P1"))
    extractor.extract(make_request("Sprayed P1"))

    assert seen == [f"Bearer {TOKEN}", f"Bearer {TOKEN}-1"]


def test_adc_refresh_is_bounded_by_the_extractor_timeout(monkeypatch):
    import requests

    timeouts: list[object] = []

    def token_endpoint(self, method, url, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests.Session, "request", token_endpoint)

    class TokenEndpointCredentials(FakeCredentials):
        def refresh(self, request) -> None:
            request(url="https://oauth2.googleapis.com/token", method="POST")

    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    extractor = adc_vertex(recorder, [], TokenEndpointCredentials(None, valid=False))

    with pytest.raises(ExtractorError, match=r"\(TransportError\)"):
        extractor.extract(make_request("Sprayed P1"))

    # A stalled token endpoint must not hold the voice log longer than a Vertex call would.
    assert timeouts == [15.0]


def test_vertex_host_override_targets_the_eu_multi_region_host():
    recorder = Recorder(httpx.Response(200, json=gemini_response(MODEL_DRAFT)))
    audit: list[AuditRecord] = []
    overrides = {
        "vertex_host": "aiplatform.eu.rep.googleapis.com",
        "vertex_location": "eu",
        "vertex_model": "gemini-3.5-flash-lite",
    }

    vertex(recorder, audit, **overrides).extract(make_request("Sprayed P1"))

    (sent,) = recorder.requests
    assert str(sent.url) == (
        "https://aiplatform.eu.rep.googleapis.com/v1beta1/projects/example-project/locations/eu"
        "/publishers/google/models/gemini-3.5-flash-lite:generateContent"
    )
    assert audit[0].endpoint == str(sent.url)


def test_default_auth_is_adc_when_a_credentials_file_is_set(monkeypatch):
    monkeypatch.delenv("VERTEX_AUTH", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/vertex_credentials")
    assert Settings(_env_file=None).vertex_auth == "adc"


def test_default_auth_is_gcloud_without_a_credentials_file(monkeypatch):
    monkeypatch.delenv("VERTEX_AUTH", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert Settings(_env_file=None).vertex_auth == "gcloud"


def test_explicit_vertex_auth_wins_over_the_credentials_file(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/vertex_credentials")
    monkeypatch.setenv("VERTEX_AUTH", "gcloud")
    assert Settings(_env_file=None).vertex_auth == "gcloud"
