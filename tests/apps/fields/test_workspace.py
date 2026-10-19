"""Parcels and workspace contracts through rendered HTTP pages; synthetic records only."""

import html
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from farmcore.apps.fields.manifest import MANIFEST as FIELDS
from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.operations.conftest import make_parcel


def links(markup):
    return [html.unescape(u) for u in re.findall(r'href="([^"]+)"', markup)]


@pytest.fixture
def app_client(settings, session_factory):
    with TestClient(create_app(settings, manifests=[FIELDS], session_factory=session_factory)) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        yield c


@pytest.fixture
def farms(app_client, session, settings):
    farm = ensure_default_farm(session, settings)
    other_farm = Farm(name="Workspace second farm")
    session.add(other_farm)
    session.flush()
    ensure_owner_grant(session, other_farm)
    parcel = make_parcel(
        session,
        farm,
        1,
        area="1.25",
        props={
            "variety": "Merlot",
            "crop_label": "Wine grapes",
            "planted_year": 2001,
        },
    )
    partial = make_parcel(session, farm, 2, area="0.8", props={})
    foreign = make_parcel(session, other_farm, 3, area="2", props={})
    session.commit()
    app_client.cookies.set(FARM_COOKIE, str(farm.id))
    return farm, other_farm, parcel, partial, foreign


def test_list_search_opens_workspace_with_frozen_search_origin(app_client, farms):
    farm, _, parcel, _, _ = farms
    response = app_client.get("/fields/", params={"view": "list", "q": "mERlot"})
    assert response.status_code == 200
    assert 'aria-label="Parcel search"' in response.text
    assert "Test parcel 1" in response.text and "Test parcel 2" not in response.text
    [url] = [u for u in links(response.text) if urlsplit(u).path == f"/fields/parcels/{parcel.id}"]
    query = parse_qs(urlsplit(url).query)
    assert query["farm"] == [str(farm.id)]
    origin = urlsplit(query["return_to"][0])
    assert origin.path == "/fields/"
    assert parse_qs(origin.query) == {"view": ["list"], "q": ["mERlot"], "farm": [str(farm.id)]}
    workspace = app_client.get(url)
    assert workspace.status_code == 200
    assert "Back to Parcels" in workspace.text
    assert query["return_to"][0] in links(workspace.text)


def test_cookie_resolved_workspace_aerial_keeps_its_farm_after_another_tab_switches(
    app_client, farms
):
    farm, other, parcel, _, _ = farms
    page = app_client.get(f"/fields/parcels/{parcel.id}")
    assert page.status_code == 200
    aerial_path = f"/fields/parcels/{parcel.id}/aerial"
    urls = {
        html.unescape(value)
        for value in re.findall(r'(?:href|hx-get)="([^"]+)"', page.text)
        if urlsplit(html.unescape(value)).path == aerial_path
    }
    assert len(urls) == 1
    [aerial_url] = urls
    app_client.cookies.set(FARM_COOKIE, str(other.id))
    assert app_client.get(aerial_path, params={"farm": farm.id}).status_code == 200
    for headers in ({}, {"HX-Request": "true"}):
        aerial = app_client.get(aerial_url, headers=headers)
        assert aerial.status_code == 200
        assert parcel.name in aerial.text
    assert parse_qs(urlsplit(aerial_url).query) == {"farm": [str(farm.id)]}
    assert app_client.get(aerial_path, params={"farm": other.id}).status_code == 404


@pytest.mark.parametrize("needle", ["%", "%2f", "_", "\"'", "two words", "<script>"])
def test_literal_search_is_escaped_and_survives_return(app_client, farms, session, needle):
    farm, _, parcel, _, _ = farms
    parcel.name = f"Literal {needle} value"
    session.commit()
    page = app_client.get("/fields/", params={"view": "list", "q": needle})
    assert page.status_code == 200
    assert "Test parcel 2" not in page.text
    assert parcel.name in html.unescape(page.text)
    assert "<script>" not in page.text if needle == "<script>" else True
    [url] = [u for u in links(page.text) if urlsplit(u).path == f"/fields/parcels/{parcel.id}"]
    detail = app_client.get(url)
    assert "Back to Parcels" in detail.text
    back = [u for u in links(detail.text) if parse_qs(urlsplit(u).query).get("q") == [needle]]
    assert back
    assert parse_qs(urlsplit(back[0]).query)["farm"] == [str(farm.id)]


