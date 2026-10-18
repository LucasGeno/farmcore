"""Database `AuditSink`: one `llm_audit` row per model call, with the exact request body.

Each record is written and committed in its own session, so a call stays audited even when the
message that caused it fails later. `audit_ref(ref)` tags the rows written inside it with the
chat message they came from (`tg:<chat_id>:<message_id>`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.orm import Session, sessionmaker

from farmcore.apps.chat.extractor import AuditRecord
from farmcore.core.models.canonical import LlmAudit
from farmcore.db import session_scope

# `llm_audit.status` for a call that failed before anything was sent (e.g. no auth token).
STATUS_NOT_SENT = "not sent"

_REF: ContextVar[str | None] = ContextVar("llm_audit_ref", default=None)


@contextmanager
def audit_ref(ref: str | None) -> Iterator[None]:
    token = _REF.set(ref)
    try:
        yield
    finally:
        _REF.reset(token)


class DbAuditSink:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self, record: AuditRecord) -> None:
        with session_scope(self._session_factory) as session:
            session.add(
                LlmAudit(
                    adapter=record.adapter,
                    model=record.model,
                    endpoint=record.endpoint,
                    request_body=record.request_body,
                    response_body=record.response_body,
                    status=STATUS_NOT_SENT if record.status is None else str(record.status),
                    error=record.error,
                    latency_ms=record.latency_ms,
                    ref=_REF.get(),
                )
            )
