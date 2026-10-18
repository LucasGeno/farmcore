"""Parcel Activity public routes; invented records through the existing grouping seam."""

import html
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from tests.apps.activity.test_columns import _cells, _rows
from tests.core.test_activity import NOW, _pid, _spray, farm  # noqa: F401


def _links(body):
    return [html.unescape(value) for value in re.findall(r'href="([^"]+)"', body)]


def _provider(client, parcel_id, farm_id, **state):
    parent = f"/fields/parcels/{parcel_id}?" + urlencode(
        {"view": "activity", "farm": farm_id, **state}
    )
    return client.get(
        f"/activity/parcels/{parcel_id}/workspace",
        params={
            "farm": farm_id,
            "parent": parent,
            **state,
        },
    )


def test_workspace_reuses_whole_grouped_actions_with_fixed_parcel(client, session, farm):  # noqa: F811
    _spray(session, farm, ["P1", "P2"], NOW - timedelta(hours=1))
    _spray(session, farm, ["P2"], NOW - timedelta(days=2))
    _spray(session, farm, ["P1"], NOW + timedelta(days=1))
    session.commit()
    pid = _pid(session, farm, "P1")
    shell = client.get(f"/fields/parcels/{pid}", params={"view": "activity", "farm": farm.id})
    assert 'aria-current="page">Activity</a>' in shell.text
    response = _provider(client, pid, farm.id)
    assert response.status_code == 200
    assert "<h1" not in response.text and "<html" not in response.text
    expected = client.get("/activity/", params={"farm": farm.id, "parcel_id": pid})
    assert [_cells(row) for row in _rows(response.text)] == [
        _cells(row) for row in _rows(expected.text)
    ]
    assert len(_rows(response.text)) == 1
    assert "Spray logged on P1, P2" in response.text
    assert "includes this parcel" in response.text
    assert 'name="parcel_id"' not in response.text
    conflict = client.get(
        f"/activity/parcels/{pid}/workspace",
        params={
            "farm": farm.id,
            "parcel_id": _pid(session, farm, "P2"),
        },
    )
    assert len(_rows(conflict.text)) == 1


def test_workspace_ordinary_filters_paging_and_poll_keep_canonical_parent(client, session, farm):  # noqa: F811
    from tests.core.test_activity import _advice

    for minute in range(1, 56):
        _advice(session, farm, "P1", "advice.changed", NOW - timedelta(minutes=minute))
    session.commit()
    pid = _pid(session, farm, "P1")
    origin = f"/activity/?type=advice&farm={farm.id}"
    response = _provider(client, pid, farm.id, type="advice", return_to=origin)
    assert response.status_code == 200 and len(_rows(response.text)) == 50
    assert f'action="/fields/parcels/{pid}"' in response.text
    assert 'name="view" value="activity"' in response.text
    assert 'name="return_to"' in response.text
    older = next(u for u in _links(response.text) if "before=" in u)
    query = parse_qs(urlsplit(older).query)
    assert urlsplit(older).path == f"/fields/parcels/{pid}"
    assert query["farm"] == [str(farm.id)] and query["return_to"] == [origin]
    assert query["view"] == ["activity"] and query["type"] == ["advice"]
    shell = client.get(older)
    [fragment] = list(
        dict.fromkeys(
            html.unescape(u)
            for u in re.findall(r'hx-get="([^"]+)"', shell.text)
            if u.startswith(f"/activity/parcels/{pid}/workspace")
        )
    )
    previous = client.get(fragment)
    assert len(_rows(previous.text)) == 5 and "data-poll-feed" not in previous.text
    newest = html.unescape(re.search(r'<a[^>]*href="([^"]+)"[^>]*>Newest</a>', previous.text)[1])
    assert parse_qs(urlsplit(newest).query) == {
        "view": ["activity"],
        "farm": [str(farm.id)],
        "type": ["advice"],
        "return_to": [origin],
    }
    poll = html.unescape(re.search(r'id="activity-feed"[^>]*hx-get="([^"]+)"', response.text)[1])
    polled = client.get(poll, headers={"HX-Request": "true", "HX-Trigger": "activity-feed"})
    assert "HX-Push-Url" not in polled.headers and "<form" not in polled.text
    assert "HX-Push-Url" not in response.headers
    # User navigation is ordinary canonical GET; the new shell owns provider failure/Retry.
    controls = re.findall(r"<(?:a|form)\b[^>]*>", response.text)
    assert all("hx-get=" not in control for control in controls)
    assert "HX-Push-Url" not in previous.headers
    # Native GET controls reset the cursor, preserve source and are usable by copied URLs.
    filtered = client.get(
        f"/fields/parcels/{pid}",
        params={
            "farm": farm.id,
            "view": "activity",
            "return_to": origin,
            "type": "spray",
            "from": "2024-04-10",
            "to": "2024-04-10",
        },
    )
    assert "before=" not in next(
        html.unescape(u)
        for u in re.findall(r'hx-get="([^"]+)"', filtered.text)
        if u.startswith(f"/activity/parcels/{pid}/workspace")
    )


