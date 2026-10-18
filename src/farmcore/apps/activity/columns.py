"""The Activity page's columns: key, header, shown by default, and the cell text per row.

A cell shows only what the row's data has; anything missing is `DASH`, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from farmcore.core.actors import mask_actor

DASH = "—"
ALWAYS = "what"  # never hidden: a row with no sentence says nothing
COOKIE = "farmcore_activity_cols_{farm_id}"  # per browser and per farm (no DB write)
# Plain words for the actors core and the apps write (`event.actor`); anything else is masked.
ACTOR_WORDS = {
    "owner": "Farmer (web)",
    "web:farmer": "Farmer (web assistant)",
    "cli:operations.import-isoxml": "Machine-file import (CLI)",
    "seed": "Demo data",
}


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    default: bool


COLUMNS: tuple[Column, ...] = (
    Column("time", "Time", True),
    Column("what", "What", True),
    Column("who", "Who", False),
    Column("parcel", "Parcel", True),
    Column("product", "Product / dose", True),
    Column("source", "Source", True),
    # The file's device list, not the machine that sprayed: a task does not name its device.
    Column("device", "Machine file device(s)", False),
    Column("farm", "Farm", False),
)
KEYS = tuple(c.key for c in COLUMNS)
DEFAULT_KEYS = tuple(c.key for c in COLUMNS if c.default)


def chosen(keys: list[str] | None) -> list[Column]:
    """The columns to show, in `COLUMNS` order: `keys` (unknown ones ignored) plus `ALWAYS`;
    `None` (no choice yet) is the default set."""
    wanted = set(DEFAULT_KEYS if keys is None else keys) | {ALWAYS}
    return [c for c in COLUMNS if c.key in wanted]


def parse_cookie(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [k for k in value.split(",") if k in KEYS]


def who(actor: str) -> str:
    """`chat:123456789` -> `Telegram chat …6789`; `job:*` -> `Automatic (job:…)`."""
    if actor in ACTOR_WORDS:
        return ACTOR_WORDS[actor]
    masked = mask_actor(actor)
    if actor.startswith(("chat:", "tg:")):
        return f"Telegram {masked}"
    if actor.startswith("job:"):
        return f"Automatic ({masked})"
    return masked


def cells(row: dict[str, Any], farm_name: str) -> dict[str, str]:
    """Plain text per column key for one `activity_rows` row (`what` is rendered by the template
    from `text` and `decision`)."""
    return {
        "time": row["when"],
        "what": row["text"],
        "who": who(row["actor"]) if row.get("actor") else DASH,
        "device": ", ".join(row.get("devices") or []) or DASH,
        "farm": farm_name or DASH,
        "parcel": ", ".join(row["parcels"]) or DASH,
        "product": " + ".join(row["products"]) or DASH,
        "source": ", ".join(s["label"] for s in row["sources"]) or DASH,
    }
