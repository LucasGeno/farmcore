"""Crops and app bundles: pre-fill from published registry codes, never guess, append seasons,
and switch a bundle's apps and dependencies on atomically.

The code-to-crop expectations come from the published RPG 2024 culture list, the SIGPAC use
list (RD 1047/2022) and the EPPO Global Database, not from the implementation.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.fields import seasons
from farmcore.apps.onboarding import crops
from farmcore.core import registry
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import CropSeason
from farmcore.core.models.kernel import AppModule
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.share.conftest import make_parcel
from tests.core.test_member_sessions import _account, _login


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


def _uninstall(session, *names: str) -> None:
    for name in names:
        row = session.get(AppModule, name)
        if row is None:  # no app has started yet: the row's absence means installed
            session.add(AppModule(name=name, version="0.1.0", state=registry.UNINSTALLED))
        else:
            row.state = registry.UNINSTALLED


def _row(html: str, parcel_id: int) -> str:
    """The parcel's table row, so its attributes are asserted independently of their order."""
    match = re.search(rf'<tr data-parcel="{parcel_id}".*?</tr>', html, re.S)
    assert match is not None, html
    return match.group(0)


@pytest.mark.parametrize(
    ("country", "code", "key", "eppo"),
    [
        ("FR", "VRC", "vine", "VITVI"),  # vigne: a species
        ("FR", "PPH", "grassland", None),  # prairie permanente: a group, no single species
        ("ES", "VI", "vine", "VITVI"),  # viñedo: a species, same rule as FR
        ("ES", "FY", "fruit-trees", None),  # frutales: a group
        ("FR", "VRG", "fruit-trees", None),  # verger: the same generic group as ES FY
        ("ES", "VO", None, None),  # viñedo-olivar: an association, not one crop
        ("ES", "1", "soft-wheat", "TRZAX"),  # TRIGO BLANDO, a declared-product code
        ("ES", "102", "vine", "VITVI"),  # VIÑA
        ("ES", "326", None, None),  # MAÍZ DULCE: listed, but not the maize crop
        ("ES", "9999", None, None),  # absent from the published product list
        ("FR", "ZZZ", None, None),  # absent from the published list
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
    assert 'data-animals="coming-later"' in page.text
    known_row = _row(page.text, known.id)
    assert 'data-crop="vine"' in known_row
    assert 'data-unknown-code=""' in known_row
    strange_row = _row(page.text, strange.id)
    assert 'data-crop=""' in strange_row
    assert 'data-unknown-code="ZZZ"' in strange_row

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
    fresh = _parcel(session, farm, 2, {"code_cultu": "MIS"})
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
            f"crop_{covered.id}": "maize",
            f"crop_{fresh.id}": "maize",
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    session.expire_all()
    kept = session.scalars(select(CropSeason).where(CropSeason.parcel_id == covered.id)).all()
    added = session.scalars(select(CropSeason).where(CropSeason.parcel_id == fresh.id)).all()
    assert len(kept) == 1
    assert (kept[0].crop_eppo, kept[0].revision) == ("VITVI", 1)
    assert [(row.crop_eppo, row.crop_name) for row in added] == [("ZEAMX", "Maize")]


def test_choosing_a_bundle_switches_on_its_apps_and_dependencies(client, session) -> None:
    names = ("stations", "advisor", "forecast", "planning", "inventory", "share")
    for name in names:
        session.get(AppModule, name).state = registry.UNINSTALLED
    session.commit()

    chosen = client.post("/onboarding/bundles/vineyard", follow_redirects=False)

    assert chosen.status_code == 303
    page = client.get("/onboarding/bundles")
    assert 'data-animals="coming-later"' in page.text
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


def test_crops_need_the_farm_owner_and_bundles_need_the_installation_admin(
    settings, session_factory, session, farm
) -> None:
    parcel = _parcel(session, farm, 1, {"code_cultu": "VRC"})
    _uninstall(session, "stations")
    session.commit()
    _account(session_factory, login="crops-advisor", farm_id=farm.id, role="advisor")
    _account(session_factory, login="farm-owner", farm_id=farm.id, role="owner")

    def headers(browser: TestClient) -> dict[str, str]:
        return {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as member:
        assert _login(member, login="crops-advisor").status_code == 303
        refused = member.post(
            "/onboarding/crops/assign",
            data={
                "starts_on": "2024-03-01",
                "parcel": str(parcel.id),
                f"crop_{parcel.id}": "vine",
            },
            headers=headers(member),
        )
        assert refused.status_code == 403

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as owner:
        assert _login(owner, login="farm-owner").status_code == 303
        refused = owner.post("/onboarding/bundles/vineyard", headers=headers(owner))
        assert refused.status_code == 403

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(CropSeason)) == 0
    assert session.get(AppModule, "stations").state == registry.UNINSTALLED


def test_a_bundle_notice_names_apps_that_load_at_next_start(
    settings, session_factory, session
) -> None:
    _uninstall(session, "stations")
    session.commit()
    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    with TestClient(
        create_app(settings, session_factory=session_factory),
        base_url="https://testserver",
        headers=headers,
    ) as browser:
        chosen = browser.post("/onboarding/bundles/vineyard", follow_redirects=False)
        assert chosen.status_code == 303
        page = browser.get("/onboarding/bundles?enabled=vineyard")
        assert page.status_code == 200
        assert "Weather" in page.text
        assert "next start" in page.text
