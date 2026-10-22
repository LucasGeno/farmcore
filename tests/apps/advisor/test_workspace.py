"""Parcel Protection/History through HTTP, using only invented stored records."""

import html
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from farmcore.core.access import ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm, Log
from farmcore.core.models.kernel import AppModule, ClockState
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.operations.conftest import make_parcel

NOW = datetime(2024, 5, 10, 4, tzinfo=UTC)


@pytest.fixture
def stored(session, settings):
    farm = ensure_default_farm(session, settings)
    other = Farm(name="Other synthetic farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    complete_station(session, farm.id, NOW)
    complete_station(session, other.id, NOW)
    parcel = make_parcel(session, farm, 301, area="1.5", props={"variety": "Merlot"})
    foreign = make_parcel(session, other, 302, area="2", props={})
    advice = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="wait",
        props={"headline": "Synthetic stored current advice"},
        reasons=[
            {
                "rule": "season_gate",
                "verdict": "active",
                "inputs": {},
                "threshold": {"season_gate": "2024-04-01"},
                "source": "Synthetic source",
            }
        ],
    )
    session.add(advice)
    for year in (2022, 2024):
        session.add(
            Log(
                farm_id=farm.id,
                type="spray",
                status="done",
                name=f"Stored {year}",
                ts_start=datetime(year, 4, 9, 6, tzinfo=UTC),
                parcels=[parcel],
            )
        )
    set_replay(session, NOW, settings)
    session.commit()
    return farm, other, parcel, foreign


def links(body):
    return [html.unescape(u) for u in re.findall(r'href="([^"]+)"', body)]


def test_protection_reuses_current_evidence_without_duplicate_workspace_chrome(client, stored):
    farm, _, parcel, _ = stored
    canonical = client.get(f"/advisor/parcels/{parcel.id}", params={"farm": farm.id})
    page = client.get(f"/advisor/parcels/{parcel.id}/workspace", params={"farm": farm.id})
    assert canonical.status_code == page.status_code == 200
    for fact in ("Synthetic stored current advice", "Season gate", "Synthetic source"):
        assert fact in canonical.text and fact in page.text
    assert "<h1" not in page.text and "<html" not in page.text
    assert "All advice" not in page.text and "What happened here" not in page.text
    assert "history-heading" not in page.text
    workspace = client.get(
        f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "view": "protection"}
    )
    assert 'aria-current="page"' in workspace.text
    assert "/advisor/parcels/" in workspace.text and "/workspace" in workspace.text


@pytest.mark.parametrize("suffix", ["workspace", "history"])
def test_advisor_workspace_providers_enforce_lifecycle_and_route_scope(
    client, stored, session, suffix
):
    farm, other, parcel, foreign = stored
    assert (
        client.get(f"/advisor/parcels/{foreign.id}/{suffix}", params={"farm": farm.id}).status_code
        == 404
    )
    session.get(AppModule, "advisor").state = "uninstalled"
    session.commit()
    assert (
        client.get(f"/advisor/parcels/{parcel.id}/{suffix}", params={"farm": farm.id}).status_code
        == 404
    )


@pytest.mark.parametrize("season", [None, "2022"])
def test_canonical_card_hides_current_and_inline_history_when_advisor_turns_off(
    client, stored, session, season
):
    farm, _, parcel, _ = stored
    params = {"farm": farm.id, **({"season": season} if season else {})}
    url = f"/advisor/parcels/{parcel.id}"
    enabled = client.get(url, params=params)
    assert enabled.status_code == 200
    assert "Synthetic stored current advice" in enabled.text
    assert f'data-history-season="{season or "2024"}"' in enabled.text
    assert "9 Apr" in enabled.text
    session.get(AppModule, "advisor").state = "uninstalled"
    session.commit()
    disabled = client.get(url, params=params)
    assert disabled.status_code == 404
    assert "Synthetic stored current advice" not in disabled.text
    assert "data-history-season=" not in disabled.text
    session.get(AppModule, "advisor").state = "installed"
    session.commit()
    assert client.get(url, params=params).status_code == 200


