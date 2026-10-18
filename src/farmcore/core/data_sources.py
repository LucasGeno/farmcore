"""Data source display contract. Apps own their queries and declare providers in Manifest."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SourceSummary:
    key: str
    label: str
    unit: str
    count: int | None = None
    last_at: datetime | None = None
    note: str = ""
