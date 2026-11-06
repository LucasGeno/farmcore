"""Planning test fixtures: default farm, parcels, product shortlist and one stored assessment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.operations.seed import ensure_products
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice
from farmcore.core.seed import ensure_default_farm
from tests.apps.share.conftest import make_parcel

NOW = datetime(2024, 4, 9, 4, tzinfo=UTC)  # 06:00 Europe/Paris


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def farm(session, settings):
    farm = ensure_default_farm(session, settings)
    set_replay(session, NOW, settings)
    session.flush()
    return farm


@pytest.fixture
def parcels(session, farm):
    return [make_parcel(session, farm, 1, area="1.25"), make_parcel(session, farm, 2)]


@pytest.fixture
def products(session):
    return ensure_products(session)


@pytest.fixture
def advice(session, farm, parcels):
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcels[0].id,
        evaluated_at=NOW,
        decision="spray",
        next_window_start=NOW,
        next_window_end=NOW + timedelta(hours=4),
        reasons=[],
        products=[],
        props={
            "assessment_status": "evaluable",
            "config_version": {"id": None, "status": "unreviewed", "accuracy_claim": False},
        },
    )
    session.add(row)
    session.flush()
    return row


def job_payload(parcels, products, advice, **overrides):
    payload = {
        "name": "Spray block A",
        "parcel_ids": [parcels[0].id],
        "assessment_id": advice.id,
        "product_id": products["copper-contact"].id,
        "scheduled_start": (NOW + timedelta(hours=2)).isoformat(),
        "duration_minutes": 90,
        "notes": "Morning pass",
    }
    return payload | overrides
