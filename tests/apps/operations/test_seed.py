"""Operations seed: the product shortlist and the Scene B 9 May copper spray, idempotently."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from farmcore.apps.chat.extractor import ExtractRequest, RegexExtractor
from farmcore.apps.chat.extractor.base import ProductRef
from farmcore.apps.operations import checks
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import ext_ids
from farmcore.core.models.canonical import Event, Log, LogSource, Product


def _count(session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where))


def test_seed_without_scene_creates_the_three_shortlist_products(session, demo, tmp_path):
    ops_seed.seed(session, scene=None, data_dir=tmp_path)

    products = {p.active_substance: p for p in session.scalars(select(Product))}
    assert set(products) == {"copper", "sulphur", "cymoxanil"}
    assert products["copper"].product_type == "contact"
    assert products["copper"].targets == ["downy"]
    assert products["sulphur"].targets == ["powdery"]
    assert products["cymoxanil"].product_type == "penetrant"
    assert products["cymoxanil"].targets == ["downy"]
    assert _count(session, Log) == 0


# E-Phy product pages (ephy.anses.fr), read 16 Oct 2026.
EPHY = {
    "copper": (
        "BOUILLIE BORDELAISE RSR DISPERSS NC",
        "9800474",
        "https://ephy.anses.fr/ppp/bouillie-bordelaise-rsr-disperss-nc",
    ),
    "sulphur": (
        "MICROTHIOL SPECIAL DISPERSS",
        "9800245",
        "https://ephy.anses.fr/ppp/microthiol-special-disperss",
    ),
    "cymoxanil": ("PAJO", "2140127", "https://ephy.anses.fr/ppp/pajo"),
}


def test_shortlist_products_carry_their_verified_e_phy_name_and_amm(session, demo, tmp_path):
    ops_seed.seed(session, scene=None, data_dir=tmp_path)

    products = {p.active_substance: p for p in session.scalars(select(Product))}
    assert {
        s: (p.name, p.authorisation_no, p.props.get("source")) for s, p in products.items()
    } == EPHY
    assert all(p.props.get("placeholder") is False for p in products.values())


# Vine usage on each E-Phy page: the register checks read these.
LABELS = {
    "copper": {
        "label_max_dose_kg_ha": 3.75,
        "label_max_applications": 5,
        "cu_fraction": 0.2,
    },
    "sulphur": {"label_max_dose_kg_ha": 12.5, "label_max_applications": 8, "phi_days": 3},
    "cymoxanil": {
        "label_max_dose_kg_ha": 0.45,
        "label_max_applications": 2,
        "phi_days": 28,
        "min_interval_days": 7,
    },
}
LABEL_KEYS = (
    "label_max_dose_kg_ha",
    "label_max_applications",
    "phi_days",
    "min_interval_days",
    "cu_fraction",
)


def test_shortlist_products_carry_their_vine_label_limits(session, demo, tmp_path):
    ops_seed.seed(session, scene=None, data_dir=tmp_path)

    products = {p.active_substance: p for p in session.scalars(select(Product))}
    got = {s: {k: p.props[k] for k in LABEL_KEYS if k in p.props} for s, p in products.items()}
    assert got == LABELS
    assert all(p.props["label_usage"] == "Vigne, Trt Part.Aer." for p in products.values())
    assert checks.Label.from_props(products["copper"].props).cu_fraction == Decimal("0.2")


@pytest.mark.parametrize(
    "old_name, old_amm",
    [
        # The shortlist as seeded before E-Phy numbers: same ext_id keys, placeholder values.
        ("Old {s}", "PLACEHOLDER-{S}"),
        # An earlier E-Phy pick, seeded before the label limits existed.
        ("THIOVIT JET MICROBILLES {s}", "2000018"),
    ],
)
def test_reseeding_an_older_shortlist_updates_the_products_in_place(
    session, demo, tmp_path, old_name, old_amm
):
    old = {}
    for key, substance in [
        ("copper-contact", "copper"),
        ("sulphur-wettable", "sulphur"),
        ("cymoxanil-penetrant", "cymoxanil"),
    ]:
        old[substance], _ = ext_ids.upsert(
            session,
            source=ops_seed.PRODUCT_SOURCE,
            key=key,
            model=Product,
            values={
                "name": old_name.format(s=substance),
                "authorisation_no": old_amm.format(S=substance.upper()),
                "active_substance": substance,
                "product_type": "contact",
                "targets": [],
                "props": {"placeholder": True},
            },
        )
    ids = {s: p.id for s, p in old.items()}

    ops_seed.seed(session, scene=None, data_dir=tmp_path)

    assert _count(session, Product) == 3
    products = {p.active_substance: p for p in session.scalars(select(Product))}
    assert {s: p.id for s, p in products.items()} == ids
    assert {s: p.authorisation_no for s, p in products.items()} == {
        s: amm for s, (_, amm, _) in EPHY.items()
    }
    assert all(p.props.get("placeholder") is False for p in products.values())
    # Products seeded before the register checks gain the label limits on the next seed.
    assert products["cymoxanil"].props["phi_days"] == 28


def test_regex_extractor_maps_the_french_demo_sentence_to_the_seeded_copper(
    session, demo, tmp_path
):
    ops_seed.seed(session, scene=None, data_dir=tmp_path)
    shortlist = [
        ProductRef(
            name=p.name,
            active_substance=p.active_substance,
            product_type=p.product_type,
            targets=p.targets,
        )
        for p in session.scalars(select(Product).order_by(Product.id))
    ]
    now = datetime(2024, 4, 10, 10, 0, tzinfo=ZoneInfo("Europe/Paris"))

    def draft(text: str):
        return RegexExtractor().extract(
            ExtractRequest(
                transcript=text,
                parcels=[{"alias": "P5", "name": "Les Granges"}, {"alias": "P6", "name": "Peyrot"}],
                products=shortlist,
                today=now.date(),
                now_local=now,
            )
        )

    # demo.md (FR/EN) and the "bouillie bordelaise" wording: all name the seeded products.
    for text in (
        "Traité Les Granges et Peyrot au cuivre et soufre, 2 kilos, ce matin",
        "Traité Les Granges et Peyrot à la bouillie bordelaise et soufre, 2 kilos, ce matin",
        "Sprayed Les Granges and Peyrot with copper and sulphur, 2 kilos, this morning",
        "Sprayed Les Granges and Peyrot with Bordelaise and sulfur, 2 kilos, this morning",
    ):
        assert draft(text).products == [
            "BOUILLIE BORDELAISE RSR DISPERSS NC",
            "MICROTHIOL SPECIAL DISPERSS",
        ], text
    assert draft("Sprayed Les Granges with Bouillie bordelaise, 2 kilos").products == [
        "BOUILLIE BORDELAISE RSR DISPERSS NC"
    ]


def test_scene_b_twice_sprays_copper_once_per_parcel_on_9_may(session, demo, tmp_path):
    first = ops_seed.seed(session, scene="B", data_dir=tmp_path)
    second = ops_seed.seed(session, scene="B", data_dir=tmp_path)

    assert first == {"products": 3, "scene_b_sprays": 2}
    assert second == {"products": 3, "scene_b_sprays": 2}
    assert _count(session, Product) == 3
    sprays = list(session.scalars(select(Log).order_by(Log.id)))
    assert len(sprays) == 2
    # 2024-05-09 08:00 Europe/Paris (CEST, UTC+2) is 06:00 UTC.
    nine_may = datetime(2024, 5, 9, 6, 0, tzinfo=UTC)
    copper = session.scalar(select(Product).where(Product.active_substance == "copper"))
    for log, parcel in zip(sprays, demo.parcels, strict=True):
        assert (log.type, log.status, log.ts_start) == ("spray", "done", nine_may)
        assert [p.id for p in log.parcels] == [parcel.id]
        assert [(q.product_id, q.value, q.unit) for q in log.quantities] == [
            (copper.id, Decimal("2"), "kg/ha")
        ]
        assert [s.source_type for s in log.sources] == ["seed"]
    assert _count(session, LogSource) == 2
    # One log.created event per spray, at the spray's clock time, tagged with its source.
    events = list(session.scalars(select(Event).order_by(Event.id)))
    assert [(e.kind, e.occurred_at, e.payload["source_types"]) for e in events] == [
        ("log.created", nine_may, ["seed"]),
        ("log.created", nine_may, ["seed"]),
    ]


def test_scene_is_case_insensitive_and_unknown_scene_sprays_nothing(session, demo, tmp_path):
    ops_seed.seed(session, scene="x", data_dir=tmp_path)
    assert _count(session, Log) == 0
    ops_seed.seed(session, scene="b", data_dir=tmp_path)
    assert _count(session, Log) == 2
