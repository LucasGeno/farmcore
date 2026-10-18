"""N9: a demo preset that leaves logged sprays must leave Sharing's exports and the Register
non-empty (CSV, JSON, NGSI-LD), through the existing preset scene functions."""

import json

import pytest

from farmcore.core import seed as core_seed
from farmcore.core.demo import collect_presets
from farmcore.core.manifest import import_string
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
pytestmark = pytest.mark.data


def _stage(session, settings, manifests, seeded, step, key):
    preset = next(p for p in collect_presets(manifests) if p.key == key)
    seeded("B")
    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    import_string(preset.scene)(session, farm, preset.clock_target)
    session.commit()
    step(f"{preset.clock_target:%Y-%m-%d %H:%M}")


@pytest.mark.parametrize("key", ["season-end", "live-loop"])
def test_presets_with_sprays_leave_register_csv_json_and_ngsi_non_empty(
    client, session, settings, manifests, seeded, step, key
):
    _stage(session, settings, manifests, seeded, step, key)

    csv = client.get("/operations/register.csv")
    assert csv.status_code == 200
    lines = [line for line in csv.text.splitlines() if line.strip()]
    assert len(lines) > 1, "register CSV has a header only"
    assert "Demo data" in csv.text or "copper" in csv.text.lower()

    rows = client.get("/operations/register.json").json()
    assert len(rows) >= 1

    ngsi = client.get("/share/ngsi-ld.json")
    assert ngsi.status_code == 200
    entities = ngsi.json()
    entities = entities if isinstance(entities, list) else entities.get("@graph", [entities])
    operations = [e for e in entities if "Operation" in json.dumps(e.get("type", ""))]
    assert operations, (
        f"NGSI-LD has no operation entities: {sorted({str(e.get('type')) for e in entities})}"
    )

    page = client.get("/share/")
    assert page.status_code == 200 and "/operations/register.csv" in page.text
