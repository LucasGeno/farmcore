"""Fertilization assistant tool (read-only): `fertilization_season()`, this season's confirmed
applications up to now, latest first: local date, parcels, fertiliser, amount as entered and the
declared composition with its source. There is no tool that records one: that is a draft the
farmer confirms.
"""

from __future__ import annotations

from typing import Any

from farmcore.apps.fertilization import views
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec, object_schema

SCOPE = "logs:read"

TOOLS = (
    ToolSpec(
        name="fertilization_season",
        description=(
            "Fertiliser applied this season up to now: local date, parcels, fertiliser, amount "
            "as entered and the declared nutrient composition with its source."
        ),
        params=object_schema(),
        effect="read",
        func="farmcore.apps.fertilization.tools:season",
        scope=SCOPE,
        keywords=(
            "fertiliser",
            "fertilizer",
            "fertilization",
            "nitrogen",
            "engrais",
            "fertilisation",
            "azote",
        ),
    ),
)


def season(ctx: ToolContext, **_: Any) -> ToolResult:
    rows = views.season_rows(ctx.repo, now=ctx.now, tz=ctx.tz.key)
    applications = [
        {
            "date": row.local_start.strftime("%Y-%m-%d"),
            "parcels": row.parcel_names,
            "fertiliser": row.fertiliser,
            "amount": row.amount,
            "composition": row.composition,
        }
        for row in rows
    ]
    if not applications:
        text = "No fertiliser application is recorded this season."
    else:
        text = "; ".join(
            f"{a['date']}: {a['fertiliser']} {a['amount']} on {', '.join(a['parcels'])}"
            for a in applications
        )
    return ToolResult(
        facts={"applications": applications},
        text=text,
        links=(("Fertilization", "/fertilization/"),),
    )