@pytest.mark.parametrize("suffix", ["", "/history", "/workspace"])
def test_advisor_parcel_views_are_absent_when_advisor_starts_off(
    client, stored, session, settings, session_factory, suffix
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    farm, _, parcel, _ = stored
    session.get(AppModule, "advisor").state = "uninstalled"
    session.commit()
    with TestClient(create_app(settings, session_factory=session_factory)) as restarted:
        restarted.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        response = restarted.get(
            f"/advisor/parcels/{parcel.id}{suffix}", params={"farm": farm.id, "season": "2022"}
        )
    assert response.status_code == 404
    assert "data-history-season=" not in response.text


def test_history_year_is_ordinary_state_and_canonical_fallback_renders_without_javascript(
    client, stored
):
    farm, _, parcel, _ = stored
    parent = f"/fields/parcels/{parcel.id}?view=history&season=2022&farm={farm.id}"
    history = client.get(
        f"/advisor/parcels/{parcel.id}/history",
        params={"farm": farm.id, "season": "2022", "parent": parent},
    )
    assert history.status_code == 200
    assert 'data-history-season="2022"' in history.text
    assert 'name="season"' in history.text and "History season" in history.text
    assert 'method="get"' in history.text
    assert f'action="/fields/parcels/{parcel.id}"' in history.text
    assert 'name="view" value="history"' in history.text
    empty = client.get(
        f"/advisor/parcels/{parcel.id}/history", params={"farm": farm.id, "season": "2023"}
    )
    assert 'data-history-season="2023"' in empty.text and "No sprays logged" in empty.text
    canonical = client.get(
        f"/advisor/parcels/{parcel.id}",
        params={"farm": farm.id, "season": "2022", "return_to": parent},
    )
    assert canonical.status_code == 200
    assert 'data-history-season="2022"' in canonical.text  # already in server HTML
    assert "9 Apr" in canonical.text and "Back to parcel" in canonical.text
    assert canonical.text.count("data-history-season=") == 1
    assert not re.search(r'hx-get="[^"]+/history', canonical.text)


@pytest.mark.parametrize("season", ["bad", "2099", "0000", "²"])
def test_unsupported_history_year_has_notice_without_changing_current_state(
    client, stored, session, season
):
    farm, _, parcel, _ = stored
    before = (
        session.get(ClockState, 1).replay_now,
        dict(parcel.props),
        client.get(f"/advisor/parcels/{parcel.id}.json", params={"farm": farm.id}).json(),
    )
    response = client.get(
        f"/advisor/parcels/{parcel.id}/history", params={"farm": farm.id, "season": season}
    )
    assert response.status_code == 200
    assert 'data-history-season="2024"' in response.text
    assert "Showing history season 2024" in response.text
    session.expire_all()
    assert (
        session.get(ClockState, 1).replay_now,
        dict(parcel.props),
        client.get(f"/advisor/parcels/{parcel.id}.json", params={"farm": farm.id}).json(),
    ) == before


def test_withheld_current_protection_stays_in_view_and_history_fallback_remains_useful(
    client, stored, session
):
    farm, _, parcel, _ = stored
    farm.props = {"advisor_unavailable_reason": "Synthetic verified catalogue unavailable"}
    session.commit()
    current = client.get(f"/advisor/parcels/{parcel.id}/workspace", params={"farm": farm.id})
    assert current.status_code == 200 and "Synthetic verified catalogue unavailable" in current.text
    assert "data-advice-unavailable" in current.text and "<h1" not in current.text
    fallback = client.get(
        f"/advisor/parcels/{parcel.id}", params={"farm": farm.id, "season": "2022"}
    )
    assert 'data-history-season="2022"' in fallback.text
    assert "9 Apr" in fallback.text


@pytest.mark.parametrize("suffix", ["workspace", "history"])
def test_out_of_range_provider_parcel_is_a_scoped_404(client, stored, suffix):
    farm, _, _, _ = stored
    assert (
        client.get(f"/advisor/parcels/{2**80}/{suffix}", params={"farm": farm.id}).status_code
        == 404
    )


@pytest.mark.parametrize("app", ["advisor", "operations"])
def test_overview_onward_details_keep_parent_and_poll_preserves_original_origin(
    client, stored, app
):
    farm, _, parcel, _ = stored
    origin = f"/fields/?view=list&q=Merlot&farm={farm.id}"
    from urllib.parse import urlencode

    parent = f"/fields/parcels/{parcel.id}?" + urlencode(
        {"view": "overview", "farm": farm.id, "return_to": origin}
    )
    section = client.get(
        f"/{app}/map/parcels/{parcel.id}", params={"farm": farm.id, "parent": parent}
    )
    [detail] = [u for u in links(section.text) if urlsplit(u).path == f"/{app}/parcels/{parcel.id}"]
    [returned] = parse_qs(urlsplit(detail).query)["return_to"]
    assert urlsplit(returned).path == f"/fields/parcels/{parcel.id}"
    assert parse_qs(urlsplit(returned).query) == {"view": ["overview"], "farm": [str(farm.id)]}
    [poll] = [html.unescape(u) for u in re.findall(r'hx-get="([^"]+)"', section.text)]
    assert parse_qs(urlsplit(poll).query)["parent"] == [parent]
    again = client.get(poll)
    assert detail in links(again.text)


@pytest.mark.parametrize(
    "path,origin_path,model",
    [
        ("/advisor/today?view=attention", "/", None),
        ("/advisor/today", "/", None),
        ("/advisor/?model=powdery", "/advisor/", "powdery"),
        ("/advisor/?model=all", "/advisor/", "all"),
    ],
)
def test_real_home_and_protection_parcel_links_keep_named_source(
    client, stored, path, origin_path, model
):
    farm, _, parcel, _ = stored
    response = (
        client.get(path, params={"farm": farm.id})
        if "?" not in path
        else client.get(path + f"&farm={farm.id}")
    )
    targets = [
        u for u in links(response.text) if urlsplit(u).path == f"/fields/parcels/{parcel.id}"
    ]
    assert targets
    query = parse_qs(urlsplit(targets[0]).query)
    assert query["view"] == ["protection"] and query["farm"] == [str(farm.id)]
    [origin] = query["return_to"]
    assert urlsplit(origin).path == origin_path
    assert parse_qs(urlsplit(origin).query).get("farm") == [str(farm.id)]
    if model:
        assert parse_qs(urlsplit(origin).query)["model"] == [model]
    assert urlsplit(origin).fragment
    assert "/advisor/today" not in origin


def test_plan_parcel_link_preserves_its_existing_selected_day(client, stored, session):
    farm, _, parcel, _ = stored
    advice = session.scalar(select(Advice).where(Advice.parcel_id == parcel.id))
    advice.next_window_start, advice.next_window_end = NOW, NOW + timedelta(hours=2)
    session.commit()
    plan = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-05-10"})
    assert plan.status_code == 200 and 'data-plan-detail="2024-05-10"' in plan.text
    [target] = [u for u in links(plan.text) if urlsplit(u).path == f"/fields/parcels/{parcel.id}"]
    [origin] = parse_qs(urlsplit(target).query)["return_to"]
    assert urlsplit(origin).path == "/forecast/plan"
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(farm.id)], "day": ["2024-05-10"]}
    assert urlsplit(origin).fragment == "plan-day-2024-05-10"
    returned = client.get(origin)
    assert 'id="plan-day-2024-05-10"' in returned.text


