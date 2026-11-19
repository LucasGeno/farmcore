"""Crops and app bundles: pre-fill from published registry codes, never guess, append seasons,
and switch a bundle's apps and dependencies on atomically.

The code-to-crop expectations come from the published RPG 2024 culture list, the SIGPAC use
list (RD 1047/2022) and the EPPO Global Database, not from the implementation.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from farmcore.apps.fields import seasons
from farmcore.apps.onboarding import crops
from farmcore.core import registry
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import CropSeason
from farmcore.core.models.kernel import AppModule
from farmcore.core.seed import ensure_default_farm
from tests.apps.share.conftest import make_parcel


@pytest.fixture
def farm(session, settings):
    farm = ensure_default_farm(session, settings)
    farm.props = {**(farm.props or {}), "country": "FR"}
    session.flush()
    return farm


def _parcel(session, farm, n: int, props: dict[str, str]):
    parcel = make_parcel(session, farm, n)
    parcel.props = props
    session.flush()
    return parcel


@pytest.mark.parametrize(
    ("country", "code", "key", "eppo"),
    [
        ("FR", "VRC", "vine", "VITVI"),  # vigne
        ("FR", "BTH", "soft-wheat", "TRZAX"),  # blé tendre d'hiver
        ("FR", "MIS", "grain-maize", "ZEAMX"),  # maïs
        ("FR", "ORH", "barley", "HORVX"),  # orge d'hiver
        ("FR", "CZH", "rapeseed", "BRSNN"),  # colza d'hiver
        ("FR", "TRN", "sunflower", "HELAN"),  # tournesol
        ("FR", "PPH", "grassland", None),  # prairie permanente: no single species
        ("FR", "OLI", "olive", "OLVEU"),  # olive
        ("FR", "PTC", "potato", "SOLTU"),  # pomme de terre
        ("ES", "VI", "vine", "VITVI"),  # viñedo
        ("ES", "OV", "olive", "OLVEU"),  # olivar
        ("ES", "FY", "fruit-trees", None),  # frutales: species unknown
        ("FR", "VRG", None, None),  # verger: a generic orchard, not a crop
        ("ES", "VO", None, None),  # viñedo-olivar: association, not one crop
        ("ES", "TA", None, None),  # tierras arables: the crop is unknown
        ("FR", "ZZZ", None, None),
    ],
)
def test_published_registry_codes_map_to_one_farmcore_crop(
    country: str, code: str, key: str | None, eppo: str | None
) -> None:
    found = crops.crosswalk(country, code)

    if key is None:
        assert found is None
    else:
        assert (found.key, found.eppo) == (key, eppo)


def test_crops_page_prefills_known_codes_and_lists_unknown_ones(client, session, farm) -> None:
    known = _parcel(session, farm, 1, {"code_cultu": "VRC"})
    strange = _parcel(session, farm, 2, {"code_cultu": "ZZZ"})
    session.commit()

    page = client.get("/onboarding/crops")

    assert page.status_code == 200
    assert f'data-parcel="{known.id}" data-crop="vine" data-unknown-code=""' in page.text
    assert f'data-parcel="{strange.id}" data-crop="" data-unknown-code="ZZZ"' in page.text

    saved = client.post(
        "/onboarding/crops/assign",
        data={
            "starts_on": "2024-03-01",
            "parcel": [str(known.id)],
            f"crop_{known.id}": "vine",
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    session.expire_all()
    rows = {row.parcel_id: row for row in session.scalars(select(CropSeason)).all()}
    assert (rows[known.id].crop_eppo, rows[known.id].crop_name) == ("VITVI", "Vine")
    assert strange.id not in rows


def test_bulk_assign_keeps_a_covering_season_and_adds_the_rest(client, session, farm) -> None:
    covered = _parcel(session, farm, 1, {"code_cultu": "VRC"})
    fresh = _parcel(session, farm, 2, {"code_cultu": "BTH"})
    repo = ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), "test", "crop seasons"
    )
    seasons.create(repo, covered, {"starts_on": date(2024, 1, 1), "crop_eppo": "VITVI"})
    session.commit()

    saved = client.post(
        "/onboarding/crops/assign",
        data={
            "starts_on": "2024-03-01",
            "parcel": [str(covered.id), str(fresh.id)],
            f"crop_{covered.id}": "soft-wheat",
            f"crop_{fresh.id}": "soft-wheat",
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    session.expire_all()
    kept = session.scalars(select(CropSeason).where(CropSeason.parcel_id == covered.id)).all()
    added = session.scalars(select(CropSeason).where(CropSeason.parcel_id == fresh.id)).all()
    assert len(kept) == 1
    assert (kept[0].crop_eppo, kept[0].revision) == ("VITVI", 1)
    assert [(row.crop_eppo, row.crop_name) for row in added] == [("TRZAX", "Soft wheat")]


def test_choosing_a_bundle_switches_on_its_apps_and_dependencies(client, session) -> None:
    names = ("stations", "advisor", "forecast", "planning", "inventory", "share")
    for name in names:
        session.get(AppModule, name).state = registry.UNINSTALLED
    session.commit()

    chosen = client.post("/onboarding/bundles/vineyard", follow_redirects=False)

    assert chosen.status_code == 303
    session.expire_all()
    states = registry.read_states(session, list(client.app.state.all_manifests))
    for name in (*names, "fields", "operations"):
        assert states[name] == registry.INSTALLED


def test_a_bundle_failure_enables_nothing(session) -> None:
    ordered = registry.discover(packages=("tests.core.fake_apps",))
    for name in ("a", "b"):
        session.add(AppModule(name=name, version="1.0.0", state=registry.UNINSTALLED))
    session.flush()
    session.get(AppModule, "a").settings = {"threshold_c": "hot"}

    targets, refusal = registry.switch_bundle(
        session, ordered, registry.read_states(session, ordered), ["a"]
    )

    assert [manifest.name for manifest in targets] == ["c", "b", "a"]
    assert refusal is not None
    states = registry.read_states(session, ordered)
    assert (states["a"], states["b"]) == (registry.UNINSTALLED, registry.UNINSTALLED)


def test_animals_are_coming_later_and_capture_nothing(client, farm) -> None:
    for path in ("/onboarding/crops", "/onboarding/bundles"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'data-animals="coming-later"' in page.text

    assert client.post("/onboarding/animals", data={"name": "Bessie"}).status_code in {404, 405}
