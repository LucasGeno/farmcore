"""Gemini function calling for the web assistant: the model only *picks* a read-only tool.

`VertexToolChooser.choose(question, tools)` sends one request to the chat app's Vertex endpoint
(same settings, auth and audit as `chat.extractor.vertex.VertexExtractor`, which it extends): the
question (plus bounded previous question/tool/parcel routing context when supplied) as the single
user part, and each tool's name, description and string parameters as a
function declaration. Nothing else leaves the host: no farm rows, no facts.
The reply's first `functionCall` becomes a `Choice`; any model text rides along in `Choice.text`
but the route never renders it (answers come from the tool's own text, facts and links), so a
number the model makes up is never shown. Every call, failed or not, emits one `AuditRecord`
(adapter `vertex_assistant`). Errors raise `ChooserError`; the route then uses the keyword router.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from farmcore.apps.chat.extractor.base import AuditRecord, ExtractorError
from farmcore.apps.chat.extractor.vertex import VertexExtractor, _json_or_text
from farmcore.core.tools import ToolSpec

ADAPTER = "vertex_assistant"
TIMEOUT_S = 6.0  # a web question; the keyword router answers when the model is slow
SYSTEM_INSTRUCTION = (
    "You route a vineyard farmer's question to one read-only function. Call exactly one "
    "function whose description fits the question, with a parcel alias or name only when the "
    "question names one, or a follow-up refers to the previous parcel. The input may be JSON "
    "with previous_turns (questions and routing metadata only) and current_question. Answer the "
    "current question; use previous turns only to resolve follow-ups. A new explicit parcel "
    "replaces the old one. If none fits, call no function. Never answer from your own knowledge."
)


class ChooserError(RuntimeError):
    """The model call failed or its reply could not be read."""


@dataclass(frozen=True)
class Choice:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ""  # the model's own words; never rendered


class ToolChooser(Protocol):
    def choose(self, question: str, tools: Sequence[ToolSpec]) -> Choice | None: ...


def declarations(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    out = []
    for tool in tools:
        decl: dict[str, Any] = {"name": tool.name, "description": tool.description}
        if tool.params:
            decl["parameters"] = {
                "type": "OBJECT",
                "properties": {name: {"type": kind} for name, kind in tool.params.items()},
            }
        out.append(decl)
    return out


class VertexToolChooser(VertexExtractor):
    def tool_request_body(self, question: str, tools: Sequence[ToolSpec]) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "tools": [{"functionDeclarations": declarations(tools)}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}},
        }

    def choose(self, question: str, tools: Sequence[ToolSpec]) -> Choice | None:
        body = self.tool_request_body(question, tools)
        started = self._monotonic()
        try:
            headers = self._auth_headers()
        except ExtractorError as exc:
            self._emit(body, None, None, f"not sent: {exc}", started)
            raise ChooserError(str(exc)) from exc
        status: int | None = None
        response_body: Any = None
        try:
            response = self._client.post(
                self.endpoint, json=body, headers=headers, timeout=TIMEOUT_S
            )
            status = response.status_code
            response_body = _json_or_text(response)
            if status in (401, 403):
                self._cached_token = None
                self._adc_rejected = True
            if status != 200:
                raise ChooserError(f"HTTP {status}")
            choice = _parse(response_body)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ChooserError) else type(exc).__name__
            self._emit(body, response_body, status, reason, started)
            if isinstance(exc, ChooserError):
                raise
            raise ChooserError(reason) from exc
        self._emit(body, response_body, status, None, started)
        return choice

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


def _parse(response_body: Any) -> Choice | None:
    try:
        parts = response_body["candidates"][0]["content"]["parts"]
        text = " ".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()
        call = next((p["functionCall"] for p in parts if "functionCall" in p), None)
        if call is None:
            return None
        args = call.get("args") or {}
        if not isinstance(call["name"], str) or not isinstance(args, dict):
            raise TypeError("bad functionCall")
    except (KeyError, IndexError, TypeError) as exc:
        raise ChooserError("invalid reply") from exc
    return Choice(call["name"], dict(args), text)
