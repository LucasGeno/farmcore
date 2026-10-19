"""Recent activity on Today (`GET /activity/today`, core): the farm's latest 5 things that
happened, newest first, up to the clock; one spray over many parcels is one row; only
`advice.changed` from the advisor; kinds of apps that are not loaded are hidden."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from farmcore.core import logs
from farmcore.core import seed as core_seed
from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.timeline import post_event
from farmcore.main import create_app

NOW = datetime(2024, 4, 10, 5, 40, tzinfo=UTC)  # Wed 10 Apr 2024 07:40 Europe/Paris
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"


@pytest.fixture
def farm(session, settings) -> Farm:
    farm = core_seed.ensure_default_farm(session, settings)
    for n in range(1, 11):
        session.add(
            Parcel(
                farm_id=farm.id,
                name=f"Parcel {n}",
                alias=f"P{n}",
                area_ha=Decimal("1"),
                geom=SQUARE,
            )
        )
    session.flush()
    set_replay(session, NOW)
    session.commit()
    return farm


def _pid(session, farm: Farm, alias: str) -> int:
    return next(p.id for p in session.query(Parcel).filter_by(farm_id=farm.id, alias=alias))


def _spray(
    session,
    farm: Farm,
    aliases: list[str],
    at: datetime,
    *,
    source: str = "voice",
    actor: str = "chat:42",
    key: str | None = None,
    pending: bool = False,
) -> logs.Log:
    kwargs = dict(
        farm_id=farm.id,
        type="spray",
        parcel_ids=[_pid(session, farm, a) for a in aliases],
        ts_start=at,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", None, "copper")],
        source=logs.SourceInput(source, key or f"k:{aliases}:{at}", {}, 0.9, logs.RANK_VOICE),
        actor=actor,
        occurred_at=at,
        source_app="chat",
    )
    if pending:
        return logs.create_pending(session, **kwargs)
    log, _ = logs.record_operation(session, **kwargs)
    return log


def _advice(session, farm: Farm, alias: str, kind: str, at: datetime, new: str = "spray") -> None:
    pid = _pid(session, farm, alias)
    post_event(
        session,
        farm_id=farm.id,
        res_model="parcel",
        res_id=pid,
        kind=kind,
        body=f"{alias}: none → {new}. Spray copper today.",
        payload={
            "parcel_id": pid,
            "alias": alias,
            "old": "none",
            "new": new,
            "decision": new,
            "card_path": f"/advisor/parcels/{pid}",
        },
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=at,
    )


def _rows(client) -> list[str]:
    resp = client.get("/activity/today")
    assert resp.status_code == 200
    body = html.unescape(resp.text)
    return re.findall(r"<li [^>]*data-activity=.*?</li>", body, re.S)


def _text(fragment: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", fragment).split())


def test_empty_feed_says_what_comes_here(client, farm) -> None:
    body = _text(html.unescape(client.get("/activity/today").text))
    assert "Recent activity" in body
    assert "Nothing yet" in body


def test_one_spray_over_three_parcels_is_one_row(client, session, farm) -> None:
    _spray(session, farm, ["P2", "P10", "P1"], NOW - timedelta(hours=1))
    session.commit()
    (row,) = _rows(client)
    text = _text(row)
    assert "Spray logged on P1, P2, P10" in text
    assert "copper 2 kg/ha" in text
    assert "Voice note" in text
    assert "Wed 10 Apr · 06:40" in text or "Today · 06:40" in text
    assert 'href="/operations/register"' in row


def test_a_batch_of_per_parcel_logs_is_one_row(client, session, farm) -> None:
    at = NOW - timedelta(days=1)
    for alias in ("P1", "P2", "P3", "P4"):
        _spray(session, farm, [alias], at, source="seed", actor="seed")
    session.commit()
    (row,) = _rows(client)
    assert "Spray logged on 4 parcels" in _text(row)
    assert "Yesterday · 07:40" in _text(row)


def test_newest_first_and_five_rows_at_most(client, session, farm) -> None:
    for h, alias in enumerate(["P1", "P2", "P3", "P4", "P5", "P6", "P7"], start=1):
        _spray(session, farm, [alias], NOW - timedelta(hours=h))  # P1 newest
    session.commit()
    rows = _rows(client)
    assert len(rows) == 5
    assert [re.search(r"on (P\d+)", _text(r)).group(1) for r in rows] == [
        "P1",
        "P2",
        "P3",
        "P4",
        "P5",
    ]


def test_events_after_the_replay_clock_are_hidden(client, session, farm) -> None:
    _spray(session, farm, ["P1"], NOW - timedelta(hours=2))
    _spray(session, farm, ["P2"], NOW + timedelta(minutes=1))
    session.commit()
    (row,) = _rows(client)
    assert "on P1" in _text(row)


def test_pending_then_confirmed_shows_the_confirmation_only(client, session, farm) -> None:
    log = _spray(session, farm, ["P5"], NOW - timedelta(minutes=10), pending=True)
    logs.confirm(session, log.id, "chat:42", NOW - timedelta(minutes=5), source_app="chat")
    session.commit()
    (row,) = _rows(client)
    assert "Spray confirmed on P5" in _text(row)


def test_a_waiting_draft_shows_and_links_to_the_diary(client, session, farm) -> None:
    _spray(session, farm, ["P5"], NOW - timedelta(minutes=10), pending=True)
    session.commit()
    (row,) = _rows(client)
    assert "waiting for confirmation" in _text(row)
    assert 'href="/operations/"' in row


def test_machine_merge_reads_as_an_update_from_the_machine_file(client, session, farm) -> None:
    _spray(session, farm, ["P1"], NOW - timedelta(hours=3))
    _spray(
        session,
        farm,
        ["P1"],
        NOW - timedelta(hours=1),
        source="isoxml",
        actor="owner",
        key="task-1",
    )
    session.commit()
    rows = _rows(client)
    assert "Spray on P1 updated from the machine file" in _text(rows[0])
    assert "Machine file" in _text(rows[0])


def test_only_advice_changes_show_grouped_per_new_decision(client, session, farm) -> None:
    at = NOW - timedelta(hours=1)
    for alias in ("P1", "P2", "P3"):
        _advice(session, farm, alias, "advice.evaluated", at)
    _advice(session, farm, "P1", "advice.changed", at)
    _advice(session, farm, "P2", "advice.changed", at)
    _advice(session, farm, "P3", "advice.changed", at, new="wait")
    session.commit()
    rows = _rows(client)
    assert len(rows) == 2
    texts = [_text(r) for r in rows]
    spray = next(r for r, t in zip(rows, texts, strict=True) if "P1, P2" in t)
    wait = next(r for r, t in zip(rows, texts, strict=True) if "P3" in t)
    assert "Advice for P1, P2 changed to" in _text(spray) and 'data-decision="spray"' in spray
    assert 'href="/advisor/"' in spray
    assert 'data-decision="wait"' in wait
    assert f'href="/advisor/parcels/{_pid(session, farm, "P3")}"' in wait


def test_share_rows_use_the_event_body_and_link_to_sharing(client, session, farm) -> None:
    post_event(
        session,
        farm_id=farm.id,
        res_model="data_grant",
        res_id=99,
        kind="share.granted",
        body="Granted Coop read access: spray records, until 01 Jun 2024",
        payload={"grantee": "Coop"},
        actor="owner",
        source_app="share",
        occurred_at=NOW - timedelta(hours=1),
    )
    session.commit()
    (row,) = _rows(client)
    assert "Granted Coop read access" in _text(row)
    assert 'href="/share/"' in row


def test_other_farms_events_never_show(client, session, farm) -> None:
    other = Farm(name="Other")
    session.add(other)
    session.flush()
    parcel = Parcel(farm_id=other.id, name="X", alias="P1", area_ha=Decimal("1"), geom=SQUARE)
    session.add(parcel)
    session.flush()
    post_event(
        session,
        farm_id=other.id,
        res_model="parcel",
        res_id=parcel.id,
        kind="log.created",
        body="spray logged",
        payload={"log_id": 1, "type": "spray", "status": "done", "source_types": ["voice"]},
        actor="x",
        source_app="core",
        occurred_at=NOW - timedelta(hours=1),
    )
    session.commit()
    assert _rows(client) == []


def test_kinds_of_apps_that_are_not_loaded_are_hidden(
    settings, session_factory, session, farm
) -> None:
    _advice(session, farm, "P1", "advice.changed", NOW - timedelta(hours=2))
    _spray(session, farm, ["P2"], NOW - timedelta(hours=1))  # posted by chat
    log, _ = logs.record_operation(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[_pid(session, farm, "P3")],
        ts_start=NOW - timedelta(hours=3),
        ts_end=None,
        quantities=[],
        source=logs.SourceInput("file", "f1", {}, None, logs.RANK_FILE),
        actor="owner",
        occurred_at=NOW - timedelta(hours=3),
    )  # source_app core
    session.commit()
    with TestClient(create_app(settings, manifests=[CORE], session_factory=session_factory)) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        rows = _rows(c)
    # The advice row goes with its app; both spray records stay (Records is always on), even the
    # one the not-loaded chat app took down.
    assert len(rows) == 2
    assert "on P2" in _text(rows[0]) and "on P3" in _text(rows[1])
    assert all("/operations/" not in r for r in rows)  # no link into an app that is not loaded
    assert not any("advice" in _text(r).lower() for r in rows)


def test_doses_keep_their_decimals() -> None:
    from farmcore.core.activity import _dose

    values = [Decimal("2.0"), Decimal("1.05"), Decimal("0.050"), Decimal("12")]
    assert [_dose(v) for v in values] == ["2", "1.05", "0.05", "12"]
