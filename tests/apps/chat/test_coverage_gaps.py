"""Coverage for chat draft fact fields and validation branches."""

from __future__ import annotations

from datetime import datetime

import pytest

from farmcore.apps.chat import service
from farmcore.apps.chat.extractor.base import DraftOperation, fact_fields
from farmcore.apps.chat.push import push_advice
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.manifest import JobContext
from tests.apps.planning.conftest import NOW


def _context(session, farm):
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")
    return service.load_context(repo, NOW, "Europe/Paris")


def test_fact_fields_of_an_unknown_type_are_empty():
    assert fact_fields(DraftOperation(type="other")) == {}


def test_check_refuses_an_unknown_operation_type(session, farm):
    ctx = _context(session, farm)
    with pytest.raises(service.Unclear, match="spray, an irrigation or a field observation"):
        service.check(DraftOperation(type="other"), ctx)


def test_a_naive_draft_time_is_read_in_the_farm_timezone(session, farm, parcels, products):
    ctx = _context(session, farm)
    draft = DraftOperation(
        type="spray",
        parcels=[parcels[0].alias],
        products=[products["copper-contact"].name],
        dose_value=2.0,
        dose_unit="kg/ha",
        ts_start=datetime(2024, 4, 9, 3),
    )
    checked = service.check(draft, ctx)
    assert checked.ts_start.tzinfo is not None
    assert checked.ts_start.astimezone(ctx.tz).hour == 3


def test_an_irrigation_with_an_unknown_basis_is_refused(session, farm, parcels):
    ctx = _context(session, farm)
    draft = DraftOperation(
        type="irrigate",
        parcels=[parcels[0].alias],
        amount_value=20,
        amount_unit="mm",
        basis="wet",
        ts_start=NOW,
    )
    with pytest.raises(service.Unclear, match="gross or net"):
        service.check(draft, ctx)


def test_advice_push_skips_while_external_effects_are_paused(session, farm, settings):
    from pydantic import SecretStr
    from sqlalchemy import text

    name = session.execute(text("SELECT current_database()")).scalar()
    assert name
    session.execute(text(f"ALTER DATABASE \"{name}\" SET farmcore.external_effects = 'paused'"))
    configured = settings.model_copy(update={"telegram_bot_token": SecretStr("synthetic-token")})
    try:
        push_advice(JobContext(session=session, now=NOW, settings=configured, farm_ids=(farm.id,)))
    finally:
        session.execute(text(f'ALTER DATABASE "{name}" RESET farmcore.external_effects'))
