"""`GET /chat/`: every recent model call with the exact payload it carried (admins), or a
sanitised demo view of the same calls (everyone else): chat ids masked, the transcript replaced
by its length, the payload by its field names. No raw `tg:` id or value reaches a non-admin.

`llm_audit` has no farm column, so it is read through `ScopedRepository.execute` with scope
`audit:read`; the route only renders plain values, after the repository has committed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from farmcore.core.access import Repo
from farmcore.core.models.canonical import LlmAudit
from farmcore.web import is_admin, render

router = APIRouter()

RECENT_CALLS = 20
VERTEX = "vertex"


@dataclass(frozen=True)
class CallView:
    ts: datetime
    adapter: str  # "vertex", or "vertex failed → regex fallback"
    ok: bool
    model: str
    endpoint: str
    status: str
    error: str | None
    latency_ms: int | None
    ref: str | None
    payload: str  # what the model saw: the user part, pretty JSON
    request_body: str
    response_body: str


@dataclass(frozen=True)
class DemoCallView:
    """What a non-admin sees of one call: no transcript, no ids, no payload values."""

    ts: datetime
    adapter: str
    ok: bool
    model: str
    status: str
    latency_ms: int | None
    chat: str | None  # "chat …8777"
    transcript_chars: int | None
    fields: list[str]


MASK_KEEP = 4  # trailing chat-id digits shown to non-admins


def _pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)


def _user_payload(request_body: dict[str, Any]) -> str:
    """The user text part(s), parsed back from JSON when possible."""
    texts = [
        part["text"]
        for content in request_body.get("contents", [])
        if content.get("role") == "user"
        for part in content.get("parts", [])
        if "text" in part
    ]
    out = []
    for text in texts:
        try:
            out.append(_pretty(json.loads(text)))
        except ValueError:
            out.append(text)
    return "\n".join(out) or _pretty(request_body)


def _user_parts(request_body: dict[str, Any]) -> list[Any]:
    """The user text part(s), each parsed back from JSON when possible."""
    out: list[Any] = []
    for content in request_body.get("contents", []):
        if content.get("role") != "user":
            continue
        for part in content.get("parts", []):
            if "text" not in part:
                continue
            try:
                out.append(json.loads(part["text"]))
            except ValueError:
                out.append(part["text"])
    return out


def _masked_chat(ref: str | None) -> str | None:
    """`tg:<chat_id>:<message_id>` -> `chat …8777`.

    Only a Telegram ref whose chat id is longer than MASK_KEEP keeps its tail; anything
    else (a short id, another ref shape) shows no digits at all.
    """
    if not ref:
        return None
    prefix, _, rest = ref.partition(":")
    chat_id = rest.split(":", 1)[0]
    if prefix != "tg" or len(chat_id) <= MASK_KEEP:
        return "chat …"
    return f"chat …{chat_id[-MASK_KEEP:]}"


def _demo_view(row: LlmAudit) -> DemoCallView:
    ok, adapter = _outcome(row)
    chars: int | None = None
    fields: list[str] = []
    for part in _user_parts(row.request_body or {}):
        if isinstance(part, dict):
            fields += [str(k) for k in part if str(k) not in fields]
            if isinstance(part.get("transcript"), str):
                chars = (chars or 0) + len(part["transcript"])
        elif isinstance(part, str):
            chars = (chars or 0) + len(part)
    return DemoCallView(
        ts=row.ts,
        adapter=adapter,
        ok=ok,
        model=row.model,
        status=row.status,
        latency_ms=row.latency_ms,
        chat=_masked_chat(row.ref),
        transcript_chars=chars,
        fields=fields,
    )


def _outcome(row: LlmAudit) -> tuple[bool, str]:
    ok = row.error is None and row.status == "200"
    adapter = row.adapter if ok or row.adapter != VERTEX else "vertex failed → regex fallback"
    return ok, adapter


def _view(row: LlmAudit) -> CallView:
    ok, adapter = _outcome(row)
    return CallView(
        ts=row.ts,
        adapter=adapter,
        ok=ok,
        model=row.model,
        endpoint=row.endpoint,
        status=row.status,
        error=row.error,
        latency_ms=row.latency_ms,
        ref=row.ref,
        payload=_user_payload(row.request_body or {}),
        request_body=_pretty(row.request_body),
        response_body=_pretty(row.response_body),
    )


# Transcripts and chat ids are personal data: only admins see them on a shared host.
@router.get("/", response_class=HTMLResponse)
def chat_page(request: Request, repo: Repo) -> HTMLResponse:
    admin = is_admin(request)
    rows = repo.execute(
        select(LlmAudit).order_by(LlmAudit.id.desc()).limit(RECENT_CALLS),
        scope="audit:read",
        resource="llm_audit",
    ).scalars()
    view = _view if admin else _demo_view
    calls = [view(row) for row in rows]
    return render(request, "chat/index.html", calls=calls, is_admin=admin)
