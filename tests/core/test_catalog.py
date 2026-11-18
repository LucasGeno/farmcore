"""Neutral catalog items and typed quantities beside the spray data (expand step).

Failure modes owned here:
- Golden: adding the catalog changes what the register export, stock, spray evidence or source
  deduplication mean. Frozen in `snapshots/catalog_golden.json` before any schema change.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from farmcore.apps.advisor import service as advisor
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.inventory.service import stock_rows
from farmcore.apps.operations.register import register_rows, to_csv
from farmcore.core import logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Asset, Log, LogSource
from tests.apps.inventory.conftest import inv, spray, stock  # noqa: F401

GOLDEN = Path(__file__).parent / "snapshots" / "catalog_golden.json"
NOW = datetime(2024, 6, 1, 4, tzinfo=UTC)
TZ = "Europe/Paris"


def _repo(session, farm):
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "golden")


def _semantics(session, inv) -> dict:  # noqa: F811
    """What the consumers mean, without generated ids or wall-clock times."""
    repo = _repo(session, inv.farm)
    names = {p.id: p.name for p in inv.products.values()}
    station = Asset(farm_id=inv.farm.id, kind="station", name="Golden station")
    session.add(station)
    session.flush()
    config = AdvisorConfig.for_region_year("FRI12", NOW.year)
    evidence = advisor.input_snapshot(repo, station, inv.parcels[0].id, [], config, NOW, TZ, 0)
    return {
        "register_csv": [
            {k: v for k, v in row.items() if k != "log_id"}  # generated id, not meaning
            for row in csv.DictReader(
                io.StringIO(to_csv(register_rows(repo, NOW, tz=TZ, checks=True), tz=TZ))
            )
        ],
        "stock": [
            {
                "product": row.product.name,
                "purchased": str(row.purchased),
                "used": None if row.used is None else str(row.used),
                "on_hand": None if row.on_hand is None else str(row.on_hand),
                "sprays": row.sprays,
                "not_computable": row.not_computable,
                "low": row.low,
            }
            for row in stock_rows(repo, now=NOW, tz=TZ)
        ],
        "spray_evidence": {
            "logs": [
                [log["name"], log["ts_start"], log["status"], log["bbch"]]
                for log in evidence["spray_logs"]
            ],
            "quantities": [
                [
                    q["quantity"]["measure"],
                    q["quantity"]["value"],
                    q["quantity"]["unit"],
                    q["quantity"]["label"],
                    names.get(q["quantity"]["product_id"]),
                ]
                for q in evidence["quantities"]
            ],
            "ignored_sprays": evidence["ignored_sprays"],
        },
        "source_dedup": [
            [
                log.ts_start.isoformat(),
                sorted(
                    [s.source_type, s.external_key, s.precision_rank]
                    for s in session.scalars(select(LogSource).where(LogSource.log_id == log.id))
                ),
                sorted([q.measure, str(q.value), q.unit] for q in log.quantities),
            ]
            for log in session.scalars(
                select(Log).where(Log.farm_id == inv.farm.id).order_by(Log.ts_start, Log.id)
            )
        ],
    }


def test_golden_register_stock_evidence_and_dedup_keep_their_meaning(session, inv):  # noqa: F811
    copper, sulphur = inv.products["copper-contact"], inv.products["sulphur-wettable"]
    stock(session, inv.farm, copper, purchased="20", low_stock="5")
    stock(session, inv.farm, sulphur, purchased="10")
    april, may = datetime(2024, 4, 20, 6, tzinfo=UTC), datetime(2024, 5, 10, 6, tzinfo=UTC)
    spray(session, inv.farm, inv.parcels, copper, at=april, value="2.5", unit="kg/ha", key="f1")
    # The same spray from a machine file 2 h later merges into it: the more precise dose wins.
    logs.record_operation(
        session,
        farm_id=inv.farm.id,
        type="spray",
        parcel_ids=[inv.parcels[0].id],
        ts_start=april.replace(hour=8),
        ts_end=None,
        quantities=[
            logs.QuantityInput("rate", Decimal("2.4"), "kg/ha", copper.id, copper.name),
        ],
        source=logs.SourceInput("isoxml", "task-1", {}, None, logs.RANK_MACHINE),
        actor="test",
        occurred_at=april,
    )
    spray(session, inv.farm, inv.parcels[:1], sulphur, at=may, value="800", unit="g/ha", key="f2")
    # A litre dose against a kilogram stock is not computable, never half-summed.
    spray(session, inv.farm, inv.parcels[1:], copper, at=may, value="1", unit="L/ha", key="f3")
    session.flush()

    actual = _semantics(session, inv)

    assert actual == json.loads(GOLDEN.read_text())
