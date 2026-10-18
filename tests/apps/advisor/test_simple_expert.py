"""N4: Expert off keeps Home and Crop protection to what a farmer needs; Expert on adds the model
internals. The priority why and the assumption labels stay in both modes."""

from __future__ import annotations

import html

import pytest

from farmcore.core import seed as core_seed
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
pytestmark = pytest.mark.data

ASSUME = "assumption, agronomist to confirm"


def _stage(session, settings, step):
    from farmcore.apps.advisor.demo import apply_live_loop

    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")
    return farm


def test_home_without_expert_hides_the_data_sources_slot(
    session, settings, client, seeded, step, manifests
):
    seeded("B")
    farm = _stage(session, settings, step)
    simple = client.get("/today/", params={"farm": farm.id}).text
    assert "/advisor/today?view=attention" in simple and "/forecast/today" in simple
    assert "/fields/today" in simple and "/activity/today" in simple
    assert "/data-sources/today" not in simple and ">Data sources</summary>" not in simple
    client.cookies.set("expert", "1")
    assert "/data-sources/today" in client.get("/today/", params={"farm": farm.id}).text


def test_crop_protection_without_expert_has_status_priority_action_and_products_only(
    session, settings, client, seeded, step, manifests
):
    seeded("B")
    farm = _stage(session, settings, step)
    simple = html.unescape(client.get("/advisor/", params={"farm": farm.id}).text)
    assert simple.count('aria-label="priority 2 of 3: High"') == 3
    assert "copper" in simple and "30 h" in simple and ASSUME in simple
    assert "next window" in simple or "spray window open" in simple
    assert "<details" not in simple.split('id="by-decision"', 1)[1].split("</section>", 1)[0]
    assert "All advice (expert)" not in simple and "Downy mildew: risk" not in simple
    client.cookies.set("expert", "1")
    full = html.unescape(client.get("/advisor/", params={"farm": farm.id}).text)
    assert "All advice (expert)" in full and "Downy mildew: risk" in full
    assert full.count('aria-label="priority 2 of 3: High"') >= 3 and ASSUME in full


@pytest.mark.parametrize("expert", [False, True])
def test_demo_path_attention_card_works_in_both_modes(
    session, settings, client, seeded, step, manifests, expert
):
    seeded("B")
    farm = _stage(session, settings, step)
    if expert:
        client.cookies.set("expert", "1")
    body = html.unescape(
        client.get("/advisor/today", params={"view": "attention", "farm": farm.id}).text
    )
    for name in ("Les Granges", "Peyrot", "La Fontaine"):
        assert name in body
    assert "7 parcels protected" in body and ASSUME in body


def test_more_sheet_puts_the_expert_toggle_first(client):
    page = client.get("/today/").text
    sheet = page.split('id="more-menu"', 1)[1]
    assert sheet.index("/prefs/expert") < sheet.index('<div class="md:hidden">')