def test_group_parcel_links_are_explicit_and_return_to_same_filtered_feed(client, session, farm):  # noqa: F811
    _spray(session, farm, ["P1", "P2"], NOW - timedelta(hours=1))
    session.commit()
    page = client.get("/activity/", params={"type": "spray", "farm": farm.id})
    (row,) = _rows(page.text)
    parcel_links = [u for u in _links(row) if urlsplit(u).path.startswith("/fields/parcels/")]
    assert {urlsplit(u).path for u in parcel_links} == {
        f"/fields/parcels/{_pid(session, farm, 'P1')}",
        f"/fields/parcels/{_pid(session, farm, 'P2')}",
    }
    assert _cells(row)["parcel"] == "P1, P2"
    for link in parcel_links:
        state = parse_qs(urlsplit(link).query)
        assert state["view"] == ["activity"] and state["farm"] == [str(farm.id)]
        source = urlsplit(state["return_to"][0])
        assert source.path == "/activity/"
        assert parse_qs(source.query) == {"type": ["spray"], "farm": [str(farm.id)]}
        assert source.fragment.startswith("activity-row-")
    assert f"/operations/register?farm={farm.id}" in _links(row)


def test_declared_fallback_and_column_form_retain_workspace_context(client, session, farm):  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    session.commit()
    pid = _pid(session, farm, "P1")
    shell = client.get(
        f"/fields/parcels/{pid}",
        params={
            "farm": farm.id,
            "view": "activity",
            "type": "spray",
        },
    )
    fallback = next(
        u for u in _links(shell.text) if urlsplit(u).path == "/activity/" and "parcel_id=" in u
    )
    page = client.get(fallback)
    state = parse_qs(urlsplit(fallback).query)
    assert "Back to parcel" in page.text and state["return_to"][0] in _links(page.text)
    assert 'name="return_to"' in page.text
    provider = _provider(
        client, pid, farm.id, type="spray", return_to=f"/fields/?view=list&farm={farm.id}"
    )
    next_url = html.unescape(re.search(r'name="next" value="([^"]+)"', provider.text)[1])
    assert urlsplit(next_url).path == f"/fields/parcels/{pid}"
    q = parse_qs(urlsplit(next_url).query)
    assert q["type"] == ["spray"] and q["view"] == ["activity"]
    change = client.get(
        "/activity/columns",
        params={
            "farm": farm.id,
            "show": "who",
            "next": next_url,
        },
        follow_redirects=False,
    )
    assert change.status_code == 303 and change.headers["location"] == next_url
    fresh = _provider(client, pid, farm.id)
    assert 'data-col="who"' in fresh.text and 'data-col="product"' not in fresh.text
    assert f"/operations/register?farm={farm.id}" in _links(page.text)


def test_invalid_parent_keeps_direct_provider_canonical_same_parcel(client, session, farm):  # noqa: F811
    pid = _pid(session, farm, "P1")
    for parent in (
        "https://evil.example/",
        f"/fields/parcels/{pid}?view=activity&farm=999",
        f"/fields/parcels/{pid}?view=activity&return_to=/clock/replay",
        f"/fields/parcels/{pid}?view=activity&view=activity",
        "bad%GG",
    ):
        response = client.get(
            f"/activity/parcels/{pid}/workspace",
            params={
                "farm": farm.id,
                "parent": parent,
                "type": "spray",
            },
        )
        assert response.status_code == 200
        assert f'name="parcel_id" value="{pid}"' in response.text
        clear = next(u for u in _links(response.text) if urlsplit(u).path == "/activity/")
        assert parse_qs(urlsplit(clear).query) == {"parcel_id": [str(pid)], "farm": [str(farm.id)]}
        assert "HX-Push-Url" not in response.headers


