"""Portable parcel ids: the NGSI-LD export carries one `AgriParcel` per exported parcel, its
genuine IACS/LPIS id and portable `ext_id` aliases in the Smart Data Models `relatedSource` property
(AgriFood-Commons: "List of IDs the current entity may have in external applications"); the
register JSON carries the aliases as `source:key`. A Varda Global FieldID would be one more
alias, source `varda`: nothing is called or generated."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.operations import register
from farmcore.apps.share import config, ngsi, service
from farmcore.core.models.canonical import ExtId, Parcel
from tests.apps.share.conftest import TEST_SOURCE

PARCEL_PREFIX = "urn:ngsi-ld:AgriParcel:farmcore-"


def _parcels(body: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in body if e["type"] == "AgriParcel"}


def _alias(demo, n: int) -> str:
    return f"FR-SHARE-{demo.farm.id}-{n}"


def test_the_export_carries_each_parcel_with_its_iacs_id_and_aliases(admin, demo, spray):
    spray()

    body = admin.get("/share/ngsi-ld.json").json()

    parcels = _parcels(body)
    referenced = {e["hasAgriParcel"] for e in body if e["type"] == "AgriParcelOperation"}
    assert set(parcels) == referenced  # every operation's parcel is in the export, once
    p1 = parcels[f"{PARCEL_PREFIX}{demo.parcels[0].id}"]
    assert p1["name"] == "P1"
    assert p1["area"] == 12500  # 1.25 ha in m², the schema's unit
    assert p1["relatedSource"] == [
        {"application": "urn:ngsi-ld:AgriApp:iacs-lpis", "applicationEntityId": "FR-SHARE-1"},
        {
            "application": f"urn:ngsi-ld:AgriApp:{TEST_SOURCE}",
            "applicationEntityId": _alias(demo, 1),
        },
    ]
    assert p1["@context"] == [config.NGSI_CONTEXT]
    assert body.index(p1) > max(
        i for i, e in enumerate(body) if e["type"] == "AgriParcelOperation"
    )  # operations first, as before


def test_a_varda_alias_is_one_more_related_source(admin, session, demo, spray):
    spray(parcels=demo.parcels[:1])
    session.add(
        ExtId(source="varda", key="key-under-test", model="parcel", res_id=demo.parcels[0].id)
    )
    session.commit()

    body = admin.get("/share/ngsi-ld.json").json()
    rows = admin.get("/operations/register.json").json()

    related = _parcels(body)[f"{PARCEL_PREFIX}{demo.parcels[0].id}"]["relatedSource"]
    assert {
        "application": "urn:ngsi-ld:AgriApp:varda",
        "applicationEntityId": "key-under-test",
    } in related
    assert "varda:key-under-test" in rows[0]["parcel_aliases"]


def test_a_synthetic_parcel_id_is_not_exported_as_iacs(admin, session, demo, spray):
    demo.parcels[0].props = {**demo.parcels[0].props, "id_label": "synthetic id"}
    session.commit()
    spray(parcels=demo.parcels[:1])

    related = _parcels(admin.get("/share/ngsi-ld.json").json())[
        f"{PARCEL_PREFIX}{demo.parcels[0].id}"
    ]["relatedSource"]

    assert [r["application"] for r in related] == [f"urn:ngsi-ld:AgriApp:{TEST_SOURCE}"]


def test_register_json_rows_carry_the_aliases_and_the_csv_is_unchanged(admin, demo, spray):
    spray()

    rows = admin.get("/operations/register.json").json()
    header = next(csv.reader(io.StringIO(admin.get("/operations/register.csv").text)))

    by_parcel = {r["parcel_alias"]: r for r in rows}
    assert by_parcel["P1"]["iacs_parcel_id"] == "FR-SHARE-1"
    assert by_parcel["P1"]["parcel_aliases"] == [f"{TEST_SOURCE}:{_alias(demo, 1)}"]
    assert by_parcel["P2"]["parcel_aliases"] == [f"{TEST_SOURCE}:{_alias(demo, 2)}"]
    assert "parcel_aliases" not in header
    assert header[: len(register.CSV_FIELDS)] == list(register.CSV_FIELDS)


def test_a_grantee_reads_the_same_ids(anonymous, session, owner_repo, demo, spray):
    spray()
    grant = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()

    body = anonymous.get(f"/share/as/{grant.id}/ngsi-ld.json").json()
    rows = anonymous.get(f"/share/as/{grant.id}/register.json").json()

    assert {p["name"] for p in _parcels(body).values()} == {"P1", "P2"}
    assert "Share parcel" not in str(body)
    assert rows[0]["parcel_aliases"] == [f"{TEST_SOURCE}:{_alias(demo, 1)}"]


def test_no_parcel_shape_is_exported_to_a_grantee_or_the_owner(
    admin, anonymous, session, owner_repo, demo, spray
):
    """Portable ids only: an `AgriParcel` carries no `location`, so a register:read grant gets
    no parcel geometry it did not get before."""
    spray()
    grant = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()

    for body in (
        anonymous.get(f"/share/as/{grant.id}/ngsi-ld.json").json(),
        admin.get("/share/ngsi-ld.json").json(),
    ):
        parcels = _parcels(body).values()
        assert parcels and all("location" not in p for p in parcels)
        assert all(
            set(p) == {"id", "type", "name", "area", "relatedSource", "@context"} for p in parcels
        )
        assert "MultiPolygon" not in str(body) and "coordinates" not in str(body)


@pytest.mark.parametrize("collision", ["alias", "iacs"])
@pytest.mark.parametrize("rpg_source", ["ign-rpg-2023", "ign-rpg-2024"])
@pytest.mark.parametrize("rpg_matches_iacs", [True, False])
def test_related_sources_identify_rpg_stand_ins_without_losing_other_namespaces(
    admin, session, demo, spray, collision, rpg_source, rpg_matches_iacs
):
    parcel = demo.parcels[0]
    varda_key = _alias(demo, 1) if collision == "alias" else parcel.iacs_id
    rpg_key = parcel.iacs_id if rpg_matches_iacs else "different-open-polygon-id"
    session.add_all(
        [
            ExtId(source=rpg_source, key=rpg_key, model="parcel", res_id=parcel.id),
            ExtId(source="varda", key=varda_key, model="parcel", res_id=parcel.id),
        ]
    )
    session.commit()
    spray(parcels=[parcel])

    related = _parcels(admin.get("/share/ngsi-ld.json").json())[f"{PARCEL_PREFIX}{parcel.id}"][
        "relatedSource"
    ]

    expected_iacs = (
        []
        if rpg_matches_iacs
        else [
            {"application": "urn:ngsi-ld:AgriApp:iacs-lpis", "applicationEntityId": parcel.iacs_id}
        ]
    )
    assert related == expected_iacs + [
        {"application": f"urn:ngsi-ld:AgriApp:{rpg_source}", "applicationEntityId": rpg_key},
        {
            "application": f"urn:ngsi-ld:AgriApp:{TEST_SOURCE}",
            "applicationEntityId": _alias(demo, 1),
        },
        {"application": "urn:ngsi-ld:AgriApp:varda", "applicationEntityId": varda_key},
    ]


def test_synthetic_dg_agri_aliases_are_omitted_from_owner_and_grantee_json(
    admin, anonymous, session, owner_repo, demo, spray
):
    parcel = demo.parcels[0]
    parcel.props = {**parcel.props, "id_label": "synthetic id"}
    session.add(
        ExtId(source="dg-agri", key="parcel:synthetic-test", model="parcel", res_id=parcel.id)
    )
    spray(parcels=[parcel])
    grant = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()

    for client, ngsi_url, register_url in (
        (admin, "/share/ngsi-ld.json", "/operations/register.json"),
        (
            anonymous,
            f"/share/as/{grant.id}/ngsi-ld.json",
            f"/share/as/{grant.id}/register.json",
        ),
    ):
        body = client.get(ngsi_url).json()
        rows = client.get(register_url).json()
        related = _parcels(body)[f"{PARCEL_PREFIX}{parcel.id}"]["relatedSource"]
        assert related == [
            {
                "application": f"urn:ngsi-ld:AgriApp:{TEST_SOURCE}",
                "applicationEntityId": _alias(demo, 1),
            }
        ]
        assert rows[0]["parcel_aliases"] == [f"{TEST_SOURCE}:{_alias(demo, 1)}"]


def test_foreign_farm_parcel_ids_never_reach_owner_or_grantee_exports(
    admin, anonymous, session, owner_repo, demo, other_farm, spray
):
    foreign = session.scalars(select(Parcel).where(Parcel.farm_id == other_farm.id)).one()
    foreign_key = "FOREIGN-KEY"
    session.add(ExtId(source="varda", key=foreign_key, model="parcel", res_id=foreign.id))
    spray()
    grant = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()

    for client, url in (
        (admin, "/share/ngsi-ld.json"),
        (admin, "/operations/register.json"),
        (anonymous, f"/share/as/{grant.id}/ngsi-ld.json"),
        (anonymous, f"/share/as/{grant.id}/register.json"),
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert foreign_key not in response.text
        assert f"FR-SHARE-{other_farm.id}-1" not in response.text
        assert f"{PARCEL_PREFIX}{foreign.id}" not in response.text
        assert response.json()
    assert ngsi.parcels(owner_repo, [foreign.id]) == []
