"""`/inventory/`: the stock list of the selected farm, the low-stock form and the app switch."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.apps.inventory.manifest import MANIFEST
from farmcore.apps.inventory.models import InventoryStock
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.main import create_app
from tests.apps.inventory.conftest import T_APR, at, spray, stock


def _card(page: str, product_id: int) -> str:
    """The product's stock card as plain text (tags dropped, whitespace collapsed)."""
    html = page.split(f'data-stock="{product_id}"', 1)[1].split("</article>", 1)[0]
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_manifest_puts_inventory_in_the_apps_group_and_it_can_be_switched_off():
    assert MANIFEST.name == "inventory"
    assert MANIFEST.nav == ("Inventory", "/inventory/")
    assert MANIFEST.nav_group == "apps"
    assert MANIFEST.required is False
    assert MANIFEST.models == ("farmcore.apps.inventory.models",)


def test_page_lists_stock_with_on_hand_unit_lot_and_last_purchase(client, session, settings, inv):
    copper = inv.products["copper-contact"]
    stock(
        session, inv.farm, copper, purchased="20", unit="kg", lot="LOT-CU-1",
        last_purchase_on=date(2024, 3, 15), low_stock=Decimal("5"), props={"demo_data": True},
    )  # fmt: skip
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="kg/ha", key="a")
    at(session, settings, T_APR + timedelta(hours=2))

    row = _card(client.get("/inventory/").text, copper.id)
    assert copper.name in row
    assert "16 kg on hand" in row  # 20 bought − 4 used
    assert "Bought this season 20 kg" in row
    assert "Used (from 1 logged spray) 4 kg" in row
    assert "LOT-CU-1" in row
    assert "15 Mar 2024" in row
    assert "Demo data" in row
    assert "Low stock" not in row


def test_incompatible_units_say_not_computable_instead_of_a_number(client, session, settings, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20", unit="kg")
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="L/ha", key="l")
    at(session, settings, T_APR + timedelta(hours=2))

    row = _card(client.get("/inventory/").text, copper.id)

    assert "Not computable on hand" in row
    assert "does not convert to kg" in row
    assert "16 kg" not in row


def test_low_stock_row_is_flagged(client, session, settings, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="3", low_stock=Decimal("2"))
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="1", unit="kg/ha", key="a")
    at(session, settings, T_APR + timedelta(hours=2))

    assert "Low stock" in _card(client.get("/inventory/").text, copper.id)


def test_farm_without_a_verified_catalogue_gets_an_honest_empty_state(client, session, inv):
    es = Farm(name="Spanish demo", props={"country": "ES", "product_ids": []})
    session.add(es)
    session.flush()
    ensure_owner_grant(session, es)
    session.commit()

    page = client.get(f"/inventory/?farm={es.id}").text

    assert "data-stock=" not in page
    assert "no verified product catalogue" in page


def test_admin_sets_the_low_stock_threshold(client, session, settings, inv):
    copper = inv.products["copper-contact"]
    row = stock(session, inv.farm, copper, purchased="20")
    at(session, settings, T_APR)

    r = client.post(f"/inventory/{row.id}/low-stock", data={"low_stock": "7.5"},
                    follow_redirects=False)  # fmt: skip

    assert r.status_code == 303
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock == Decimal("7.5")
    client.post(f"/inventory/{row.id}/low-stock", data={"low_stock": ""})
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock is None


def test_low_stock_rejects_negative_or_non_numbers(client, session, settings, inv):
    row = stock(session, inv.farm, inv.products["copper-contact"], purchased="20")
    session.commit()

    url = f"/inventory/{row.id}/low-stock"
    assert client.post(url, data={"low_stock": "-1"}).status_code == 422
    assert client.post(url, data={"low_stock": "x"}).status_code == 422


def test_browser_low_stock_error_keeps_the_typed_value_and_the_saved_threshold(
    client, session, inv
):
    row = stock(session, inv.farm, inv.products["copper-contact"], purchased="20")
    row.low_stock = Decimal("5")
    session.commit()

    r = client.post(
        f"/inventory/{row.id}/low-stock", data={"low_stock": "-3"}, headers={"Accept": "text/html"}
    )

    assert r.status_code == 422
    assert 'name="low_stock" inputmode="decimal" value="-3"' in r.text
    assert f'aria-describedby="low-stock-{row.id}-error"' in r.text
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock == Decimal("5")


def test_low_stock_rejects_huge_tiny_or_too_precise_values_with_422(client, session, inv):
    row = stock(session, inv.farm, inv.products["copper-contact"], purchased="20")
    session.commit()

    url = f"/inventory/{row.id}/low-stock"
    for raw in ("1e999999", "1e131073", "9" * 200_000, "1e-9999999", "1000000.001", "0.0001"):
        assert client.post(url, data={"low_stock": raw}).status_code == 422, raw[:20]
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock is None


def test_low_stock_accepts_the_bounds_with_three_decimals(client, session, inv):
    row = stock(session, inv.farm, inv.products["copper-contact"], purchased="20")
    session.commit()

    url = f"/inventory/{row.id}/low-stock"
    assert client.post(url, data={"low_stock": "1e6"}, follow_redirects=False).status_code == 303
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock == Decimal("1000000")
    assert client.post(url, data={"low_stock": "0,125"}, follow_redirects=False).status_code == 303
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock == Decimal("0.125")
    assert "1000000" not in _card(client.get("/inventory/").text, inv.products["copper-contact"].id)


def test_low_stock_of_another_farms_row_is_404(client, session, inv):
    other = Farm(name="Other farm")
    session.add(other)
    session.flush()
    row = stock(session, other, inv.products["copper-contact"], purchased="20")
    session.commit()

    r = client.post(f"/inventory/{row.id}/low-stock", data={"low_stock": "1"})

    assert r.status_code == 404


def test_low_stock_needs_the_admin_token_and_is_blocked_for_tour_visitors(
    settings, session_factory, session, inv
):
    row = stock(session, inv.farm, inv.products["copper-contact"], purchased="20")
    session.commit()
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr("t0ken")}),
        session_factory=session_factory,
    )
    with TestClient(app) as c:
        assert 'action="/inventory/' not in c.get("/inventory/").text
        r = c.post(f"/inventory/{row.id}/low-stock", data={"low_stock": "1"})
        assert r.status_code == 403
        c.cookies.set("farmcore_tour", "1")
        r = c.post(
            f"/inventory/{row.id}/low-stock",
            data={"low_stock": "1"},
            headers={"X-Admin-Token": "t0ken"},
        )
        assert r.status_code >= 400
    session.expire_all()
    assert session.get(InventoryStock, row.id).low_stock is None


def test_switching_the_app_off_removes_its_nav_entry(client, inv):
    assert 'href="/inventory/"' in client.get("/today/").text
    assert client.post("/apps/inventory/uninstall").status_code == 200

    page = client.get("/today/").text

    assert 'href="/inventory/"' not in page
    assert 'hx-get="/inventory/' not in page
