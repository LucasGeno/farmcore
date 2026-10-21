"""NGSI-LD export: done sprays as FIWARE `AgriParcelOperation` entities (key-values).

The embedded check stands in for the published schema (no network, no `jsonschema`):
smart-data-models dataModel.Agrifood `AgriParcelOperation/schema.json` at commit
1d5694e021b7070fd08b647093186e7ad8828202 lists `required: [id, type, hasAgriParcel,
plannedStartAt, plannedEndAt]`, `operationType` enum (fertiliser, inspection, pesticide, water,
other) and `status` enum (planned, ongoing, finished, scheduled, cancelled).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from farmcore.apps.operations import register
from farmcore.apps.share import config, ngsi, service
from farmcore.core import logs
from farmcore.core.access import OWNER
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import AccessLog
from tests.apps.share.synth import NOW, T_SPRAY, VOICE

REQUIRED = (
    "id",
    "type",
    "@context",
    "hasAgriParcel",
    "operationType",
    "startedAt",
    "dateCreated",
    # the schema's own `required` array
    "plannedStartAt",
    "plannedEndAt",
)
OPERATION_TYPE_ENUM = {"fertiliser", "inspection", "pesticide", "water", "other"}
STATUS_ENUM = {"planned", "ongoing", "finished", "scheduled", "cancelled"}
TIMESTAMPS = (
    "startedAt",
    "endedAt",
    "plannedStartAt",
    "plannedEndAt",
    "dateCreated",
    "dateModified",
)
ENTITY_PREFIX = "urn:ngsi-ld:AgriParcelOperation:farmcore-"


def check_entity(e: dict) -> None:
    missing = [k for k in REQUIRED if k not in e]
    assert not missing, missing
    assert e["type"] == "AgriParcelOperation"
    assert e["id"].startswith(ENTITY_PREFIX)
    assert e["hasAgriParcel"].startswith("urn:ngsi-ld:AgriParcel:farmcore-")
    assert e["@context"] == [config.NGSI_CONTEXT]
    assert e["operationType"] in OPERATION_TYPE_ENUM
    assert e.get("status", "finished") in STATUS_ENUM
    for key in TIMESTAMPS:
        if key in e:
            assert e[key].endswith("Z"), (key, e[key])
            datetime.fromisoformat(e[key].replace("Z", "+00:00"))
    if "hasAgriProductType" in e:
        assert e["hasAgriProductType"].startswith("urn:ngsi-ld:AgriProductType:farmcore-")
    if "quantity" in e:
        assert isinstance(e["quantity"], int | float) and not isinstance(e["quantity"], bool)
    if "unitCode" in e:
        assert e["unitCode"] in set(config.UNIT_CODES.values())


def test_one_entity_per_done_log_parcel_and_product_quantity(owner_repo, demo, spray) -> None:
    log = spray(
        doses=(("copper-contact", "2", "kg/ha"), ("sulphur-wettable", "3", "kg")),
        ts_end=T_SPRAY + timedelta(hours=2),
    )
    p1, p2 = demo.parcels
    copper = demo.products["copper-contact"]

    entities = ngsi.operations(owner_repo, NOW)

    assert len(entities) == 4
    for e in entities:
        check_entity(e)
    first = next(
        e
        for e in entities
        if e["hasAgriParcel"].endswith(f"-{p1.id}")
        and e.get("hasAgriProductType", "").endswith(f"-{copper.id}")
    )
    q_copper = next(q for q in log.quantities if q.product_id == copper.id)
    assert first["id"] == f"{ENTITY_PREFIX}{log.id}-{p1.id}-{q_copper.id}"
    assert first["operationType"] == "pesticide"
    assert first["status"] == "finished"
    assert first["startedAt"] == "2024-04-10T06:00:00Z"
    assert first["endedAt"] == "2024-04-10T08:00:00Z"
    assert first["quantity"] == 2
    assert "unitCode" not in first  # Rec 20 has no kg/ha code
    assert "kg/ha" in first["description"]
    assert copper.name in first["description"] and copper.authorisation_no in first["description"]
    sulphur = [e for e in entities if e.get("unitCode")]
    assert len(sulphur) == 2 and {e["unitCode"] for e in sulphur} == {"KGM"}
    assert {e["hasAgriParcel"] for e in entities} == {
        f"urn:ngsi-ld:AgriParcel:farmcore-{p1.id}",
        f"urn:ngsi-ld:AgriParcel:farmcore-{p2.id}",
    }


def test_a_log_without_product_quantity_gives_one_entity_per_parcel(
    session, owner_repo, demo
) -> None:
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in demo.parcels],
        ts_start=T_SPRAY,
        ts_end=None,
        quantities=[],
        source=VOICE,
        actor=OWNER,
        occurred_at=T_SPRAY,
    )
    session.flush()

    entities = ngsi.operations(owner_repo, NOW)

    assert len(entities) == 2
    for e in entities:
        check_entity(e)
        assert "quantity" not in e and "unitCode" not in e and "hasAgriProductType" not in e
        assert e["id"].endswith("-0")
        assert e["plannedEndAt"] == e["plannedStartAt"] == "2024-04-10T06:00:00Z"


def test_route_exports_done_sprays_up_to_the_clock_as_ld_json(
    admin, session, settings, demo, spray
) -> None:
    done = spray()
    spray(status="pending", source=replace(VOICE, external_key="tg:1:2"))
    later = spray(ts=NOW + timedelta(days=3), source=replace(VOICE, external_key="tg:1:3"))
    last = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0

    resp = admin.get("/share/ngsi-ld.json")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/ld+json")
    assert isinstance(resp.json(), list)
    body = [e for e in resp.json() if e["type"] == "AgriParcelOperation"]  # then its AgriParcels
    assert len(body) == 2  # one done spray × 2 parcels × 1 product
    for e in body:
        check_entity(e)
    ids = {e["id"].removeprefix(ENTITY_PREFIX).split("-")[0] for e in body}
    assert ids == {str(done.id)}
    assert str(later.id) not in ids
    session.expire_all()
    scopes = set(session.scalars(select(AccessLog.scope).where(AccessLog.id > last)))
    assert scopes == {"register:read"}

    set_replay(session, T_SPRAY - timedelta(hours=1), settings)
    session.commit()
    assert admin.get("/share/ngsi-ld.json").json() == []


def test_grantee_export_reads_under_the_grant(
    grantee_client, session, owner_repo, demo, spray, grantee_identity
) -> None:
    spray()
    grant = service.create_grant(
        owner_repo,
        recipient_identity_id=grantee_identity.id,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    last = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0

    resp = grantee_client.get(f"/share/as/{grant.id}/ngsi-ld.json")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/ld+json")
    assert sum(e["type"] == "AgriParcelOperation" for e in resp.json()) == 2
    session.expire_all()
    rows = session.scalars(select(AccessLog).where(AccessLog.id > last)).all()
    assert rows and {(r.actor, r.grant_id, r.scope) for r in rows} == {
        (f"user:{grantee_client.identity_id}", grant.id, "register:read")
    }


def test_quantity_is_a_number_not_a_string(owner_repo, demo, spray) -> None:
    spray(doses=(("copper-contact", "1.5", "kg/ha"),))
    [e, _] = ngsi.operations(owner_repo, NOW)
    assert e["quantity"] == float(Decimal("1.5"))


def test_products_entries_follow_the_register_rule(session, owner_repo, demo) -> None:
    """A file-imported log (labels + `props.products`, no product rows): one entity per register
    row, the label enriched with the entry's active substance, a null-dose entry kept."""
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in demo.parcels],
        ts_start=T_SPRAY,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", label="synth-a")],
        source=VOICE,
        actor=OWNER,
        occurred_at=T_SPRAY,
    )
    log.props = {
        **(log.props or {}),
        "products": {
            "synth-a": {"name": "Synthetic A", "active_substance": "substance A", "dose": "2"},
            "synth-b": {"name": "Synthetic B", "active_substance": "substance B", "dose": None},
        },
    }
    session.flush()

    entities = ngsi.operations(owner_repo, NOW)

    assert len(entities) == len(register.register_rows(owner_repo, NOW)) == 4
    assert len({e["id"] for e in entities}) == 4
    for e in entities:
        check_entity(e)
    dosed = [e for e in entities if "quantity" in e]
    assert len(dosed) == 2
    assert all("Synthetic A (substance A)" in e["description"] for e in dosed)
    undosed = [e for e in entities if "quantity" not in e]
    assert len(undosed) == 2
    assert all(e["description"] == "Synthetic B (substance B)" for e in undosed)


def test_a_labelled_quantity_without_products_entry_keeps_its_label(
    session, owner_repo, demo
) -> None:
    """An ISOXML dose whose product did not match (label, no product row, no `props.products`)
    still exports its label; the register shows that log as one blank-product row."""
    logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[demo.parcels[0].id],
        ts_start=T_SPRAY,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal("1"), "L/ha", label="Unmatched product")],
        source=VOICE,
        actor=OWNER,
        occurred_at=T_SPRAY,
    )
    session.flush()

    [e] = ngsi.operations(owner_repo, NOW)

    check_entity(e)
    assert e["quantity"] == 1
    assert e["description"].startswith("Unmatched product")
