"""Public read-only tool and handler fallbacks on a wholly synthetic farm.

The failing extension performs real invalid SQL within the assistant's savepoint. No internal
collaborators are patched: the next real question must succeed with the same database session.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from farmcore.apps.advisor import tools as advisor_tools
from farmcore.apps.chat.dto import Incoming
from farmcore.apps.chat.handlers import ChatDeps, on_message
from farmcore.apps.fields import tools as fields_tools
from farmcore.apps.operations import tools as operation_tools
from farmcore.apps.stations import tools as station_tools
from farmcore.core import logs, registry
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import AccessLog, Advice, Parcel
from farmcore.core.tools import ToolSpec
from tests.apps.chat.assistant.toolkit import CHAT, ctx_at, write_counts
from tests.apps.chat.test_handlers import NOW_LOCAL, seed_farm

NOW = "2024-04-10 06:00"


@pytest.fixture
def synthetic(session, settings):
    return seed_farm(session, settings)


def failing_extension(ctx):
    """A third-party tool with a database error, reached through its declared dotted path."""
    ctx.repo.execute(
        text("SELECT CAST('invalid synthetic integer' AS integer)"),
        scope="fields:read",
        resource="parcel",
    )


def test_extension_failure_rolls_back_its_savepoint_and_the_next_question_works(
    session, session_factory, settings, synthetic, caplog
):
    manifests = registry.discover()
    broken = Manifest(
        name="synthetic_extension",
        version="0.0.0",
        tools=(
            ToolSpec(
                name="synthetic_failure",
                description="Test extension failure",
                params={},
                func=f"{__name__}:failing_extension",
                scope="fields:read",
                keywords=("extension failure",),
            ),
        ),
    )
    dependencies = ChatDeps(session_factory, None, settings, tuple([*manifests, broken]))
    before = write_counts(session)

    failed = on_message(
        dependencies,
        Incoming(chat_id=CHAT, text="What is extension failure?", transcript=None, message_id=201),
    )
    recovered = on_message(
        dependencies,
        Incoming(chat_id=CHAT, text="List my parcels?", transcript=None, message_id=202),
    )

    assert failed.text.startswith("I can't answer that yet; try")
    assert failed.buttons == [] and failed.url_buttons == []
    assert "invalid synthetic integer" not in failed.text
    assert "synthetic_failure failed (DataError)" in caplog.text
    assert recovered.text.startswith("3 parcels: P1 Le Plateau, P5 Les Granges, P6 Peyrot.")
    assert write_counts(session) == before
    assert session.scalar(select(AccessLog.id).where(AccessLog.purpose == "assistant")) is not None


def test_french_tools_list_the_farms_parcels_and_unknown_parcel_help(session, settings, synthetic):
    context = ctx_at(session, settings, NOW, lang="fr")
    before = write_counts(session)

    listed = fields_tools.list_parcels(context)
    unknown = advisor_tools.parcel_status(context, parcel="Unknown synthetic parcel")

    assert listed.text == "3 parcelles : P1 Le Plateau, P5 Les Granges, P6 Peyrot."
    assert len(listed.facts["parcels"]) == 3
    assert unknown.text == (
        "Je ne connais pas cette parcelle. Vos parcelles : "
        "P1 Le Plateau, P5 Les Granges, P6 Peyrot."
    )
    assert unknown.facts == {
        "error": "unknown parcel",
        "parcels": [
            "P1 Le Plateau",
            "P5 Les Granges",
            "P6 Peyrot",
        ],
    }
    assert write_counts(session) == before


@pytest.mark.parametrize("decision", ["spray", "wait"])
def test_french_advice_with_a_window_mentions_stored_washoff(
    session, settings, synthetic, decision
):
    parcel = session.get(Parcel, synthetic["P1"])
    session.add(
        Advice(
            farm_id=parcel.farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW_LOCAL,
            decision=decision,
            next_window_start=NOW_LOCAL.replace(hour=8),
            next_window_end=NOW_LOCAL.replace(hour=10),
            products=[{"name": "Synthetic Copper"}],
            props={"flags": ["washed_off"]},
        )
    )
    session.commit()
    before = write_counts(session)

    result = advisor_tools.parcel_status(ctx_at(session, settings, NOW, lang="fr"), parcel="P1")

    expected = "À traiter" if decision == "spray" else "Prochaine fenêtre"
    assert result.text.splitlines()[1] == (
        f"{expected} avec Synthetic Copper dès le 10 avr. 08:00 "
        "(le dernier traitement a été lessivé par la pluie)."
    )
    assert result.facts["flags"] == ["washed_off"]
    assert write_counts(session) == before


def test_done_spray_without_rates_is_unknown_dose_not_zero(session, settings, synthetic):
    farm_id = session.get(Parcel, synthetic["P1"]).farm_id
    logs.record_operation(
        session,
        farm_id=farm_id,
        type="spray",
        parcel_ids=[synthetic["P1"]],
        ts_start=NOW_LOCAL.replace(hour=5),
        ts_end=None,
        quantities=[],
        source=logs.SourceInput("isoxml", "synthetic-no-rates", {}, None, logs.RANK_FILE),
        actor="synthetic-test",
        occurred_at=NOW_LOCAL,
    )
    session.commit()
    before = write_counts(session)

    result = operation_tools.last_spray(ctx_at(session, settings, NOW), parcel="P1")

    assert result.text == "P1 Le Plateau was last sprayed on 10 Apr 05:00: ?, no dose."
    assert result.facts["products"] == []
    assert result.facts["sources"] == ["isoxml"]
    assert "dose" not in result.facts and "unit" not in result.facts
    assert write_counts(session) == before


def test_weather_without_a_station_says_unavailable(session, settings, synthetic):
    before = write_counts(session)

    result = station_tools.weather(ctx_at(session, settings, NOW), day="tomorrow")

    assert result.facts == {"error": "no station"}
    assert result.text == "No weather station is set up."
    assert result.links == ()
    assert write_counts(session) == before


def test_french_unknown_weather_day_asks_for_a_date(session, settings, synthetic):
    result = station_tools.weather(ctx_at(session, settings, NOW, lang="fr"), day="not a day")

    assert result.facts == {"error": "unknown day"}
    assert result.text == "Quel jour ? Dites aujourd'hui, demain ou une date (AAAA-MM-JJ)."


def test_french_evaluated_no_action_needs_no_invented_second_line(session, settings, synthetic):
    parcel = session.get(Parcel, synthetic["P1"])
    session.add(
        Advice(
            farm_id=parcel.farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW_LOCAL,
            decision="none",
            props={"headline": "Synthetic evaluated calm parcel"},
        )
    )
    session.commit()

    result = advisor_tools.parcel_status(ctx_at(session, settings, NOW, lang="fr"), parcel="P1")

    assert result.text == "P1 Le Plateau : rien à faire."
    assert result.facts["decision"] == "none"
    assert result.facts["headline"] == "Synthetic evaluated calm parcel"
    assert result.facts["next_window"] is None
