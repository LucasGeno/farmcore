"""Advisor debug panel: `GET /advisor/debug/parcels/{id}`, admin only.

Data tests seed the demo farm (open IGN parcels and Météo-France station CSV) and step the replay
clock to 10 Apr 06:00, where every parcel is `spray` with a `downy_3_10` reason.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from farmcore.apps.advisor import debug as advisor_debug
from farmcore.core import logs
from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import Advice, DataGrant, Event, Log, Parcel
from farmcore.main import create_app
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

TOKEN = "tok-panel"
CHAT = 4242  # synthetic Telegram chat id


@pytest.fixture
def guarded(settings, session_factory):
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr(TOKEN)}),
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        yield c


def _p1(session) -> Parcel:
    return session.scalar(select(Parcel).where(Parcel.alias == "P1"))


def _counts(session) -> tuple[int, int, int]:
    return tuple(session.scalar(select(func.count()).select_from(m)) for m in (Log, Advice, Event))


def test_panel_does_not_import_the_card_routes() -> None:
    # The card lives in `advisor/routes.py`; the panel reads advice through `advisor/service.py`.
    tree = ast.parse(inspect.getsource(advisor_debug))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "farmcore.apps.advisor.routes" not in imported


def test_anonymous_gets_403(guarded) -> None:
    resp = guarded.get("/advisor/debug/parcels/1")
    assert resp.status_code == 403


@pytest.mark.data
def test_admin_sees_reason_codes_inputs_thresholds_and_sources(
    guarded, session, seeded, step
) -> None:
    seeded()
    step("2024-04-10 06:00")
    p1 = _p1(session)
    row = session.scalar(
        select(Advice).where(Advice.parcel_id == p1.id).order_by(Advice.id.desc()).limit(1)
    )
    downy = next(r for r in row.reasons if r["rule"] == "downy_3_10")

    guarded.cookies.set("admin_token", TOKEN)
    resp = guarded.get(f"/advisor/debug/parcels/{p1.id}")

    assert resp.status_code == 200
    body = resp.text
    assert 'data-debug-panel="advisor"' in body
    assert f'data-advice-id="{row.id}"' in body
    for reason in row.reasons:
        assert f'data-rule-code="{reason["rule"]}"' in body
    for key in downy["inputs"]:
        assert f"<dt>{key}</dt>" in body
    for key in downy["threshold"]:
        assert f"{key}" in body
    assert downy["source"] in body
    assert "downy_3_10" in body and "&#34;decision&#34;: &#34;spray&#34;" in body  # raw JSON


@pytest.mark.data
def test_admin_panel_marks_assumed_thresholds(guarded, session, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    p1 = _p1(session)
    row = session.scalar(
        select(Advice).where(Advice.parcel_id == p1.id).order_by(Advice.id.desc()).limit(1)
    )
    assumed = [r for r in row.reasons if "(assumed:" in r.get("note", "")]
    assert assumed, "the fixed scene must exercise assumed-threshold rendering"
    guarded.cookies.set("admin_token", TOKEN)
    body = guarded.get(f"/advisor/debug/parcels/{p1.id}").text
    assert 'data-assumed="true"' in body


@pytest.mark.data
def test_debug_gets_change_no_log_advice_or_event_rows(guarded, session, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    p1 = _p1(session)
    before = _counts(session)

    guarded.cookies.set("admin_token", TOKEN)
    guarded.cookies.set("farmcore_debug", "1")
    for url in (
        f"/advisor/debug/parcels/{p1.id}",
        f"/debug/drawer?path=/advisor/parcels/{p1.id}&debug=1",
        "/debug/drawer?path=/fields/&debug=1",
        "/debug/drawer?path=/operations/register&debug=0",
    ):
        assert guarded.get(url).status_code == 200, url

    assert _counts(session) == before


def _chat_spray(session, settings, alias: str) -> None:
    """A confirmed voice spray from a synthetic Telegram chat (`tg:<chat>:<msg>` key)."""
    farm = core_seed.ensure_default_farm(session, settings)
    parcel = session.scalar(select(Parcel).where(Parcel.alias == alias))
    ts = scenes.local("2024-04-10 08:00")
    pending = logs.create_pending(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[parcel.id],
        ts_start=ts,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", None, "copper")],
        source=logs.SourceInput("voice", f"tg:{CHAT}:77", {}, 0.9, logs.RANK_VOICE),
        actor=f"chat:{CHAT}",
        occurred_at=ts,
    )
    logs.confirm(session, pending.id, f"chat:{CHAT}", ts)
    session.commit()


@pytest.mark.data
def test_anonymous_pages_leak_nothing_even_with_debug_1(
    guarded, session, settings, seeded, step
) -> None:
    seeded()
    step("2024-04-10 06:00")
    p1 = _p1(session)
    _chat_spray(session, settings, "P1")
    grant_ids = [str(g) for g in session.scalars(select(DataGrant.id))]
    guarded.cookies.set("farmcore_debug", "1")  # a stale cookie gives nothing either

    for path in ("/fields/", f"/advisor/parcels/{p1.id}", "/operations/register"):
        page = guarded.get(path, params={"debug": "1"})
        assert page.status_code == 200, path
        html = page.text
        assert 'id="debug-slot"' in html  # the self-loading slot is on every page
        assert 'id="debug-drawer"' not in html
        assert "debug-access-log" not in html and "data-grant-id" not in html
        assert f"tg:{CHAT}" not in html and f"chat:{CHAT}" not in html
        drawer = guarded.get("/debug/drawer", params={"path": path, "debug": "1"})
        assert drawer.text == "" and "set-cookie" not in drawer.headers
        for gid in grant_ids:
            assert f"grant {gid}" not in html and f"grant_id={gid}" not in html
    assert guarded.get(f"/advisor/debug/parcels/{p1.id}").status_code == 403


def test_unknown_parcel_is_404_for_admins(guarded, session, settings) -> None:
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    guarded.cookies.set("admin_token", TOKEN)
    assert guarded.get("/advisor/debug/parcels/999999").status_code == 404
