"""Activity filters select complete farm actions before pagination, never slices of a spray."""

from datetime import timedelta

from tests.apps.activity.test_columns import _rows
from tests.core.test_activity import NOW, _pid, _spray, farm  # noqa: F401


def test_parcel_filter_preserves_a_whole_multi_parcel_action(client, session, farm):  # noqa: F811
    _spray(session, farm, ["P1", "P2"], NOW - timedelta(hours=1))
    _spray(session, farm, ["P3"], NOW - timedelta(hours=2))
    session.commit()
    response = client.get("/activity/", params={"parcel_id": _pid(session, farm, "P1")})
    assert response.status_code == 200
    rows = _rows(response.text)
    assert len(rows) == 1
    assert "Spray logged on P1, P2" in rows[0]


def test_type_filter_selects_existing_actions_without_creating_new_record_types(
    client,
    session,
    farm,  # noqa: F811
):
    from tests.apps.activity.test_paging import _grant
    from tests.core.test_activity import _advice

    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    _advice(session, farm, "P2", "advice.changed", NOW - timedelta(hours=2))
    _grant(session, farm, 1, NOW - timedelta(hours=3))
    session.commit()
    response = client.get("/activity/", params={"type": "advice"})
    rows = _rows(response.text)
    assert len(rows) == 1
    assert 'data-kind="advice.changed"' in rows[0]


def test_period_uses_local_midnights_and_includes_the_whole_through_day(
    client,
    session,
    farm,  # noqa: F811
):
    from datetime import UTC, datetime

    _spray(session, farm, ["P1"], datetime(2024, 4, 8, 22, tzinfo=UTC))
    _spray(session, farm, ["P2"], datetime(2024, 4, 8, 21, 59, tzinfo=UTC))
    _spray(session, farm, ["P3"], datetime(2024, 4, 9, 22, tzinfo=UTC))
    session.commit()
    response = client.get("/activity/", params={"from": "2024-04-09", "to": "2024-04-09"})
    rows = _rows(response.text)
    assert len(rows) == 1
    assert "Spray logged on P1" in rows[0]


def test_filters_survive_poll_paging_and_column_preferences(client, session, farm):  # noqa: F811
    import html
    import re
    from urllib.parse import parse_qs, urlsplit

    from tests.core.test_activity import _advice

    for minute in range(1, 56):
        _advice(session, farm, "P1", "advice.changed", NOW - timedelta(minutes=minute))
    _advice(session, farm, "P2", "advice.changed", NOW)
    session.commit()
    pid = _pid(session, farm, "P1")
    params = {
        "type": "advice",
        "parcel_id": str(pid),
        "from": "2024-04-10",
        "to": "2024-04-10",
        "farm": str(farm.id),
    }
    page = client.get("/activity/", params=params).text
    poll = html.unescape(re.search(r'hx-get="(/activity/[^\"]*)"', page)[1])
    assert parse_qs(urlsplit(poll).query) == {k: [v] for k, v in params.items()}
    assert len(_rows(page)) == 50
    assert f'href="/operations/register?farm={farm.id}"' in page
    assert "Export register" in page
    assert f'href="/advisor/parcels/{pid}?farm={farm.id}"' in _rows(page)[0]
    next_url = html.unescape(re.search(r'name="next" value="([^\"]+)"', page)[1])
    assert parse_qs(urlsplit(next_url).query) == {k: [v] for k, v in params.items()}
    older = html.unescape(re.search(r'href="([^\"]*before=[^\"]*)"', page)[1])
    previous = client.get(older).text
    assert len(_rows(previous)) == 5
    assert "data-poll-feed" not in previous
    newest = html.unescape(re.search(r'href="([^\"]*)"[^>]*>Newest', previous)[1])
    assert parse_qs(urlsplit(newest).query) == {k: [v] for k, v in params.items()}
    response = client.get("/activity/columns", params={"show": "who", "next": next_url})
    assert parse_qs(urlsplit(str(response.url)).query) == {k: [v] for k, v in params.items()}
    assert 'data-col="who"' in response.text


def test_recent_activity_opens_activity_for_the_selected_farm(client, session, farm):  # noqa: F811
    page = client.get("/activity/today", params={"farm": farm.id}).text
    assert f'href="/activity/?farm={farm.id}"' in page


def test_unfiltered_native_form_accepts_empty_parcel_and_dates(client, farm):  # noqa: F811
    response = client.get(
        "/activity/", params={"parcel_id": "", "type": "all", "from": "", "to": ""}
    )
    assert response.status_code == 200
    assert "Nothing yet" in response.text