@pytest.mark.parametrize("needle", ["wine GRAPES", "FR-TEST-1", "P1"])
def test_search_includes_known_current_crop_iacs_and_alias(app_client, farms, needle):
    body = app_client.get("/fields/", params={"view": "list", "q": needle}).text
    assert "Test parcel 1" in body and "Test parcel 2" not in body


def test_modes_nav_empty_results_and_search_bound(app_client, farms):
    farm, _, parcel, _, _ = farms
    page = app_client.get("/fields/", params={"view": "list", "q": "missing"})
    assert "No parcels match" in page.text and "Reset search" in page.text
    assert f"/fields/?view=map&farm={farm.id}" in links(page.text)
    assert "q=missing" not in next(u for u in links(page.text) if "view=map" in u)
    bound = app_client.get("/fields/", params={"view": "list", "q": "a" * 81})
    assert bound.status_code == 200 and "80 characters" in bound.text
    for url in ("/fields/", "/fields/?view=list", f"/fields/parcels/{parcel.id}"):
        body = app_client.get(url).text
        assert re.search(r'<a href="/fields/(\?farm=[0-9]+)?"\s+aria-current="page"', body)
        assert ">Map</span>" in body
    unknown = app_client.get("/fields/?view=alien")
    assert "Unknown presentation" in unknown.text and "data-map-panel" in unknown.text
    assert "data-map-panel" in app_client.get("/fields/").text
    assert "data-map-panel" in app_client.get(f"/fields/?parcel={parcel.id}").text


def test_no_parcels_is_distinct_from_no_results(app_client, farms, session):
    _, second, _, _, _ = farms
    # Second farm has a parcel, so an unmatched query is a no-results state.
    empty = app_client.get("/fields/", params={"view": "list", "farm": second.id, "q": "no"})
    assert "No parcels match" in empty.text
    third = Farm(name="Empty workspace farm")
    session.add(third)
    session.flush()
    ensure_owner_grant(session, third)
    session.commit()
    empty = app_client.get("/fields/", params={"view": "list", "farm": third.id})
    assert "No parcels recorded" in empty.text and "No parcels match" not in empty.text


def test_overview_current_metadata_direct_fallback_and_scope(app_client, client, farms):
    farm, second, parcel, partial, foreign = farms
    body = app_client.get(f"/fields/parcels/{parcel.id}").text
    for value in ("Crop details", "Wine grapes", "2001", "FR-TEST-1", "Merlot", "1.25 ha"):
        assert value in body
    assert f"/fields/?parcel={parcel.id}&farm={farm.id}#map-panel-heading" in links(body)
    unknown = app_client.get(f"/fields/parcels/{partial.id}").text
    assert unknown.count("Not recorded") >= 3
    assert "No app adds facts here yet." in unknown
    assert app_client.get(f"/fields/parcels/{foreign.id}").status_code == 404
    assert app_client.get(f"/fields/parcels/{foreign.id}?farm={second.id}").status_code == 200
    for pid in (0, 999999, 99999999999999999):
        assert app_client.get(f"/fields/parcels/{pid}").status_code == 404
    switched = client.get(
        "/farms/select",
        params={
            "farm": second.id,
            "next": f"/fields/parcels/{parcel.id}?view=overview&return_to=/",
        },
        follow_redirects=False,
    )
    assert switched.headers["location"] == f"/fields/?farm={second.id}"


