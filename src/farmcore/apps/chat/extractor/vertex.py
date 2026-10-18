"""Vertex AI adapter: Gemini on the EU regional endpoint, JSON-schema output.

Only `request_payload(req)` leaves the host, as the single user text part. Every call, failed or
not, emits one AuditRecord with the exact request body; auth travels in headers only.

Auth (`settings.vertex_auth`), always `Authorization: Bearer`: `adc` uses Application Default
Credentials (`GOOGLE_APPLICATION_CREDENTIALS` service-account file; containers), refreshed by
google-auth when not valid or near expiry and cached; `gcloud` runs `gcloud auth print-access-token`
(cached 50 min; laptop); `token` uses a fixed `VERTEX_ACCESS_TOKEN`. No API key is ever sent: the
regional host rejects them (401, verified 16 Oct). A 401/403 drops the cached gcloud token, or
forces an ADC refresh, before the next call. When auth fails before sending, the AuditRecord has
`status=None` and an error starting "not sent:", so the payload view still shows why.
"""

from __future__ import annotations

import functools
import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import google.auth
import google.auth.exceptions
import google.auth.transport.requests
import httpx
from pydantic import BaseModel, ValidationError

from farmcore.apps.chat.extractor.base import (
    DOSE_UNITS,
    OPERATION_TYPES,
    SYSTEM_INSTRUCTION,
    AuditRecord,
    AuditSink,
    DraftOperation,
    ExtractorError,
    ExtractRequest,
    ProductDose,
    finalize,
    request_payload,
)
from farmcore.settings import Settings

ADAPTER = "vertex"
TIMEOUT_S = 15.0
GCLOUD_TOKEN_TTL_S = 50 * 60
GCLOUD_CMD = ["gcloud", "auth", "print-access-token"]
AUTH_REJECTED = (401, 403)
ADC_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Vertex `Schema` (OpenAPI subset) for the model's answer; `adapter` is ours, not the model's.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "type": {"type": "STRING", "enum": list(OPERATION_TYPES)},
        "parcels": {"type": "ARRAY", "items": {"type": "STRING"}},
        "products": {"type": "ARRAY", "items": {"type": "STRING"}},
        "dose_value": {"type": "NUMBER", "nullable": True},
        "dose_unit": {"type": "STRING", "enum": list(DOSE_UNITS), "nullable": True},
        "product_doses": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "product": {"type": "STRING"},
                    "value": {"type": "NUMBER"},
                    "unit": {"type": "STRING", "enum": list(DOSE_UNITS)},
                },
                "required": ["product", "value", "unit"],
            },
        },
        "ts_start": {"type": "STRING", "nullable": True},
        "confidence": {"type": "NUMBER"},
        "notes": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["type", "parcels", "products", "dose_value", "dose_unit", "ts_start"],
    "propertyOrdering": [
        "type",
        "parcels",
        "products",
        "dose_value",
        "dose_unit",
        "ts_start",
        "confidence",
        "notes",
    ],
}


class _ModelDraft(BaseModel):
    type: str = "spray"
    parcels: list[str] = []
    products: list[str] = []
    dose_value: float | None = None
    dose_unit: str | None = None
    product_doses: list[ProductDose] = []
    ts_start: str | None = None
    confidence: float = 0.0
    notes: list[str] = []


def _load_adc() -> Any:
    credentials, _project = google.auth.default(scopes=ADC_SCOPES)
    return credentials


def _run_gcloud(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, check=True)
    return done.stdout


