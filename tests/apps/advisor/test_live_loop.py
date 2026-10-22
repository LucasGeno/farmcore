"""The live-loop scene uses real station weather and existing demo copper doses."""

import re

import pytest
from sqlalchemy import select

from farmcore.core import seed as core_seed
from farmcore.core.models.canonical import Log, LogSource
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
pytestmark = pytest.mark.data


def test_live_loop_scene_yields_three_uncovered_and_seven_protected(
    session, settings, client, seeded, step, manifests
):
    from farmcore.core.demo import collect_presets
    from farmcore.core.manifest import import_string

    preset = next(p for p in collect_presets(manifests) if p.key == "live-loop")
    apply_live_loop = import_string(preset.scene)

    seeded("B")
    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, preset.clock_target)
    session.commit()
    step("2024-04-10 06:00")

    features = client.get("/fields/parcels.geojson").json()["features"]
    decisions = {f["properties"]["alias"]: f["properties"]["decision"] for f in features}
    assert decisions == {f"P{n}": "spray" if n in (5, 6, 7) else "protected" for n in range(1, 11)}
    for feature in features:
        parcel = feature["properties"]
        advice = client.get(f"/advisor/parcels/{parcel['id']}.json").json()
        reasons = {r["rule"]: r for r in advice["reasons"]}
        assert reasons["downy_3_10"]["inputs"]["event_r48_mm"] == 10.3
        assert reasons["downy_3_10"]["inputs"]["event_t48_c"] == pytest.approx(15.4145833333)
        if parcel["alias"] in ("P5", "P6", "P7"):
            assert reasons["downy_3_10"]["verdict"] == "risk"
            assert reasons["protection_downy"]["verdict"] == "no cover"
        else:
            assert reasons["protection_downy"]["inputs"]["mm_since_spray"] == 1.2
    demo_logs = session.scalars(select(Log).where(Log.ts_start == local("2024-04-09 08:00"))).all()
    assert len(demo_logs) == 7
    assert all("demo data" in log.name.lower() for log in demo_logs)
    sources = session.scalars(
        select(LogSource).where(LogSource.log_id.in_([log.id for log in demo_logs]))
    )
    assert all(
        source.source_type == "seed" and source.raw_payload["demo_data"] for source in sources
    )


def test_scene_is_idempotent_and_never_adds_french_sprays_to_another_farm(
    session, settings, seeded
):
    from farmcore.apps.advisor.demo import apply_live_loop
    from farmcore.core.models.canonical import Farm, Parcel

    seeded()
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    before = list(session.scalars(select(Log.id).order_by(Log.id)))
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    assert list(session.scalars(select(Log.id).order_by(Log.id))) == before

    other = Farm(name="Other country demo farm")
    session.add(other)
    session.flush()
    original = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P1"))
    session.add(
        Parcel(
            farm_id=other.id,
            alias="P1",
            name="Other parcel",
            area_ha=original.area_ha,
            geom=original.geom,
        )
    )
    session.flush()
    apply_live_loop(session, other, local("2024-04-10 06:00"))
    assert list(session.scalars(select(Log).where(Log.farm_id == other.id))) == []


def test_live_loop_real_push_and_voice_confirm_protect_only_les_granges(
    session, settings, client, session_factory, manifests, seeded, monkeypatch
):
    import asyncio

    from farmcore.apps.advisor.demo import apply_live_loop
    from farmcore.apps.chat import push
    from tests.e2e.test_hero_loop import Demo, voice_update

    seeded("B")
    phrase = "Sprayed Les Granges at 6 this morning with Bouillie bordelaise, 2 kilos per hectare"
    demo = Demo(client, session, session_factory, manifests, settings, phrase)
    monkeypatch.setattr(push, "_sender", demo.digests)
    demo.start()
    demo.step("2024-04-08 06:00")
    assert demo.digests.sent == []
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    session.commit()
    demo.step("2024-04-10 06:00")
    [digest] = demo.digests.sent
    assert digest.text.splitlines()[0] == "3 parcels → spray, 7 parcels → protected"
    named = [line.split(":", 1)[0] for line in digest.text.splitlines() if line.startswith("P")]
    assert named == ["P5 Les Granges", "P6 Peyrot", "P7 La Fontaine"]
    demo.step("2024-04-10 06:00")
    assert len(demo.digests.sent) == 1

    asyncio.run(demo.runtime.on_voice(voice_update(), demo.context))
    echo = demo.replies.sent[-1]
    assert "P5 Les Granges · copper · 2 kg/ha · 10 Apr 06:00 — confirm?" in echo.text
    confirm = next(data for label, data in echo.buttons if "Confirm" in label)
    assert "Advice updated." in demo.tap(confirm)
    assert demo.map_decisions() == {
        f"P{n}": "spray" if n in (6, 7) else "protected" for n in range(1, 11)
    }
    assert "already" in demo.tap(confirm).lower()
    assert len(demo.digests.sent) == 1


