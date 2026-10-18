"""Bounded browser preference, never permission or farm data."""

import re
from urllib.parse import unquote

COOKIE = "farmcore_notifications_read_{farm_id}"
MAX_LENGTH = 3000
MAX_ITEMS = 100
KEY = re.compile(r"[0-9a-f]{24}")


def read_keys(value: str | None) -> set[str]:
    if not value or len(value) > MAX_LENGTH:
        return set()
    parts = unquote(value).split(",")
    if len(parts) > MAX_ITEMS or any(not KEY.fullmatch(p) for p in parts):
        return set()
    return set(parts)