class VertexExtractor:
    def __init__(
        self,
        settings: Settings,
        audit: AuditSink,
        *,
        client: httpx.Client | None = None,
        gcloud: Callable[[list[str]], str] = _run_gcloud,
        monotonic: Callable[[], float] = time.monotonic,
        adc: Callable[[], Any] = _load_adc,
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._gcloud = gcloud
        self._monotonic = monotonic
        self._cached_token: tuple[str, float] | None = None
        self._adc = adc
        self._credentials: Any = None
        self._adc_rejected = False

    @property
    def model(self) -> str:
        return self._settings.vertex_model

    @property
    def endpoint(self) -> str:
        s = self._settings
        host = s.vertex_host or f"{s.vertex_location}-aiplatform.googleapis.com"
        return (
            f"https://{host}/v1beta1/projects/"
            f"{s.vertex_project}/locations/{s.vertex_location}/publishers/google/models/"
            f"{s.vertex_model}:generateContent"
        )

    def request_body(self, req: ExtractRequest) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [
                {"role": "user", "parts": [{"text": json.dumps(request_payload(req))}]},
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                # gemini-2.5-flash thinks by default; plain extraction does not need it.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

    def extract(self, req: ExtractRequest) -> DraftOperation:
        body = self.request_body(req)
        started = self._monotonic()
        try:
            headers = self._auth_headers()
        except ExtractorError as exc:
            # Nothing was sent; the record says so and shows the body that would have gone.
            self._emit(body, None, None, f"not sent: {exc}", started)
            raise
        status: int | None = None
        response_body: Any = None
        try:
            response = self._client.post(
                self.endpoint, json=body, headers=headers, timeout=TIMEOUT_S
            )
            status = response.status_code
            response_body = _json_or_text(response)
            if status in AUTH_REJECTED:
                self._cached_token = None
                self._adc_rejected = True
            if status != 200:
                raise ExtractorError(f"HTTP {status}")
            draft = _parse(response_body)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ExtractorError) else type(exc).__name__
            self._emit(body, response_body, status, reason, started)
            if isinstance(exc, ExtractorError):
                raise
            raise ExtractorError(reason) from exc
        self._emit(body, response_body, status, None, started)
        return finalize(draft, req)

    def _emit(
        self, body: dict, response_body: Any, status: int | None, error: str | None, started: float
    ) -> None:
        self._audit(
            AuditRecord(
                adapter=ADAPTER,
                model=self.model,
                endpoint=self.endpoint,
                request_body=body,
                response_body=response_body,
                status=status,
                error=error,
                latency_ms=round((self._monotonic() - started) * 1000),
            )
        )

    def _auth_headers(self) -> dict[str, str]:
        s = self._settings
        if s.vertex_auth == "token":
            if s.vertex_access_token is None:
                raise ExtractorError("no VERTEX_ACCESS_TOKEN set")
            return {"Authorization": f"Bearer {s.vertex_access_token.get_secret_value()}"}
        if s.vertex_auth == "adc":
            return {"Authorization": f"Bearer {self._adc_token()}"}
        return {"Authorization": f"Bearer {self._gcloud_token()}"}

    def _adc_token(self) -> str:
        try:
            if self._credentials is None:
                self._credentials = self._adc()
            if self._adc_rejected or not self._credentials.valid:
                # google-auth's own default is 120 s; a stalled refresh must fall back fast.
                request = google.auth.transport.requests.Request()
                self._credentials.refresh(functools.partial(request, timeout=TIMEOUT_S))
                self._adc_rejected = False
        except google.auth.exceptions.GoogleAuthError as exc:
            # The class name only: provider messages can echo account or key details.
            raise ExtractorError(f"ADC credentials failed ({type(exc).__name__})") from exc
        return self._credentials.token

    def _gcloud_token(self) -> str:
        now = self._monotonic()
        if self._cached_token and now - self._cached_token[1] < GCLOUD_TOKEN_TTL_S:
            return self._cached_token[0]
        try:
            token = self._gcloud(list(GCLOUD_CMD)).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExtractorError(f"gcloud token failed ({type(exc).__name__})") from exc
        if not token:
            raise ExtractorError("gcloud returned no token")
        self._cached_token = (token, now)
        return token


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _parse(response_body: Any) -> DraftOperation:
    try:
        parts = response_body["candidates"][0]["content"]["parts"]
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        raw = _ModelDraft.model_validate(json.loads(text))
        ts_start = _parse_ts(raw.ts_start)
    except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
        raise ExtractorError("invalid JSON") from exc
    return DraftOperation(
        type=raw.type,
        parcels=raw.parcels,
        products=raw.products,
        dose_value=raw.dose_value,
        dose_unit=raw.dose_unit,
        product_doses=raw.product_doses,
        ts_start=ts_start,
        confidence=raw.confidence,
        adapter=ADAPTER,
        notes=raw.notes,
    )


def _parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