def test_generic_views_forward_only_declared_query_and_canonical_fallback(
    settings,
    session_factory,
    session,
    farms,
):
    from farmcore.core.manifest import Manifest, ParcelView
    from farmcore.core.models.kernel import AppModule

    farm, _, parcel, _, _ = farms
    provider = Manifest(
        name="fixture",
        version="1",
        nav=("Fixture records", "/fixture/"),
        parcel_sections=("/fixture/facts/{id}",),
        parcel_views=(
            ParcelView(
                "archive",
                "Season records",
                "/fixture/records/{id}",
                "/fixture/canonical/{id}#record-heading",
                20,
                ("season",),
            ),
            ParcelView(
                "work",
                "Grouped work",
                "/fixture/work/{id}",
                "/fixture/?parcel_id={id}",
                30,
                ("type", "from", "to", "before", "before_id"),
            ),
        ),
    )
    app = create_app(settings, manifests=[FIELDS, provider], session_factory=session_factory)
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    probe = APIRouter()

    @probe.get("/fixture/records/{parcel_id}")
    def records(request: Request, parcel_id: int):
        return JSONResponse(dict(request.query_params))

    app.include_router(probe)
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        origin = f"/fields/?view=list&q=Merlot&farm={farm.id}"
        page = c.get(
            f"/fields/parcels/{parcel.id}",
            params={
                "farm": farm.id,
                "view": "archive",
                "season": "2023",
                "type": "spray",
                "return_to": origin,
                "token": "do-not-copy",
            },
        )
        assert page.status_code == 200
        assert 'aria-current="page">Season records</a>' in page.text
        [fragment] = list(
            dict.fromkeys(
                html.unescape(u)
                for u in re.findall(r'hx-get="([^"]+)"', page.text)
                if u.startswith("/fixture/records/")
            )
        )
        params = c.get(fragment).json()
        assert params["season"] == "2023" and params["farm"] == str(farm.id)
        assert "type" not in params and "token" not in params
        parent = urlsplit(params["parent"])
        assert parent.path == f"/fields/parcels/{parcel.id}"
        assert parse_qs(parent.query) == {
            "view": ["archive"],
            "farm": [str(farm.id)],
            "season": ["2023"],
            "return_to": [origin],
        }
        fallback = next(
            u for u in links(page.text) if urlsplit(u).path == f"/fixture/canonical/{parcel.id}"
        )
        fq = parse_qs(urlsplit(fallback).query)
        assert fq["season"] == ["2023"] and urlsplit(fallback).fragment == "record-heading"
        onward = parse_qs(urlsplit(fq["return_to"][0]).query)
        assert onward == {"view": ["archive"], "farm": [str(farm.id)], "season": ["2023"]}
        work = next(
            u for u in links(page.text) if parse_qs(urlsplit(u).query).get("view") == ["work"]
        )
        assert parse_qs(urlsplit(work).query) == {
            "view": ["work"],
            "farm": [str(farm.id)],
            "return_to": [origin],
        }
        assert (
            "No longer available"
            in c.get(f"/fields/parcels/{parcel.id}?view=unknown&farm={farm.id}").text
        )
        session.get(AppModule, "fixture").state = "uninstalled"
        session.commit()
        off = c.get(f"/fields/parcels/{parcel.id}?view=archive&farm={farm.id}")
        assert "No longer available" in off.text and "Season records</a>" not in off.text


def test_overview_uses_current_sections_with_noscript_reading_links(
    settings, session_factory, farms
):
    from farmcore.core.manifest import Manifest

    farm, _, parcel, _, _ = farms
    facts = Manifest(
        name="facts",
        version="1",
        nav=("Facts", "/facts/"),
        parcel_sections=("/facts/parcels/{id}",),
    )
    provenance = Manifest(
        name="provenance", version="1", extra_nav=(), parcel_sections=("/provenance/parcels/{id}",)
    )
    with TestClient(
        create_app(settings, manifests=[FIELDS, facts, provenance], session_factory=session_factory)
    ) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        body = c.get(f"/fields/parcels/{parcel.id}?farm={farm.id}").text
        assert "Retry Facts" in body and "Retry Provenance" in body
        noscript = "".join(re.findall(r"<noscript>(.*?)</noscript>", body, re.S))
        assert f"/facts/?farm={farm.id}" in links(noscript)
        assert "Provenance requires JavaScript" in noscript
        assert "data-fragment" not in noscript


