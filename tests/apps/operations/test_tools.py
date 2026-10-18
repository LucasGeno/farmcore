"""`operations_last_spray` on the seeded Scene B farm (9 May 2024 08:00 copper, 2 kg/ha, every
parcel): name variants, unknown parcel, clock bound, facts allowlist, no writes."""

from __future__ import annotations

import pytest

from farmcore.apps.operations import tools
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local
from tests.apps.chat.assistant.toolkit import CHAT, assistant_reads, ctx_at, keys, write_counts

pytestmark = pytest.mark.data

manifests, seeded = scenes.manifests, scenes.seeded

# The facts the model chooser may send, from the tool contract ("products" holds name and
# active_substance). Literal on purpose: widening the allowlist is a test change.
LAST_SPRAY_FACTS = {"parcel", "date", "products", "name", "active_substance", "dose", "unit"}
LAST_SPRAY_FACTS |= {"sources", "parts"}
# The unknown-parcel error fact : {"error": "unknown parcel", "parcels": [...]}.
ERROR_FACTS = {"error", "parcels"}

COPPER = {"name": "BOUILLIE BORDELAISE RSR DISPERSS NC", "active_substance": "copper"}


@pytest.mark.parametrize("said", ["Le Plateau", "le platéau", "P1", "p1"])
def test_last_spray_after_scene_b(session, settings, seeded, said):
    seeded("B")
    ctx = ctx_at(session, settings, "2024-05-15 06:00")

    result = tools.last_spray(ctx, parcel=said)

    assert result.facts == {
        "parcel": "P1 Le Plateau",
        "date": "2024-05-09T08:00:00+02:00",
        "products": [{**COPPER, "dose": 2.0, "unit": "kg/ha"}],
        "dose": 2.0,
        "unit": "kg/ha",
        "sources": ["seed"],
    }
    assert keys(result.facts) <= LAST_SPRAY_FACTS
    assert result.text.splitlines()[0] == (
        "P1 Le Plateau was last sprayed on 09 May 08:00: copper, 2 kg/ha."
    )
    assert len(result.text.splitlines()) <= 3
    assert ("Open register", "/operations/register") in result.links


def test_a_spray_after_the_clock_is_hidden(session, settings, seeded):
    seeded("B")  # the 9 May spray is after 10 Apr
    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), "copper-contact")
    confirm_spray(session, settings, "P1", local("2024-04-12 07:00"), "sulphur-wettable")
    ctx = ctx_at(session, settings, "2024-04-10 06:00")

    result = tools.last_spray(ctx, parcel="P1")

    assert result.facts["date"] == "2024-04-10T05:30:00+02:00"
    assert result.facts["products"] == [{**COPPER, "dose": 2.0, "unit": "kg/ha"}]
    assert result.facts["sources"] == ["voice"]


def test_no_spray_up_to_now(session, settings, seeded):
    seeded("B")
    ctx = ctx_at(session, settings, "2024-04-10 06:00")

    result = tools.last_spray(ctx, parcel="Peyrot")

    assert result.facts == {"parcel": "P6 Peyrot", "date": None}
    assert result.text == "No spray is logged on P6 Peyrot yet."


def test_french_text(session, settings, seeded):
    seeded("B")
    ctx = ctx_at(session, settings, "2024-05-15 06:00", lang="fr")

    result = tools.last_spray(ctx, parcel="Peyrot")

    assert result.text.splitlines()[0] == (
        "Dernier traitement de P6 Peyrot : 09/05 08:00, copper, 2 kg/ha."
    )


def test_unknown_parcel_returns_the_error_fact_with_the_parcels(session, settings, seeded):
    seeded("B")
    ctx = ctx_at(session, settings, "2024-05-15 06:00")

    result = tools.last_spray(ctx, parcel="Le Château")

    assert result.facts["error"] == "unknown parcel"
    assert result.facts["parcels"][0] == "P1 Le Plateau"
    assert len(result.facts["parcels"]) == 10
    assert keys(result.facts) <= ERROR_FACTS


def test_reads_as_the_assistant_and_writes_nothing(session, settings, seeded):
    seeded("B")
    before = write_counts(session)
    ctx = ctx_at(session, settings, "2024-05-15 06:00")

    tools.last_spray(ctx, parcel="P5")

    reads = assistant_reads(session)
    assert (f"chat:{CHAT}", "log", "logs:read") in reads
    assert write_counts(session) == before


