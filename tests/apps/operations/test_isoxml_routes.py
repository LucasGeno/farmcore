"""`/operations/` upload form and `POST /operations/isoxml` (HTMX partial, full-page fallback)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from farmcore.apps.operations import isoxml_demo
from farmcore.apps.operations.manifest import MANIFEST
from farmcore.core import clock
from farmcore.core.models.canonical import AccessLog, Log
from tests.apps.operations import isoxml_farm
from tests.apps.operations.isoxml_farm import CLOCK, local, voice_spray

farm = isoxml_farm.farm  # fixture
HTMX = {"HX-Request": "true"}


def _upload(client, data: bytes | None = None, *, headers=None, name="TASKDATA.XML"):
    data = isoxml_demo.demo_taskdata() if data is None else data
    return client.post(
        "/operations/isoxml",
        files={"file": (name, data, "application/xml")},
        headers=headers or {},
    )


def _ready(session, settings, farm, *, voice=True, at=CLOCK):
    log = voice_spray(session, farm) if voice else None
    clock.set_replay(session, local(at), settings)
    session.commit()
    return log


def test_manifest_declares_logs_write():
    assert "logs:write" in MANIFEST.scopes


def test_index_shows_the_upload_form(client, session, settings, farm):
    _ready(session, settings, farm, voice=False)

    html = client.get("/operations/").text

    form = re.search(r"<form[^>]*/operations/isoxml[^>]*>", html)
    assert form, html
    assert 'enctype="multipart/form-data"' in form.group(0)
    # htmx swaps no 403/500: the form writes a visible message into the result slot instead
    assert "hx-on::response-error" in form.group(0)
    assert "Import refused: admin token required" in form.group(0)
    assert "Import failed (HTTP " in form.group(0)
    assert re.search(r'<input[^>]*type="file"[^>]*accept="\.xml,\.zip"', html)


def test_the_error_handler_shows_the_servers_sentence_and_keeps_the_generic_fallback(
    client, session, settings, farm
):
    """htmx fires `response-error` on the form for a 400 and the handler replaced the server's
    sentence with "Import failed (HTTP 400)". It now reads the partial's error text (400 only,
    parsed and set as plain text, never the raw body, so no markup and no stack trace) and falls
    back to the generic line."""
    _ready(session, settings, farm, voice=False)

    html = client.get("/operations/").text

    handler = re.search(r'hx-on::response-error="([^"]*)"', html).group(1)
    assert "s === 400" in handler and "DOMParser" in handler
    assert "[data-isoxml=error]" in handler and "responseText" in handler
    assert "innerHTML" not in handler and "insertAdjacentHTML" not in handler
    assert "p.textContent = msg" in handler  # escaped by construction
    assert "'Import failed (HTTP ' + s + ')'" in handler  # the fallback stays
    assert "Import refused: admin token required" in handler


def test_the_file_picker_button_is_a_44px_target(client, session, settings, farm):
    _ready(session, settings, farm, voice=False)

    html = client.get("/operations/").text

    field = re.search(r'<input[^>]*type="file"[^>]*>', html).group(0)
    assert "file:min-h-[44px]" in field and "file:px-4" in field


def test_htmx_upload_says_merged_and_the_record_shows_both_badges(client, session, settings, farm):
    voice = _ready(session, settings, farm)

    resp = _upload(client, headers=HTMX)

    assert resp.status_code == 200
    assert "<html" not in resp.text  # a partial
    assert "merged into Spray 10 Apr 2024 (voice) (🎤 + 🚜), time and dose from the machine" in (
        resp.text
    )
    index = client.get("/operations/").text
    [record] = re.findall(
        r"<li[^>]*>(?:(?!</li>).)*Spray 10 Apr 2024 \(voice\).*?</li>", index, re.S
    )
    assert 'data-source="voice"' in record and 'data-source="isoxml"' in record
    timeline = client.get(f"/operations/parcels/{farm.parcels['P5'].id}/timeline").text
    merged = re.findall(r'<li[^>]*data-kind="log.merged".*?</li>', timeline, re.S)
    assert merged and all('data-source="isoxml"' in li for li in merged)
    assert all("Records merged" in li for li in merged)
    assert session.scalar(select(func.count()).select_from(Log)) == 1
    assert voice.id == session.scalar(select(Log.id))


def test_upload_without_a_voice_log_says_new_log(client, session, settings, farm):
    _ready(session, settings, farm, voice=False)

    resp = _upload(client, headers=HTMX)

    assert resp.status_code == 200
    assert resp.text.count("new log") == 2


def test_upload_without_htmx_renders_the_full_page(client, session, settings, farm):
    _ready(session, settings, farm)

    resp = _upload(client)

    assert resp.status_code == 200
    assert "<html" in resp.text and "time and dose from the machine" in resp.text


def test_register_after_the_merge_has_four_rows_at_machine_dose_and_time(
    client, session, settings, farm
):
    voice = _ready(session, settings, farm)
    assert _upload(client, headers=HTMX).status_code == 200

    rows = [r for r in client.get("/operations/register.json").json() if r["log_id"] == voice.id]

    assert sorted((r["parcel_alias"], r["active_substance"], r["dose"]) for r in rows) == [
        ("P5", "copper", "1.94"),
        ("P5", "sulphur", "2.06"),
        ("P6", "copper", "1.94"),
        ("P6", "sulphur", "2.06"),
    ]
    assert {(r["parcel_alias"], r["start_time"]) for r in rows} == {
        ("P5", "07:12"),
        ("P6", "08:03"),
    }


@pytest.mark.parametrize(
    ("at", "data", "message"),
    [
        ("2024-04-10 07:00", None, "step the clock past 08:03"),
        (
            CLOCK,
            isoxml_demo.demo_taskdata().replace(b'C="Peyrot"', b'C="Le Clos"'),
            "unknown partfield(s) Le Clos",
        ),
    ],
)
def test_a_late_task_or_unknown_partfield_is_a_400_with_the_plain_message(
    client, session, settings, farm, at, data, message
):
    _ready(session, settings, farm, at=at)

    resp = _upload(client, data, headers=HTMX)

    assert resp.status_code == 400
    assert message in resp.text
    assert session.scalar(select(func.count()).select_from(Log)) == 1  # the voice log only


def test_a_malformed_file_is_a_400(client, session, settings, farm):
    _ready(session, settings, farm)

    resp = _upload(client, b"<ISO11783_TaskData><TSK", headers=HTMX)

    assert resp.status_code == 400
    assert "not valid XML" in resp.text


def test_upload_writes_an_access_log_row_with_logs_write(client, session, settings, farm):
    _ready(session, settings, farm)

    assert _upload(client, headers=HTMX).status_code == 200

    session.expire_all()
    scopes = set(session.scalars(select(AccessLog.scope)))
    assert "logs:write" in scopes


def test_upload_needs_the_admin_token_when_set(session, settings, session_factory, farm):
    from farmcore.main import create_app

    _ready(session, settings, farm)
    locked = settings.model_copy(update={"admin_token": SecretStr("tok-test")})
    with TestClient(create_app(locked, session_factory=session_factory)) as c:
        assert _upload(c, headers=HTMX).status_code == 403
        assert session.scalar(select(func.count()).select_from(Log)) == 1
        ok = _upload(c, headers={**HTMX, "X-Admin-Token": "tok-test"})
        assert ok.status_code == 200, ok.text


def test_upload_form_is_shown_to_admins_only(session, settings, session_factory, farm):
    from farmcore.main import create_app

    _ready(session, settings, farm, voice=False)
    locked = settings.model_copy(update={"admin_token": SecretStr("tok-test")})
    with TestClient(create_app(locked, session_factory=session_factory)) as c:
        anonymous = c.get("/operations/").text
        c.cookies.set("admin_token", "tok-test")
        admin = c.get("/operations/").text
    assert "/operations/isoxml" not in anonymous
    assert "Machine records (ISOXML)" in anonymous  # the section explains; only the form is hidden
    assert re.search(r"<form[^>]*/operations/isoxml", admin)


def test_successful_htmx_upload_refreshes_recent_records_with_current_machine_values(
    client, session, settings, farm
):
    voice = _ready(session, settings, farm)
    page = client.get("/operations/").text
    assert "2 kg/ha" in re.sub(r"<[^>]+>", "", page)

    response = _upload(client, headers=HTMX)

    assert response.status_code == 200
    section = re.search(
        r'<section\b[^>]*id="recent-records"[^>]*>(.*?)</section>', response.text, re.S
    )
    assert section, "successful import must return the updated Recent records block"
    assert 'hx-swap-oob="outerHTML"' in section[0]
    assert f'data-record="{voice.id}"' in section[1]
    text = re.sub(r"<[^>]+>", "", section[1])
    assert f"{farm.products['copper'].name} · 1.94 kg/ha" in text
    assert f"{farm.products['sulphur'].name} · 2.06 kg/ha" in text
    assert 'data-source="voice"' in section[1]
    assert 'data-source="isoxml"' in section[1]
    assert 'id="recent-records"' in page  # the out-of-band swap has a target on the page