@pytest.mark.parametrize(
    "view, state",
    [
        ("archive", {"season": "2023"}),
        (
            "work",
            {
                "type": "spray",
                "from": "2024-03-01",
                "to": "2024-04-01",
                "before": "2024-03-15T12:00:00Z",
                "before_id": "9",
            },
        ),
    ],
)
def test_declared_workspace_reading_origins_preserve_provider_queries(
    settings,
    session_factory,
    farms,
    view,
    state,
):
    from urllib.parse import urlencode

    from farmcore.core.manifest import Manifest, ParcelView

    farm, _, parcel, _, _ = farms
    provider = Manifest(
        name="fixture",
        version="1",
        parcel_views=(
            ParcelView(
                "archive",
                "Season records",
                "/fixture/records/{id}",
                "/fixture/canonical/{id}",
                query_keys=("season",),
            ),
            ParcelView(
                "work",
                "Grouped work",
                "/fixture/work/{id}",
                "/fixture/?parcel_id={id}",
                query_keys=("type", "from", "to", "before", "before_id"),
            ),
        ),
    )
    with TestClient(
        create_app(settings, manifests=[FIELDS, provider], session_factory=session_factory)
    ) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        origin = f"/fields/parcels/{parcel.id}?" + urlencode(
            {"view": view, **state, "farm": farm.id}
        )
        body = c.get(
            f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "return_to": origin}
        ).text
        assert origin in links(body)
        assert "Back to parcel" in body
        bad = c.get(
            f"/fields/parcels/{parcel.id}",
            params={"farm": farm.id, "return_to": origin + "&debug=1"},
        ).text
        assert origin + "&debug=1" not in links(bad)


def test_provider_parent_validation_keeps_single_original_origin(
    settings,
    session_factory,
    farms,
):
    from urllib.parse import urlencode

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from farmcore.core.access import Repo
    from farmcore.core.manifest import Manifest, ParcelView

    farm, other, parcel, _, _ = farms
    provider = Manifest(
        name="fixture",
        version="1",
        parcel_views=(
            ParcelView(
                "archive",
                "Archive",
                "/fixture/records/{id}",
                "/fixture/canonical/{id}",
                query_keys=("season",),
            ),
        ),
    )
    app = create_app(settings, manifests=[FIELDS, provider], session_factory=session_factory)

    @app.get("/inspect/{parcel_id}")
    def inspect(request: Request, parcel_id: int, repo: Repo):
        from farmcore.core.navigation import workspace_parent

        return JSONResponse({"parent": workspace_parent(request, repo, parcel_id, "archive")})

    origin = f"/fields/?view=list&q=%25&farm={farm.id}"
    base = f"/fields/parcels/{parcel.id}"
    parent = (
        base
        + "?"
        + urlencode({"view": "archive", "season": "2023", "farm": farm.id, "return_to": origin})
    )
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        good = c.get(f"/inspect/{parcel.id}", params={"farm": farm.id, "parent": parent})
        assert good.json()["parent"] == parent
        for bad in (
            parent + "&debug=1",
            parent + "&season=2022",
            parent.replace("view=archive", "view=work"),
            parent.replace(f"farm={farm.id}", f"farm={other.id}"),
            "https://evil.example" + parent,
            base + "?view=archive&return_to=" + "/fields/?return_to=/",
            parent + "#heading",
            "http://[invalid",
            parent.replace("/fields/", "/fields/\n"),
            parent + "&season=%GG",
            base + "?view=archive&broken",
            parent + "x" * 2049,
        ):
            assert c.get(
                f"/inspect/{parcel.id}", params={"farm": farm.id, "parent": bad}
            ).json() == {"parent": None}
        assert c.get(f"/inspect/{parcel.id}", params={"farm": farm.id}).json() == {"parent": None}


def test_map_rows_and_compact_panel_open_workspace_even_without_advisor(app_client, farms):
    farm, _, parcel, _, _ = farms
    for url in ("/fields/", f"/fields/panel/parcels/{parcel.id}"):
        body = app_client.get(url).text
        detail = [u for u in links(body) if urlsplit(u).path == f"/fields/parcels/{parcel.id}"]
        assert detail
        assert parse_qs(urlsplit(detail[0]).query)["farm"] == [str(farm.id)]
        assert parse_qs(urlsplit(detail[0]).query)["return_to"] == [
            f"/fields/?parcel={parcel.id}&farm={farm.id}#map-panel-heading"
        ]


def test_unqualified_valid_origin_stays_frozen_across_views(app_client, farms):
    farm, _, parcel, _, _ = farms
    body = app_client.get(
        f"/fields/parcels/{parcel.id}", params={"return_to": "/fields/?view=list&q=Merlot"}
    ).text
    overview = next(u for u in links(body) if urlsplit(u).path == f"/fields/parcels/{parcel.id}")
    assert parse_qs(urlsplit(overview).query)["return_to"] == [
        f"/fields/?view=list&q=Merlot&farm={farm.id}"
    ]