def test_home_map_data_and_popup_origin_freeze_resolved_farm_before_cookie_changes(client, stored):
    farm, other, parcel, _ = stored
    from farmcore.core.access import FARM_COOKIE

    client.cookies.set(FARM_COOKIE, str(farm.id))
    page = client.get("/fields/today", params={"compact": "1"}).text
    [origin] = [html.unescape(u) for u in re.findall(r'data-workspace-origin="([^"]+)"', page)]
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(farm.id)]}
    layers = json.loads(html.unescape(re.search(r"data-layers='([^']*)'", page)[1]))
    urls = [layer["url"] for layer in layers]
    urls += [html.unescape(u) for u in re.findall(r'data-geojson-url="([^"]+)"', page)]
    assert all(parse_qs(urlsplit(u).query).get("farm") == [str(farm.id)] for u in urls)
    client.cookies.set(FARM_COOKIE, str(other.id))
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        if "/fields/" in url:
            assert response.json()["features"][0]["properties"]["id"] == parcel.id


def test_history_form_and_fallback_keep_year_origin_and_existing_download_scope(client, stored):
    farm, _, parcel, _ = stored
    origin = f"/advisor/?model=powdery&farm={farm.id}#advisor-parcel-{parcel.id}"
    shell = client.get(
        f"/fields/parcels/{parcel.id}",
        params={"farm": farm.id, "view": "history", "season": "2022", "return_to": origin},
    )
    providers = {
        html.unescape(u)
        for u in re.findall(r'hx-get="([^"]+)"', shell.text)
        if f"/advisor/parcels/{parcel.id}/history" in html.unescape(u)
    }
    [provider] = providers
    body = client.get(provider).text
    action = html.unescape(re.search(r'<form method="get" action="([^"]+)"', body)[1])
    controls = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', body)
    }
    assert controls == {"farm": str(farm.id), "view": "history", "return_to": origin}
    changed = client.get(action, params={**controls, "season": "2023"})
    assert changed.status_code == 200
    fallbacks = [
        u for u in links(changed.text) if urlsplit(u).path == f"/advisor/parcels/{parcel.id}"
    ]
    [fallback] = fallbacks
    assert parse_qs(urlsplit(fallback).query)["season"] == ["2023"]
    assert urlsplit(fallback).fragment == "history-heading"
    canonical = client.get(fallback)
    assert 'data-history-season="2023"' in canonical.text and "No sprays logged" in canonical.text
    expected = {
        f"/operations/register.csv?season=2023&parcel={parcel.id}&farm={farm.id}",
        f"/share/ngsi-ld.json?season=2023&parcel={parcel.id}&farm={farm.id}",
    }
    assert expected <= set(links(canonical.text))
    assert "Back to parcel" in canonical.text


