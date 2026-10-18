"""Extractor seam: `get_extractor(settings, audit).extract(ExtractRequest) -> DraftOperation`."""

from __future__ import annotations

from farmcore.apps.chat.extractor.base import (
    SYSTEM_INSTRUCTION,
    AuditRecord,
    AuditSink,
    DraftOperation,
    Extractor,
    ExtractorError,
    ExtractRequest,
    ParcelRef,
    ProductRef,
    request_payload,
)
from farmcore.apps.chat.extractor.fake import FakeExtractor
from farmcore.apps.chat.extractor.fallback import FallbackExtractor
from farmcore.apps.chat.extractor.regex import RegexExtractor
from farmcore.apps.chat.extractor.vertex import VertexExtractor
from farmcore.settings import Settings

__all__ = [
    "SYSTEM_INSTRUCTION",
    "AuditRecord",
    "AuditSink",
    "DraftOperation",
    "ExtractRequest",
    "Extractor",
    "ExtractorError",
    "FakeExtractor",
    "FallbackExtractor",
    "ParcelRef",
    "ProductRef",
    "RegexExtractor",
    "VertexExtractor",
    "get_extractor",
    "request_payload",
]


def get_extractor(settings: Settings, audit: AuditSink) -> Extractor:
    """`settings.extractor`: vertex (with regex fallback) | regex | fake.

    `vertex` without `VERTEX_PROJECT` is the regex extractor: no cloud call is attempted.
    """
    if settings.extractor == "vertex" and settings.vertex_project:
        return FallbackExtractor(VertexExtractor(settings, audit), RegexExtractor())
    if settings.extractor in ("vertex", "regex"):
        return RegexExtractor()
    return FakeExtractor()
