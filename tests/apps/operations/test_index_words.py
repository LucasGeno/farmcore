"""The operations page in plain words for a visitor of the public site."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.apps.operations import seed as ops_seed
from farmcore.core import clock, logs
from farmcore.main import create_app

T_VOICE = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
CLOCK = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
TOKEN = "tok-ops"


@pytest.fixture
def pages(settings, session_factory, session, demo):
    """(visitor html, admin html) of /operations/ after one voice spray, on a site with a token."""
    products = ops_seed.ensure_products(session)
    logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in demo.parcels],
        ts_start=T_VOICE,
        ts_end=None,
        quantities=[
            logs.QuantityInput(
                "rate", Decimal("2"), "kg/ha", product_id=products["copper-contact"].id
            )
        ],
        source=logs.SourceInput("voice", "tg:1", {"transcript": "synthetic"}, 0.9, 30),
        actor="owner",
        occurred_at=T_VOICE,
    )
    clock.set_replay(session, CLOCK, settings)
    session.commit()
    app = create_app(
        settings.model_copy(
            update={
                "admin_token": SecretStr(TOKEN),
                "demo_mode": True,
                "demo_farm_ids": [demo.farm.id],
            }
        ),
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        return c.get("/operations/").text, c.get(
            "/operations/", headers={"X-Admin-Token": TOKEN}
        ).text


def test_recent_record_is_named_in_words_not_by_its_internal_name(pages):
    visitor, _ = pages

    assert "spray 2024-04-10" not in visitor
    record = visitor[visitor.index('data-record="') :]
    assert ">Spray<" in record
    assert "10 Apr 2024 08:00" in record


def test_bbch_note_is_plain_words_without_config_text(pages):
    visitor, _ = pages

    assert "assumption: grapevine BBCH phase dates" not in visitor
    assert "pending P3" not in visitor  # no internal ticket refs
    assert "assumed: awaiting agronomist" in visitor
    assert "Growth stage" in visitor and "Bordeaux" in visitor


def test_machine_upload_panel_is_collapsed_for_visitors_and_open_for_admins(pages, demo):
    visitor, admin = pages

    assert "<details data-isoxml-panel" in visitor
    import re
    from urllib.parse import parse_qs, urlsplit

    assert not re.search(r'<form[^>]*action="/operations/isoxml', visitor)
    action = re.search(r'<form[^>]*action="([^"]+/isoxml[^"]*)"', admin)
    assert action is not None
    target = urlsplit(action.group(1))
    assert target.path == "/operations/isoxml"
    # A form freezes the farm whose records were rendered, even without a URL selector.
    assert parse_qs(target.query) == {"farm": [str(demo.farm.id)]}
    assert "<details data-isoxml-panel" not in admin


def _spray(session, settings, demo, ts_start: datetime, name: str | None = None) -> None:
    """One voice spray on the first parcel, the clock at CLOCK, committed."""
    logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[demo.parcels[0].id],
        ts_start=ts_start,
        ts_end=None,
        quantities=[],
        source=logs.SourceInput("voice", "tg:1", {"transcript": "synthetic"}, 0.9, 30),
        actor="owner",
        occurred_at=ts_start,
        name=name,
    )
    clock.set_replay(session, CLOCK, settings)
    session.commit()


def _record(html: str) -> str:
    return html[html.index('data-record="') :]


def test_default_name_given_in_paris_time_near_midnight_is_still_named_in_words(
    client, session, settings, demo
):
    # 00:30 Paris on 10 Apr is 22:30 UTC on 9 Apr: core.logs names it "spray 2024-04-10" from the
    # Paris date, while the page loads ts_start in the session's time zone.
    _spray(session, settings, demo, datetime(2024, 4, 10, 0, 30, tzinfo=ZoneInfo("Europe/Paris")))

    html = client.get("/operations/").text

    assert "spray 2024-04-10" not in html.lower()
    assert ">Spray<" in _record(html)


def test_a_custom_record_name_is_kept(client, session, settings, demo):
    _spray(session, settings, demo, T_VOICE, name="Copper pass north rows")

    assert ">Copper pass north rows<" in _record(client.get("/operations/").text)


def test_bbch_badge_follows_the_bbch_source(client, demo, monkeypatch):
    from farmcore.apps.operations import bbch

    assert bbch.SOURCE.startswith("assumption")
    assert "assumed: awaiting agronomist" in client.get("/operations/").text

    monkeypatch.setattr(bbch, "SOURCE", "ISVV Bordeaux phenology bulletin 2024")
    html = client.get("/operations/").text

    assert "assumed: awaiting agronomist" not in html
    assert "ISVV Bordeaux phenology bulletin 2024" in html