def test_filter_scans_past_unmatched_batches_without_splitting_actions(
    client,
    session,
    farm,  # noqa: F811
):
    import html
    import re

    from tests.core.test_activity import _advice

    for second in range(205):
        _advice(session, farm, "P2", "advice.changed", NOW - timedelta(seconds=second))
    for minute in range(10, 61):
        _advice(session, farm, "P1", "advice.changed", NOW - timedelta(minutes=minute))
    session.commit()
    page = client.get("/activity/", params={"parcel_id": _pid(session, farm, "P1")}).text
    assert len(_rows(page)) == 50
    older = html.unescape(re.search(r'href="([^\"]*before=[^\"]*)"', page)[1])
    assert len(_rows(client.get(older).text)) == 1


def test_period_does_not_resurrect_a_draft_resolved_on_a_later_day(
    client,
    session,
    farm,  # noqa: F811
):
    from farmcore.core import logs

    pending = _spray(session, farm, ["P1"], NOW - timedelta(days=1), pending=True)
    logs.confirm(session, pending.id, "chat:42", NOW)
    session.commit()
    response = client.get("/activity/", params={"from": "2024-04-09", "to": "2024-04-09"})
    assert _rows(response.text) == []
    assert "No matching activity" in response.text


def test_activity_link_falls_back_to_diary_when_the_activity_app_is_off(
    client,
    session,
    farm,  # noqa: F811
):
    from farmcore.core.models.kernel import AppModule

    session.get(AppModule, "activity").state = "uninstalled"
    session.commit()
    page = client.get("/activity/today").text
    assert 'href="/operations/"' in page


def test_zero_is_not_a_parcel_filter(client, farm):  # noqa: F811
    assert client.get("/activity/", params={"parcel_id": "0"}).status_code == 422


def test_filter_cannot_read_or_offer_another_farms_parcels(client, session, farm):  # noqa: F811
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm, Parcel
    from tests.core.test_activity import SQUARE

    other = Farm(name="Other test farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    parcel = Parcel(farm_id=other.id, alias="ES1", name="Other test parcel", area_ha=1, geom=SQUARE)
    session.add(parcel)
    session.commit()
    foreign = client.get("/activity/", params={"parcel_id": parcel.id})
    assert foreign.status_code == 200
    assert "That parcel is not on this farm" in foreign.text
    assert "Other test parcel" not in foreign.text
    assert _rows(foreign.text) == []
    selected = client.get("/activity/", params={"farm": other.id, "parcel_id": parcel.id})
    assert selected.status_code == 200
    assert "ES1 · Other test parcel" in selected.text
    assert "P1 · Parcel 1" not in selected.text
    assert f'href="/operations/register?farm={other.id}"' in selected.text


def test_bad_filters_are_validation_errors_not_server_errors(client, farm):  # noqa: F811
    for query in (
        {"type": "harvest"},
        {"parcel_id": "-1"},
        {"parcel_id": str(2**63)},
        {"from": "invalid"},
        {"from": "2024-04-10", "to": "2024-04-09"},
    ):
        assert client.get("/activity/", params=query).status_code == 422
    assert client.get("/activity/", params={"to": "9999-12-31"}).status_code == 200


def test_browser_bad_date_filter_is_a_422_beside_the_field_keeping_the_typed_value(
    client,
    farm,  # noqa: F811
):
    page = client.get("/activity/", params={"from": "10/04/2024"}, headers={"Accept": "text/html"})

    assert page.status_code == 422
    assert 'name="from" value="10/04/2024"' in page.text
    assert 'aria-describedby="activity-from-error"' in page.text
    assert "literal" not in page.text


def test_farm_switch_keeps_activity_usable_when_the_parcel_filter_belongs_to_the_old_farm(
    client,
    session,
    farm,  # noqa: F811
):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm

    other = Farm(name="Other test farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    previous = f"/activity/?farm={farm.id}&parcel_id={_pid(session, farm, 'P1')}"
    response = client.get("/farms/select", params={"farm": other.id, "next": previous})
    assert response.status_code == 200
    assert "That parcel is not on this farm" in response.text
    assert _rows(response.text) == []


def test_staged_spray_source_is_explicitly_demo_data(client, session, farm):  # noqa: F811
    from tests.apps.activity.test_columns import _cells

    _spray(session, farm, ["P1"], NOW, source="seed", actor="seed")
    session.commit()
    (row,) = _rows(client.get("/activity/").text)
    assert _cells(row)["source"] == "Demo data"