@pytest.mark.parametrize(
    "parent",
    [
        "https://evil.example/",
        "/fields/parcels/999999?view=history",
        "/fields/parcels/{parcel}?view=protection",
        "/fields/parcels/{parcel}?view=history&farm=999999",
    ],
)
def test_forged_history_parent_has_only_same_farm_canonical_controls(client, stored, parent):
    farm, _, parcel, _ = stored
    body = client.get(
        f"/advisor/parcels/{parcel.id}/history",
        params={"farm": farm.id, "season": "2022", "parent": parent.format(parcel=parcel.id)},
    ).text
    assert f'action="/advisor/parcels/{parcel.id}#history-heading"' in body
    controls = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', body)
    assert controls == [("farm", str(farm.id))]


def test_tour_missing_current_advice_and_replay_do_not_expose_hidden_facts(
    client, stored, session, settings
):
    farm, _, parcel, _ = stored
    client.cookies.set("farmcore_tour", "1")
    set_replay(session, NOW - timedelta(days=1), settings)
    session.commit()
    page = client.get(
        f"/advisor/parcels/{parcel.id}/workspace", params={"farm": farm.id, "debug": "1"}
    )
    assert page.status_code == 200
    assert "The advisor has not evaluated this parcel" in page.text
    assert "Synthetic stored current advice" not in page.text
    assert ".json" not in page.text and "Inputs</th>" not in page.text


def test_home_aggregate_remains_aggregate_with_multiple_parcel_entries(client, stored, session):
    farm, _, parcel, _ = stored
    another = make_parcel(session, farm, 303, area="1", props={})
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=another.id,
            evaluated_at=NOW,
            decision="wait",
            props={"headline": "Other current advice"},
        )
    )
    session.commit()
    page = client.get("/advisor/today", params={"farm": farm.id, "view": "attention"})
    targets = {
        urlsplit(u).path
        for u in links(page.text)
        if urlsplit(u).path.startswith("/fields/parcels/")
    }
    assert targets == {f"/fields/parcels/{parcel.id}", f"/fields/parcels/{another.id}"}
    assert f"/advisor/?farm={farm.id}" in links(page.text)


def test_home_map_wrapper_keeps_its_source_farm_for_open_and_retry(client, stored):
    farm, other, _, _ = stored
    from farmcore.core.access import FARM_COOKIE

    client.cookies.set(FARM_COOKIE, str(farm.id))
    wrapper = client.get("/fields/today?desktop=1").text
    assert f"/fields/?farm={farm.id}" in links(wrapper)
    urls = [html.unescape(u) for u in re.findall(r'hx-get="([^"]+)"', wrapper)]
    assert urls and all(parse_qs(urlsplit(u).query).get("farm") == [str(farm.id)] for u in urls)
    client.cookies.set(FARM_COOKIE, str(other.id))
    for url in urls:
        child = client.get(url)
        assert child.status_code == 200
        [origin] = re.findall(r'data-workspace-origin="([^"]+)"', child.text)
        assert parse_qs(urlsplit(html.unescape(origin)).query)["farm"] == [str(farm.id)]


@pytest.mark.parametrize(
    "source,poll_path",
    [
        ("/advisor/today?view=attention", "/advisor/today"),
        ("/advisor/today", "/advisor/today"),
        ("/forecast/plan?day=2024-05-10", "/forecast/plan/days"),
    ],
)
def test_source_advice_and_plan_polls_freeze_cookie_resolved_farm(
    client, stored, source, poll_path
):
    from farmcore.core.access import FARM_COOKIE

    farm, other, _, _ = stored
    client.cookies.set(FARM_COOKIE, str(farm.id))
    page = client.get(source).text
    urls = [
        html.unescape(u)
        for u in re.findall(r'hx-get="([^"]+)"', page)
        if urlsplit(html.unescape(u)).path == poll_path
    ]
    assert urls
    assert all(parse_qs(urlsplit(u).query).get("farm") == [str(farm.id)] for u in urls)
    client.cookies.set(FARM_COOKIE, str(other.id))
    for url in urls:
        assert client.get(url).status_code == 200