def test_declared_tool():
    [spec, recent] = tools.TOOLS
    assert (spec.name, spec.scope, spec.params) == (
        "operations_last_spray",
        "logs:read",
        {"parcel": "STRING"},
    )
    assert "last spray" in spec.keywords
    assert (recent.name, recent.scope, recent.params) == (
        "operations_recent_sprays",
        "logs:read",
        {},
    )
    assert "which parcels did i spray" in recent.keywords


def _record_p5(session, settings, quantities) -> None:
    """One done spray on P5 at 10 Apr 05:30 with `quantities` [(value, unit, product key or
    None, label)], recorded through the public logs API."""
    from decimal import Decimal

    from sqlalchemy import select

    from farmcore.apps.operations import seed as ops_seed
    from farmcore.core import logs
    from farmcore.core.models.canonical import Parcel, Product
    from farmcore.core.seed import ensure_default_farm

    farm = ensure_default_farm(session, settings)
    parcel = session.scalar(select(Parcel).where(Parcel.alias == "P5"))

    def product_id(key: str | None) -> int | None:
        if key is None:
            return None
        name = next(p.name for p in ops_seed.SHORTLIST if p.key == key)
        return session.scalar(select(Product.id).where(Product.name == name))

    ts = local("2024-04-10 05:30")
    logs.record_operation(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[parcel.id],
        ts_start=ts,
        ts_end=None,
        quantities=[
            logs.QuantityInput("rate", Decimal(v), unit, product_id(key), label)
            for v, unit, key, label in quantities
        ],
        source=logs.SourceInput("voice", "test:P5:rates", {}, 0.9, logs.RANK_VOICE),
        actor="test",
        occurred_at=ts,
    )
    session.commit()


SULPHUR_NAME = "MICROTHIOL SPECIAL DISPERSS"


def test_a_tank_mix_keeps_each_products_own_dose(session, settings, seeded):
    seeded("B")
    _record_p5(
        session,
        settings,
        [("1.94", "kg/ha", "copper-contact", None), ("2.06", "kg/ha", "sulphur-wettable", None)],
    )
    ctx = ctx_at(session, settings, "2024-04-10 06:00")

    result = tools.last_spray(ctx, parcel="P5")

    assert result.facts["products"] == [
        {**COPPER, "dose": 1.94, "unit": "kg/ha"},
        {"name": SULPHUR_NAME, "active_substance": "sulphur", "dose": 2.06, "unit": "kg/ha"},
    ]
    assert "dose" not in result.facts  # no single dose for two products
    assert keys(result.facts) <= LAST_SPRAY_FACTS
    assert result.text.splitlines()[0] == (
        "P5 Les Granges was last sprayed on 10 Apr 05:30: copper 1.94 kg/ha + sulphur 2.06 kg/ha."
    )


def test_two_rates_of_one_product_in_one_unit_add_up_and_show_their_parts(
    session, settings, seeded
):
    # Review R3: the second rate was silently dropped (1 kg/ha shown for 1 + 2 stored).
    seeded("B")
    _record_p5(
        session,
        settings,
        [("1", "kg/ha", "copper-contact", None), ("2", "kg/ha", "copper-contact", None)],
    )
    ctx = ctx_at(session, settings, "2024-04-10 06:00")

    result = tools.last_spray(ctx, parcel="P5")

    assert result.facts["products"] == [
        {**COPPER, "dose": 3.0, "unit": "kg/ha", "parts": [1.0, 2.0]}
    ]
    assert (result.facts["dose"], result.facts["unit"]) == (3.0, "kg/ha")
    assert keys(result.facts) <= LAST_SPRAY_FACTS
    assert result.text.splitlines()[0] == (
        "P5 Les Granges was last sprayed on 10 Apr 05:30: copper, 3 kg/ha (1 + 2 kg/ha logged)."
    )


def test_rates_in_different_units_or_unknown_products_are_never_merged(session, settings, seeded):
    seeded("B")
    _record_p5(
        session,
        settings,
        [
            ("1", "kg/ha", "copper-contact", None),
            ("0.5", "L/ha", "copper-contact", None),
            ("2", "kg/ha", None, "mystery mix"),
        ],
    )
    ctx = ctx_at(session, settings, "2024-04-10 06:00")

    result = tools.last_spray(ctx, parcel="P5")

    assert result.facts["products"] == [
        {**COPPER, "dose": 1.0, "unit": "kg/ha"},
        {**COPPER, "dose": 0.5, "unit": "L/ha"},
        {"name": "mystery mix", "active_substance": None, "dose": 2.0, "unit": "kg/ha"},
    ]
    assert "dose" not in result.facts
    assert result.text.splitlines()[0].endswith(
        ": copper 1 kg/ha + copper 0.5 L/ha + mystery mix 2 kg/ha."
    )