def test_today_poll_preserves_farm_and_shows_confirmation(session, settings, client, seeded, step):
    from farmcore.apps.advisor.demo import apply_live_loop
    from tests.apps.advisor.scenes import confirm_spray

    seeded()
    farm = core_seed.ensure_default_farm(session, settings)
    now = local("2024-04-10 06:00")
    apply_live_loop(session, farm, now)
    session.commit()
    step("2024-04-10 06:00")
    path = f"/advisor/today?farm={farm.id}"
    initial = client.get(path).text
    assert "Spray 3 parcels today" in " ".join(re.sub("<[^>]*>", " ", initial).split())
    assert "data-poll-feed" in initial
    assert f'hx-get="{path}"' in initial
    assert "every 12s [!document.hidden]" in initial
    confirm_spray(session, settings, "P5", now, "copper-contact")
    from farmcore.core import jobs

    jobs.run_now(
        client.app.state.session_factory, client.app.state.manifests, "advisor.evaluate", settings
    )
    refreshed = client.get(path, headers={"HX-Request": "true"}).text
    assert "<html" not in refreshed
    assert "Spray 2 parcels today" in " ".join(re.sub("<[^>]*>", " ", refreshed).split())
    assert 'hx-swap="outerHTML"' in refreshed


@pytest.mark.parametrize(
    "section",
    [
        "/advisor/map/farm",
        "/advisor/map/parcels/{id}",
        "/operations/map/farm",
        "/operations/map/parcels/{id}",
    ],
)
def test_panel_poll_keeps_farm_and_reflects_a_new_confirmation(
    session, session_factory, settings, client, seeded, step, manifests, section
):
    from farmcore.apps.advisor.demo import apply_live_loop
    from farmcore.core import jobs
    from farmcore.core.models.canonical import Parcel
    from tests.apps.advisor.scenes import confirm_spray

    seeded()
    farm = core_seed.ensure_default_farm(session, settings)
    now = local("2024-04-10 06:00")
    apply_live_loop(session, farm, now)
    session.commit()
    step("2024-04-10 06:00")
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P5"))
    path = section.format(id=parcel.id) + f"?farm={farm.id}"
    initial = client.get(path).text
    assert "data-poll-feed" in initial
    assert f'hx-get="{path}"' in initial
    confirm_spray(session, settings, "P5", now, "copper-contact")
    jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)
    refreshed = client.get(path, headers={"HX-Request": "true"}).text
    plain = " ".join(re.sub("<[^>]*>", " ", refreshed).split())
    assert "<html" not in refreshed
    assert 'hx-swap="outerHTML"' in refreshed
    if section == "/advisor/map/farm":
        assert "Spray 2" in plain and "Protected 8" in plain
    elif section.startswith("/advisor/"):
        assert "Protected" in plain and "Covered until" in plain
    else:
        assert "10 Apr 2024" in plain and "BOUILLIE BORDELAISE" in plain


def test_map_list_poll_updates_only_the_confirmed_parcel_and_keeps_farm(
    session, session_factory, settings, client, seeded, step, manifests
):
    from farmcore.apps.advisor.demo import apply_live_loop
    from farmcore.core import jobs
    from tests.apps.advisor.scenes import confirm_spray

    seeded()
    farm = core_seed.ensure_default_farm(session, settings)
    now = local("2024-04-10 06:00")
    apply_live_loop(session, farm, now)
    session.commit()
    step("2024-04-10 06:00")
    path = f"/fields/list?farm={farm.id}"
    initial = client.get(path)
    assert initial.status_code == 200
    assert "<html" not in initial.text
    assert f'hx-get="{path}"' in initial.text
    assert "data-poll-feed" in initial.text
    assert initial.text.count('data-icon="spray"') == 3
    assert initial.text.count('data-icon="protected"') == 7
    confirm_spray(session, settings, "P5", now, "copper-contact")
    jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)
    refreshed = client.get(path, headers={"HX-Request": "true"}).text
    assert refreshed.count('data-icon="spray"') == 2
    assert refreshed.count('data-icon="protected"') == 8
    assert "?parcel=" in refreshed and f"farm={farm.id}" in refreshed