def test_peer_priority_survives_shared_canonical_and_workspace_advice(client, stored):
    farm, _, parcel, _ = stored
    for suffix in ("", "/workspace"):
        response = client.get(f"/advisor/parcels/{parcel.id}{suffix}", params={"farm": farm.id})
        assert response.status_code == 200
        assert 'aria-label="priority 1 of 3: Due"' in response.text
        assert response.text.count("data-priority-bar=") == 3
        assert "assumption, agronomist to confirm" in response.text
        assert 'id="decision-badge" data-decision="wait"' in response.text


def test_field_daily_brief_gives_one_scoped_review_action_and_advice_freshness(client, stored):
    farm, _, parcel, _ = stored
    body = client.get("/advisor/today", params={"farm": farm.id, "view": "attention"}).text
    primary = re.search(r"<a\b[^>]*data-brief-action[^>]*>(.*?)</a>", body, re.S)
    assert primary is not None and "Review parcel" in primary.group(1)
    assert f"/fields/parcels/{parcel.id}?" in primary.group(0)
    assert "Advice checked" in body and "10 May" in body
    assert "data-brief-parcels" in body


def test_field_parcel_overview_puts_current_answer_before_secondary_metadata_and_aerial(
    client, stored
):
    farm, _, parcel, _ = stored
    body = client.get(f"/fields/parcels/{parcel.id}", params={"farm": farm.id}).text
    assert body.index("/advisor/map/parcels/") < body.index('id="crop-heading"')
    secondary = re.search(r"<details\b[^>]*data-parcel-metadata[^>]*>(.*?)</details>", body, re.S)
    assert secondary is not None
    assert "Crop details" in secondary.group(0) and "/aerial" in secondary.group(0)
    assert " open" not in secondary.group(0).split(">")[0]


@pytest.mark.parametrize("expert", [False, True])
def test_parcel_rule_details_follow_explicit_density_never_viewport(client, stored, expert):
    farm, _, parcel, _ = stored
    if expert:
        client.cookies.set("expert", "1")
    body = client.get(f"/advisor/parcels/{parcel.id}/workspace", params={"farm": farm.id}).text
    details = re.search(r'<details\b[^>]*id="why"[^>]*>', body)
    assert details is not None
    assert (" open" in details.group(0)) is expert
    assert "matchMedia" not in body
    assert "Synthetic stored current advice" in body


def test_field_checklist_is_secondary_but_missing_inputs_and_flags_stay_visible(
    client, stored, session
):
    farm, _, parcel, _ = stored
    advice = session.scalar(select(Advice).where(Advice.parcel_id == parcel.id))
    advice.reasons = [
        *advice.reasons,
        {
            "rule": "downy_3_10",
            "verdict": "no data",
            "inputs": {},
            "threshold": {"downy_temp_min_c": 10, "downy_rain_min_mm": 10, "downy_window_h": 48},
            "source": "Synthetic source",
        },
    ]
    advice.props = {"headline": "Synthetic stored current advice", "flags": ["data_gap"]}
    session.commit()
    body = client.get(f"/advisor/parcels/{parcel.id}/workspace", params={"farm": farm.id}).text
    details = re.search(r"<details\b[^>]*data-advice-checklist[^>]*>(.*?)</details>", body, re.S)
    assert details is not None and " open" not in details.group(0).split(">")[0]
    assert 'data-check="rain"' in details.group(0)
    outside = body.replace(details.group(0), "")
    assert "Some downy mildew checks have no data" in outside
    assert 'data-flag="data_gap"' in outside


@pytest.mark.parametrize(
    "decision, view, expected",
    [
        ("spray", "attention", "Review parcel"),
        ("wait", "attention", "Review parcel"),
        ("protected", "calm", "parcel protected"),
        ("unknown", "calm", "No advice yet"),
    ],
)
def test_daily_brief_preserves_decision_states_and_visible_weather_gaps(
    client, stored, session, decision, view, expected
):
    farm, _, parcel, _ = stored
    advice = session.scalar(select(Advice).where(Advice.parcel_id == parcel.id))
    if decision == "unknown":
        session.delete(advice)
    else:
        advice.decision = decision
        advice.props = {"headline": "Synthetic stored current advice", "flags": ["data_gap"]}
    session.commit()
    body = client.get("/advisor/today", params={"farm": farm.id, "view": view}).text
    assert expected in body and "data-home-empty" not in body
    if decision == "unknown":
        assert "Nothing to spray today" not in body
    else:
        assert 'data-brief-warning="data_gap"' in body
        assert "missing station hours" in body
