"""The viewer picks the Activity columns. `GET /activity/columns?show=…&next=…` only
sets a per-farm cookie (no database write) and goes back; extra columns Who (masked like
/share/), Machine file device(s) and Farm; "What" is always shown."""

from __future__ import annotations

from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

import pytest
from sqlalchemy import func, select

from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import AccessLog, Event, Farm
from farmcore.core.models.kernel import AppModule
from tests.apps.activity.test_columns import _cells, _header, _rows
from tests.core.test_activity import NOW, _spray, farm  # noqa: F401  (fixture)

COOKIE = "farmcore_activity_cols_{}"


def _show(client, *keys: str, next: str = "/activity/"):
    params = [("show", k) for k in keys] + [("next", next)]
    return client.get("/activity/columns", params=params, follow_redirects=False)


def test_columns_link_sets_a_per_farm_cookie_and_goes_back(client, session, farm) -> None:  # noqa: F811
    resp = _show(client, "parcel", "who")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/activity/"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(COOKIE.format(farm.id) + "=")
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()


def test_chosen_columns_are_shown_in_their_fixed_order(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1), actor="chat:123456789")
    session.commit()
    _show(client, "farm", "who", "time")
    page = client.get("/activity/").text
    assert _header(page) == ["time", "what", "who", "farm"]  # "what" is always shown
    cells = _cells(_rows(page)[0])
    assert cells["who"] == "Telegram chat …6789"
    assert "123456789" not in page
    assert cells["farm"] == farm.name


def test_device_column_reads_machine_file_and_dashes_without_one(client, session, farm) -> None:  # noqa: F811
    _spray(session, farm, ["P1"], NOW - timedelta(hours=1))
    session.commit()
    _show(client, "time", "device")
    page = client.get("/activity/").text
    assert "Machine file device(s)" in page
    assert _cells(_rows(page)[0])["device"] == "—"


def test_unknown_keys_are_ignored_and_next_is_kept_local(client, farm) -> None:  # noqa: F811
    resp = _show(client, "time", "secret", next="https://evil.example/")
    assert resp.headers["location"] == "/"
    assert "secret" not in client.get("/activity/").text


def test_who_labels_for_each_kind_of_actor(client, session, farm) -> None:  # noqa: F811
    actors = ["owner", "cli:operations.import-isoxml", "seed", "job:x"]
    for h, actor in enumerate(actors, start=1):
        _spray(session, farm, [f"P{h}"], NOW - timedelta(hours=h), actor=actor)
    session.commit()
    _show(client, "who")
    rows = _rows(client.get("/activity/").text)
    assert [_cells(r)["who"] for r in rows] == [
        "Farmer (web)",
        "Machine-file import (CLI)",
        "Demo data",
        "Automatic (job:x)",
    ]


def test_setting_columns_writes_nothing_to_the_database(client, session, farm) -> None:  # noqa: F811
    def counts():
        tables = (AccessLog, Event, AppModule)
        rows = [session.scalar(select(func.count()).select_from(t)) for t in tables]
        return [*rows, session.get(Farm, farm.id).props]

    session.commit()
    before = counts()
    assert _show(client, "who", "device", "farm").status_code == 303
    session.expire_all()
    assert counts() == before


@pytest.mark.parametrize("tour", [False, True])
def test_native_columns_form_saves_explicit_farm_not_cookie_farm(
    client,
    session,
    farm,  # noqa: F811
    tour,
) -> None:
    other = Farm(name="Other test farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    session.commit()
    client.cookies.set("farmcore_farm", str(farm.id))
    client.cookies.set(COOKIE.format(farm.id), "time")
    if tour:
        client.cookies.set("farmcore_tour", "1")

    class ColumnsForm(HTMLParser):
        action = ""
        inside = False

        def __init__(self):
            super().__init__()
            self.controls = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.inside = urlsplit(attrs.get("action", "")).path == "/activity/columns"
                if self.inside:
                    self.action = attrs["action"]
            if tag != "input" or not self.inside or "disabled" in attrs:
                return
            if "name" not in attrs:
                return
            # Simulate enabling Who, retaining every other checked checkbox.
            if (
                attrs.get("type") == "checkbox"
                and "checked" not in attrs
                and attrs.get("value") != "who"
            ):
                return
            self.controls.append((attrs["name"], attrs.get("value", "")))

        def handle_endtag(self, tag):
            if tag == "form":
                self.inside = False

    page = client.get("/activity/", params={"farm": other.id})
    assert page.status_code == 200
    form = ColumnsForm()
    form.feed(page.text)
    assert form.action
    # Native GET replaces the action query; it does not append successful controls.
    native_url = urlsplit(form.action)._replace(query=urlencode(form.controls)).geturl()
    response = client.get(native_url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["set-cookie"].startswith(COOKIE.format(other.id) + "=")
    assert f"farm={other.id}" in response.headers["location"]
    assert client.cookies.get(COOKIE.format(farm.id)) == "time"
    assert "who" in client.cookies.get(COOKIE.format(other.id))