def test_advisor_registers_live_loop_preset_through_the_shared_contract():
    from farmcore.apps.advisor.manifest import MANIFEST
    from farmcore.core.demo import collect_presets
    from farmcore.core.manifest import import_string

    presets = collect_presets([MANIFEST])
    assert [preset.key for preset in presets] == ["live-loop", "season-2024"]
    preset = presets[0]
    assert preset.label == "Live loop: 3 parcels need treatment"
    assert "demo data" in preset.description
    assert preset.clock_target == local("2024-04-10 06:00")
    assert preset.clock_target.tzinfo.key == "Europe/Paris"
    assert callable(import_string(preset.scene))


def test_demo_http_reset_stages_live_loop_and_preserves_the_linked_chat(
    session, session_factory, settings, client, seeded, manifests, monkeypatch
):
    import asyncio

    from farmcore.apps.chat import push
    from tests.e2e.test_hero_loop import Demo, voice_update

    seeded("B")
    phrase = "Sprayed Les Granges at 6 this morning with Bouillie bordelaise, 2 kilos per hectare"
    demo = Demo(client, session, session_factory, manifests, settings, phrase)
    monkeypatch.setattr(push, "_sender", demo.digests)
    monkeypatch.setattr(client.app.state, "settings", demo.settings)
    demo.start()
    demo.step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    session.refresh(farm)
    chats = farm.props["chat"]["telegram_chat_ids"]
    for attempt in range(2):
        page = client.get("/demo/")
        assert page.status_code == 200 and "Bot linked: yes" in page.text
        generation = re.search(r'name="generation" value="(\d+)"', page.text)[1]
        form = {
            "action": "reset",
            "preset": "live-loop",
            "farm_id": farm.id,
            "generation": generation,
        }
        preview = client.post("/demo/preview", data=form, headers={"Origin": "http://testserver"})
        assert preview.status_code == 200 and "Confirm reset" in preview.text
        result = client.post(
            "/demo/apply", data={**form, "confirm": "yes"}, headers={"Origin": "http://testserver"}
        )
        assert result.status_code == 200 and "Bot linked: yes" in result.text
        session.refresh(farm)
        assert farm.props["chat"]["telegram_chat_ids"] == chats
        assert len(demo.digests.sent) == attempt + 1
        assert demo.digests.sent[-1].text.startswith("3 parcels → spray, 7 parcels → protected")
        assert demo.map_decisions() == {
            f"P{n}": "spray" if n in (5, 6, 7) else "protected" for n in range(1, 11)
        }
    asyncio.run(demo.runtime.on_voice(voice_update(), demo.context))
    confirm = next(data for label, data in demo.replies.sent[-1].buttons if "Confirm" in label)
    assert "Advice updated." in demo.tap(confirm)
    assert demo.map_decisions() == {
        f"P{n}": "spray" if n in (6, 7) else "protected" for n in range(1, 11)
    }


def test_live_loop_priority_contract_in_json_layer_and_presenter(
    session, settings, client, seeded, step, manifests
):
    from farmcore.apps.advisor import service
    from farmcore.apps.advisor.demo import apply_live_loop
    from farmcore.apps.advisor.presenter import parcel_row
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.models.canonical import Parcel

    seeded("B")
    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")

    keys = ("priority", "priority_label", "priority_why", "priority_assumption")
    layer = client.get("/advisor/layer.geojson").json()["features"]
    by_alias = {f["properties"]["alias"]: f["properties"] for f in layer}
    for alias, props in by_alias.items():
        advice = client.get(f"/advisor/parcels/{props['id']}.json").json()
        assert {k: advice[k] for k in keys} == {k: props[k] for k in keys}
        if alias in ("P5", "P6", "P7"):
            assert (props["priority"], props["priority_label"]) == (2, "High")
            assert props["priority_assumption"] is True
            assert "30 h" in props["priority_why"]
        else:
            assert props["priority"] is None and props["priority_label"] is None

    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")
    latest = service.latest_advice(repo, local("2024-04-10 06:00"), tz=settings.display_tz)
    p5 = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P5"))
    row = parcel_row(p5, latest[p5.id], settings.display_tz)
    assert (row.priority, row.priority_label, row.priority_assumption) == (2, "High", True)
    assert row.priority_why == by_alias["P5"]["priority_why"]
