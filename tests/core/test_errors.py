"""Plain-words HTML error pages for browsers; JSON clients keep FastAPI's JSON errors."""

from __future__ import annotations

from farmcore.core.seed import ensure_default_farm

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def test_unseeded_page_in_a_browser_is_a_503_html_page_naming_the_seed_command(client):
    resp = client.get("/fields/", headers=HTML)

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("text/html")
    assert "farmcore seed" in resp.text
    assert "seed --scene B" in resp.text
    assert 'id="navbar"' in resp.text  # extends base.html


def test_unseeded_page_for_a_json_client_keeps_the_503_json_body(client):
    resp = client.get("/fields/", headers={"Accept": "application/json"})

    assert resp.status_code == 503
    assert resp.headers["content-type"] == "application/json"
    assert resp.json() == {
        "detail": "default farm farmcore-demo:chateau not found; run `farmcore seed`"
    }


def test_unseeded_data_download_in_a_browser_is_not_an_html_page(client):
    for path in ("/operations/register.csv", "/fields/parcels.geojson"):
        resp = client.get(path, headers=HTML)

        assert resp.status_code == 503, path
        assert not resp.headers["content-type"].startswith("text/html"), path
        assert "<html" not in resp.text, path


def test_unseeded_htmx_partial_is_not_a_full_html_page(client):
    resp = client.get("/fields/", headers={**HTML, "HX-Request": "true"})

    assert resp.status_code == 503
    assert resp.headers["content-type"] == "application/json"


def test_unknown_station_in_a_browser_is_a_404_page_naming_what_was_not_found(
    client, session, settings
):
    ensure_default_farm(session, settings)

    resp = client.get("/stations/999999", headers=HTML)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "No station with id 999999." in resp.text
    assert "farmcore seed" not in resp.text


def test_unknown_address_in_a_browser_says_the_page_was_not_found(client):
    resp = client.get("/no-such-page", headers=HTML)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Page not found." in resp.text
    assert "Not Found" not in resp.text  # Starlette's default detail


def test_invalid_path_value_in_a_browser_is_an_html_page_in_plain_words(client, session, settings):
    ensure_default_farm(session, settings)

    for path, words in (
        ("/stations/NOPE", "“NOPE” is not a valid station id: it should be a whole number."),
        ("/advisor/parcels/abc", "“abc” is not a valid parcel id: it should be a whole number."),
    ):
        resp = client.get(path, headers=HTML)

        assert resp.status_code == 422, path
        assert resp.headers["content-type"].startswith("text/html"), path
        assert 'id="navbar"' in resp.text, path  # extends base.html
        assert words in resp.text, path
        assert "int_parsing" not in resp.text and '"detail"' not in resp.text, path


def test_invalid_path_value_for_a_json_client_keeps_the_422_json_body(client, session, settings):
    ensure_default_farm(session, settings)

    resp = client.get("/stations/NOPE", headers={"Accept": "application/json"})

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "int_parsing"


def test_unknown_station_for_a_json_client_keeps_the_404_json_body(client, session, settings):
    ensure_default_farm(session, settings)

    resp = client.get("/stations/999999", headers={"Accept": "application/json"})

    assert resp.status_code == 404
    assert resp.json() == {"detail": "No station with id 999999."}


def test_other_errors_in_a_browser_show_status_and_detail(client):
    resp = client.post("/health", headers=HTML)

    assert resp.status_code == 405
    assert resp.headers["content-type"].startswith("text/html")
    assert "405: Method Not Allowed" in resp.text
    assert resp.headers["allow"] == "GET"
    assert "Traceback" not in resp.text


def test_bodyless_status_in_a_browser_keeps_the_default_empty_response():
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from farmcore.core import errors

    app = FastAPI()
    errors.register(app)

    @app.get("/cached")
    def cached():
        raise HTTPException(status_code=304)

    resp = TestClient(app).get("/cached", headers=HTML)

    assert resp.status_code == 304
    assert resp.content == b""