def test_long_imported_names_and_metadata_have_wrapping_contract(app_client, farms, session):
    _, _, parcel, _, _ = farms
    parcel.name = "LongImportedName" * 40
    parcel.props = {"variety": "LongImportedVariety" * 40}
    session.commit()
    listing = app_client.get("/fields/?view=list").text
    workspace = app_client.get(f"/fields/parcels/{parcel.id}").text
    for body in (listing, workspace):
        assert parcel.name in body and parcel.props["variety"] in body
    # The shared shape precedes the text; its shrinking text container owns wrapping.
    assert re.search(
        r'<a[^>]*class="[^"]*min-h-\[44px\][^"]*"[^>]*>.*?'
        r'<span class="[^"]*min-w-0[^"]*break-words[^"]*">P1 · LongImportedName',
        listing,
        re.S,
    )
    assert re.search(r'<p class="[^"]*break-words[^\"]*">LongImportedVariety', listing)
    assert re.search(r'<h1[^>]*class="[^"]*break-words', workspace)
    assert re.search(r'<p class="[^"]*break-words', workspace)


@pytest.mark.parametrize(
    "state",
    [
        "view=list&parcel={parcel}",
        "view=map&q=Merlot",
        "view=alien",
        "view=list&q=" + "a" * 81,
        "parcel=%252f",
        "view=overview&season=2023",
    ],
)
def test_unsupported_list_origins_do_not_replace_safe_map_fallback(app_client, farms, state):
    farm, _, parcel, _, _ = farms
    origin = "/fields/?" + state.format(parcel=parcel.id)
    body = app_client.get(f"/fields/parcels/{parcel.id}", params={"return_to": origin}).text
    assert origin not in links(body)
    assert f"/fields/?parcel={parcel.id}&farm={farm.id}#map-panel-heading" in links(body)


def test_disabled_view_origins_and_oversized_state_recover_safely(
    settings, session_factory, session, farms
):
    from farmcore.core.manifest import Manifest, ParcelView
    from farmcore.core.models.kernel import AppModule

    farm, _, parcel, _, _ = farms
    provider = Manifest(
        name="fixture",
        version="1",
        parcel_views=(
            ParcelView(
                "archive",
                "Archive",
                "/fixture/records/{id}",
                "/fixture/canonical/{id}",
                query_keys=("season",),
            ),
        ),
    )
    with TestClient(
        create_app(settings, manifests=[FIELDS, provider], session_factory=session_factory)
    ) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        origin = f"/fields/parcels/{parcel.id}?view=archive&season=" + "2" * 257
        body = c.get(
            f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "return_to": origin}
        ).text
        assert origin not in links(body)
        session.get(AppModule, "fixture").state = "uninstalled"
        session.commit()
        origin = f"/fields/parcels/{parcel.id}?view=archive&season=2023"
        body = c.get(
            f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "return_to": origin}
        ).text
        assert "Back to Map" in body
        assert f"/fields/?parcel={parcel.id}&farm={farm.id}#map-panel-heading" in links(body)


def test_overview_sections_receive_validated_workspace_parent_with_list_origin(
    settings,
    session_factory,
    farms,
):
    from fastapi import Request

    from farmcore.core.access import Repo
    from farmcore.core.manifest import Manifest
    from farmcore.core.navigation import workspace_parent

    farm, _, parcel, _, _ = farms
    facts = Manifest(name="facts", version="1", parcel_sections=("/facts/parcels/{id}",))
    app = create_app(settings, manifests=[FIELDS, facts], session_factory=session_factory)

    @app.get("/facts/parcels/{parcel_id}")
    def facts_page(request: Request, parcel_id: int, repo: Repo):
        return {"parent": workspace_parent(request, repo, parcel_id, "overview")}

    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        origin = f"/fields/?view=list&q=Merlot&farm={farm.id}"
        body = c.get(
            f"/fields/parcels/{parcel.id}", params={"farm": farm.id, "return_to": origin}
        ).text
        urls = list(
            dict.fromkeys(
                html.unescape(u)
                for u in re.findall(r'hx-get="([^"]+)"', body)
                if u.startswith("/facts/parcels/")
            )
        )
        assert len(urls) == 1
        parent = c.get(urls[0]).json()["parent"]
        assert urlsplit(parent).path == f"/fields/parcels/{parcel.id}"
        assert parse_qs(urlsplit(parent).query) == {
            "view": ["overview"],
            "farm": [str(farm.id)],
            "return_to": [origin],
        }