class NativeForm(HTMLParser):
    """Successful native controls: GET replaces the query embedded in form action."""

    def __init__(self, action):
        super().__init__()
        self.path, self.action, self.inside = action, "", False
        self.controls, self.select = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.inside = urlsplit(attrs.get("action", "")).path == self.path
            if self.inside:
                self.action = attrs["action"]
        if not self.inside or "disabled" in attrs:
            return
        if (
            tag == "input"
            and "name" in attrs
            and (attrs.get("type") != "checkbox" or "checked" in attrs)
        ):
            self.controls.append((attrs["name"], attrs.get("value", "")))
        if tag == "select":
            self.select = [attrs["name"], None]
        if tag == "option" and self.select and (self.select[1] is None or "selected" in attrs):
            self.select[1] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select" and self.inside and self.select:
            self.controls.append(tuple(self.select))
            self.select = None
        if tag == "form":
            self.inside = False

    def url(self, **changes):
        controls = [(k, v) for k, v in self.controls if k not in changes]
        controls.extend(changes.items())
        return urlsplit(self.action)._replace(query=urlencode(controls)).geturl()


def test_native_workspace_filter_form_drops_cursor_and_retains_origin(client, session, farm):  # noqa: F811
    pid = _pid(session, farm, "P1")
    origin = f"/fields/?view=list&q=P1&farm={farm.id}"
    response = _provider(
        client, pid, farm.id, before=NOW.isoformat(), before_id="4", return_to=origin
    )
    form = NativeForm(f"/fields/parcels/{pid}")
    form.feed(response.text)
    assert form.action
    url = form.url(type="advice", **{"from": "2024-04-09", "to": "2024-04-10"})
    state = parse_qs(urlsplit(url).query)
    assert state == {
        "farm": [str(farm.id)],
        "view": ["activity"],
        "return_to": [origin],
        "type": ["advice"],
        "from": ["2024-04-09"],
        "to": ["2024-04-10"],
    }
    shell = client.get(url)
    fragment = next(
        html.unescape(u)
        for u in re.findall(r'hx-get="([^"]+)"', shell.text)
        if u.startswith(f"/activity/parcels/{pid}/workspace")
    )
    fresh = client.get(fragment)
    assert fresh.status_code == 200 and "data-poll-feed" in fresh.text


