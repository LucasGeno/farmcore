"""Test double: returns a configured draft and records every request."""

from __future__ import annotations

from farmcore.apps.chat.extractor.base import DraftOperation, ExtractRequest


class FakeExtractor:
    def __init__(self, draft: DraftOperation | None = None) -> None:
        self.draft = draft or DraftOperation(adapter="fake")
        self.requests: list[ExtractRequest] = []

    def extract(self, req: ExtractRequest) -> DraftOperation:
        self.requests.append(req)
        return self.draft.model_copy(deep=True)
