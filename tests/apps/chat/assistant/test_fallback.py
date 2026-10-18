"""Keyword fallback, no database: which of the apps' real tools a question picks, and the `day`
argument it reads. `names_parcel` stands in for the parcel lookup the handler does on the DB."""

from __future__ import annotations

import pytest

from farmcore.apps.advisor import tools as advisor_tools
from farmcore.apps.chat.assistant import fallback
from farmcore.apps.fields import tools as fields_tools
from farmcore.apps.operations import tools as operations_tools
from farmcore.apps.stations import tools as stations_tools

# Load order of the real manifests: fields, operations, advisor, stations.
TOOLS = (
    *fields_tools.TOOLS,
    *operations_tools.TOOLS,
    *advisor_tools.TOOLS,
    *stations_tools.TOOLS,
)


@pytest.mark.parametrize(
    ("question", "names_parcel", "tool"),
    [
        # The four demo questions.
        ("When did I last spray Le Plateau?", True, "operations_last_spray"),
        ("What do I spray today?", False, "advisor_today"),
        ("Is Peyrot protected?", True, "advisor_parcel_status"),
        ("Weather tomorrow?", False, "stations_weather"),
        # Farm-wide protection, no parcel named: the whole farm's advice, not a parcel list.
        ("Are my parcels protected?", False, "advisor_today"),
        ("Is everything protected?", False, "advisor_today"),
        ("Mes parcelles sont-elles protégées ?", False, "advisor_today"),
        ("Do I need to spray today?", False, "advisor_today"),
        ("How much rain since the spray?", False, "advisor_today"),
        ("Combien de pluie depuis le traitement ?", False, "advisor_today"),
        # One parcel named: that parcel's status.
        ("Do I need to spray Peyrot?", True, "advisor_parcel_status"),
        ("Does Peyrot need to spray?", True, "advisor_parcel_status"),
        ("What should I spray on Peyrot?", True, "advisor_parcel_status"),
        ("How much rain on Peyrot since the spray?", True, "advisor_parcel_status"),
        ("Combien de pluie sur Peyrot depuis le traitement ?", True, "advisor_parcel_status"),
        # "it" is a parcel: status, which then asks which one.
        ("Is it protected?", False, "advisor_parcel_status"),
        # Still the parcel list and the weather.
        ("List my parcels", False, "fields_list_parcels"),
        ("How much rain tomorrow?", False, "stations_weather"),
    ],
)
def test_choose(question: str, names_parcel: bool, tool: str) -> None:
    chosen = fallback.choose(TOOLS, question, names_parcel=names_parcel)

    assert chosen is not None and chosen.name == tool


@pytest.mark.parametrize(
    ("question", "day"),
    [
        ("Weather tomorrow?", "tomorrow"),
        ("Météo demain ?", "tomorrow"),
        ("Météo après-demain ?", "day after tomorrow"),
        ("Weather the day after tomorrow?", "day after tomorrow"),
        ("Rain yesterday?", "yesterday"),
        ("Pluie hier ?", "yesterday"),
        ("Pluie avant-hier ?", "day before yesterday"),
        ("Rain the day before yesterday?", "day before yesterday"),
        ("Weather on 2024-04-12?", "2024-04-12"),
        ("Weather?", "today"),
    ],
)
def test_day(question: str, day: str) -> None:
    assert fallback._day(question) == day


def action_tool(ctx):
    from farmcore.core.tools import ToolResult

    return ToolResult(
        {}, "Open the register.", (("Register", "/operations/register?view=all&farm=999#recent"),)
    )


@pytest.mark.parametrize("base", ["https://agri.example.test", "http://localhost:8000"])
def test_answer_actions_pin_context_farm_without_mutating_tool_paths(session, settings, base):
    from farmcore.apps.chat.assistant.context import tool_context
    from farmcore.core.tools import ToolSpec
    from tests.apps.chat.test_handlers import CHAT, NOW_LOCAL, seed_farm

    seed_farm(session, settings)
    ctx = tool_context(session, settings, CHAT, NOW_LOCAL)
    spec = ToolSpec(
        "qa_actions",
        "Synthetic read action",
        {},
        f"{__name__}:action_tool",
        "logs:read",
        ("register",),
    )

    out = fallback.answer("Show the register", [spec], ctx, base)

    url = f"{base}/operations/register?view=all&farm={ctx.repo.farm_id}#recent"
    if base.startswith("https:"):
        assert out.url_buttons == [("Register", url)]
    else:
        assert out.url_buttons == []
        assert f"Register: {url}" in out.text
    assert action_tool(ctx).links == (
        ("Register", "/operations/register?view=all&farm=999#recent"),
    )
