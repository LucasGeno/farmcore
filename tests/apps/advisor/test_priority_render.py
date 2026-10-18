"""N2 rendering: the priority badge ('priority N of 3'), its why and the assumption note on the
Home attention card, Crop protection rows, the map panel, the bell and the map popup/label."""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from sqlalchemy import select

from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import Advice, Parcel
from farmcore.web import templates
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

BADGE = 'data-priority="2" role="img" aria-label="priority 2 of 3: High"'
ASSUME = "assumption, agronomist to confirm"


@pytest.mark.parametrize("level,label", [(1, "Due"), (2, "High"), (3, "Urgent")])
def test_priority_badge_has_three_bars_and_a_word(level, label):
    macro = templates.env.from_string(
        '{% from "core/_ui.html" import priority_badge %}{{ priority_badge(label, level) }}'
    )
    body = macro.render(level=level, label=label)
    assert f'aria-label="priority {level} of 3: {label}"' in body
    assert body.count("data-priority-bar=") == 3
    assert body.count('data-filled="true"') == level
    assert f">{label}</span>" in body


def test_no_priority_has_no_badge():
    macro = templates.env.from_string(
        '{% from "core/_ui.html" import priority_badge %}{{ priority_badge(none, none) }}'
    )
    assert macro.render().strip() == ""


def _stage(session, settings, step):
    from farmcore.apps.advisor.demo import apply_live_loop

    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")
    return farm


@pytest.mark.data
def test_home_attention_card_badges_why_and_assumption(
    session, settings, client, seeded, step, manifests
):
    seeded("B")
    farm = _stage(session, settings, step)
    body = html.unescape(
        client.get("/advisor/today", params={"view": "attention", "farm": farm.id}).text
    )
    assert body.count(BADGE) == 3 and ASSUME in body
    assert "priority 1 of 3" not in body
    full = html.unescape(client.get("/advisor/today", params={"farm": farm.id}).text)
    assert full.count(BADGE) == 3


@pytest.mark.data
def test_crop_protection_rows_badge_only_parcels_that_need_a_pass(
    session, settings, client, seeded, step, manifests
):
    seeded("B")
    farm = _stage(session, settings, step)
    body = html.unescape(client.get("/advisor/", params={"farm": farm.id}).text)
    assert body.count(BADGE) == 3 and "30 h" in body and ASSUME in body
    client.cookies.set("expert", "1")
    expert = html.unescape(client.get("/advisor/", params={"farm": farm.id}).text)
    expert_list = expert.split('id="all-advice"', 1)[1]
    assert expert_list.count(BADGE) == 3


@pytest.mark.data
def test_map_panel_and_bell_carry_the_priority(session, settings, client, seeded, step, manifests):
    seeded("B")
    farm = _stage(session, settings, step)
    p5 = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P5"))
    panel = html.unescape(client.get(f"/advisor/map/parcels/{p5.id}").text)
    assert BADGE in panel and "30 h" in panel and ASSUME in panel
    # The badge keeps its own width (a stretched full-width bar looked like a form field).
    fact = panel.split('data-fact="priority"', 1)[1].split(">", 1)[0]
    assert "items-start" in fact
    bell = html.unescape(client.get("/notifications", params={"farm": farm.id}).text)
    assert bell.count(BADGE) == 3 and "30 h" in bell and ASSUME in bell


def test_map_label_and_popup_render_the_layer_priority():
    js = (
        Path(__file__).resolve().parents[3] / "src/farmcore/apps/fields/static/map.js"
    ).read_text()
    assert "priority_label" in js and "priority_why" in js and "priority_assumption" in js
    assert " of 3" in js and "agronomist to confirm" in js


@pytest.mark.data
def test_urgent_advice_card_keeps_wait_status(session, settings, client, seeded, step, manifests):
    seeded("B")
    farm = _stage(session, settings, step)
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P5"))
    row = session.scalar(
        select(Advice).where(Advice.parcel_id == parcel.id).order_by(Advice.evaluated_at.desc())
    )
    row.decision = "wait"
    row.next_window_start = None
    row.next_window_end = None
    row.reasons = [
        *row.reasons,
        {
            "rule": "downy_incubation",
            "source": "Priority display scenario",
            "verdict": "incubating",
            "inputs": {"pct": 75},
            "threshold": {"incub_alert_pct": 50},
        },
    ]
    session.commit()
    body = html.unescape(client.get(f"/advisor/parcels/{parcel.id}").text)
    assert 'aria-label="priority 3 of 3: Urgent"' in body
    assert body.count('data-filled="true"') == 3
    assert 'id="decision-badge" data-decision="wait"' in body
    assert ASSUME in body
    panel = html.unescape(client.get(f"/advisor/map/parcels/{parcel.id}").text)
    assert 'aria-label="priority 3 of 3: Urgent"' in panel
    assert 'data-fact="decision" data-advised="yes"' in panel
    assert 'data-decision="wait"' in panel
