"""Primary adapter with a deterministic fallback: any primary failure says so in the notes."""

from __future__ import annotations

from farmcore.apps.chat.extractor.base import (
    DraftOperation,
    Extractor,
    ExtractorError,
    ExtractRequest,
)


class FallbackExtractor:
    def __init__(self, primary: Extractor, fallback: Extractor) -> None:
        self.primary = primary
        self.fallback = fallback

    def extract(self, req: ExtractRequest) -> DraftOperation:
        try:
            return self.primary.extract(req)
        except Exception as exc:  # any primary failure, by design
            reason = str(exc) if isinstance(exc, ExtractorError) else type(exc).__name__
            draft = self.fallback.extract(req)
            return draft.model_copy(
                update={"notes": [*draft.notes, f"fallback: {draft.adapter} ({reason})"]}
            )