@pytest.mark.parametrize("tour", [False, True])
def test_native_columns_preserve_explicit_farm_and_workspace_cursor(client, session, farm, tour):  # noqa: F811
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm, Parcel
    from tests.core.test_activity import SQUARE

    other = Farm(name="Synthetic second owner farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    parcel = Parcel(farm_id=other.id, alias="P1", name="Second parcel", area_ha=1, geom=SQUARE)
    session.add(parcel)
    session.commit()
    client.cookies.set("farmcore_farm", str(farm.id))
    client.cookies.set(f"farmcore_activity_cols_{farm.id}", "time")
    if tour:
        client.cookies.set("farmcore_tour", "1")
    response = _provider(
        client, parcel.id, other.id, type="spray", before=NOW.isoformat(), before_id="4"
    )
    form = NativeForm("/activity/columns")
    form.feed(response.text)
    form.controls.append(("show", "who"))
    change = client.get(form.url(), follow_redirects=False)
    assert change.status_code == 303
    assert change.headers["set-cookie"].startswith(f"farmcore_activity_cols_{other.id}=")
    next_state = parse_qs(urlsplit(change.headers["location"]).query)
    assert next_state == {
        "view": ["activity"],
        "type": ["spray"],
        "farm": [str(other.id)],
        "before": [NOW.isoformat()],
        "before_id": ["4"],
    }
    assert client.cookies.get(f"farmcore_activity_cols_{farm.id}") == "time"
    assert "who" in client.cookies.get(f"farmcore_activity_cols_{other.id}")
    assert _provider(client, parcel.id, farm.id).status_code == 404
    for pid in (0, 999999, 2**31, 10**30):
        assert (
            client.get(f"/activity/parcels/{pid}/workspace", params={"farm": farm.id}).status_code
            == 404
        )


def test_empty_and_filter_no_matches_are_distinct_and_app_off_denies(client, session, farm):  # noqa: F811
    from farmcore.core.models.kernel import AppModule

    pid = _pid(session, farm, "P3")
    assert "Nothing yet" in _provider(client, pid, farm.id).text
    assert "No matching activity" in _provider(client, pid, farm.id, type="spray").text
    session.get(AppModule, "activity").state = "uninstalled"
    session.commit()
    assert _provider(client, pid, farm.id).status_code == 404
    body = client.get(f"/fields/parcels/{pid}", params={"farm": farm.id, "view": "activity"}).text
    assert "No longer available" in body and 'aria-current="page">Activity</a>' not in body


def test_local_date_resolution_and_replay_rules_match_existing_feed(client, session, farm):  # noqa: F811
    from farmcore.core import logs
    from tests.core.test_activity import _advice

    _advice(session, farm, "P1", "advice.changed", datetime(2024, 4, 8, 22, tzinfo=UTC))
    _advice(session, farm, "P1", "advice.changed", datetime(2024, 4, 8, 21, 59, tzinfo=UTC))
    pending = _spray(session, farm, ["P1"], NOW - timedelta(days=1), pending=True)
    logs.confirm(session, pending.id, "chat:42", NOW)
    session.commit()
    pid = _pid(session, farm, "P1")
    response = _provider(
        client, pid, farm.id, type="advice", **{"from": "2024-04-09", "to": "2024-04-09"}
    )
    assert len(_rows(response.text)) == 1
    suppressed = _provider(
        client, pid, farm.id, type="spray", **{"from": "2024-04-09", "to": "2024-04-09"}
    )
    assert _rows(suppressed.text) == []
    for query in ({"type": "harvest"}, {"from": "bad"}, {"from": "2024-04-10", "to": "2024-04-09"}):
        assert (
            client.get(
                f"/activity/parcels/{pid}/workspace", params={"farm": farm.id, **query}
            ).status_code
            == 422
        )


def test_same_time_cursor_reads_whole_pages_without_business_or_clock_writes(client, session, farm):  # noqa: F811
    from sqlalchemy import func, select

    from farmcore.core.models.canonical import Event, Log
    from farmcore.core.models.kernel import ClockState
    from farmcore.core.timeline import post_event

    pid = _pid(session, farm, "P1")
    # Invented, independent demo markers exercise tied event IDs without grouping advice together.
    for n in range(55):
        post_event(
            session,
            farm_id=farm.id,
            res_model="parcel",
            res_id=pid,
            kind="demo.qa",
            body=f"QA synthetic marker {n}",
            payload={},
            actor="seed",
            source_app="core",
            occurred_at=NOW,
        )
    session.commit()

    def stored_state():
        session.expire_all()
        clock = session.get(ClockState, 1)
        return (
            session.scalar(select(func.count()).select_from(Event)),
            session.scalar(select(func.count()).select_from(Log)),
            clock.mode,
            clock.replay_now,
            dict(clock.job_runs),
        )

    before = stored_state()
    first = _provider(client, pid, farm.id, type="demo")
    assert len(_rows(first.text)) == 50
    older = next(u for u in _links(first.text) if "before=" in u)
    shell = client.get(older)
    fragment = next(
        html.unescape(u)
        for u in re.findall(r'hx-get="([^"]+)"', shell.text)
        if u.startswith(f"/activity/parcels/{pid}/workspace")
    )
    last = client.get(fragment)
    assert len(_rows(last.text)) == 5
    markers = re.findall(
        r"QA synthetic marker (\d+)", "".join(_rows(first.text) + _rows(last.text))
    )
    assert sorted(map(int, markers)) == list(range(55))
    assert stored_state() == before


def test_field_activity_discloses_reading_notes_but_keeps_active_filters_visible(client, farm):  # noqa: F811
    body = client.get("/activity/", params={"farm": farm.id, "type": "spray"}).text
    detail = re.search(r"<details\b[^>]*data-activity-notes[^>]*>(.*?)</details>", body, re.S)
    assert detail is not None and " open" not in detail.group(0).split(">")[0]
    outside = body.replace(detail.group(0), "")
    assert 'aria-label="Filter activity"' in outside
    assert "Clear filters" in outside
