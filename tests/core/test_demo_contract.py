"""The shared demo-preset contract; no application or database side effects."""

from datetime import datetime
from zoneinfo import ZoneInfo

from farmcore.core.demo import Preset, collect_presets
from farmcore.core.manifest import Manifest

DEMO_START = Preset(
    key="demo-start",
    label="Demo start",
    description="Demo data at the start of the demo.",
    clock_target=datetime(2024, 4, 8, 6, tzinfo=ZoneInfo("Europe/Paris")),
    scene=None,
)


def test_collects_registered_presets_in_manifest_order():
    manifest = Manifest(name="demo-fixture", version="1", demo_presets=(f"{__name__}:DEMO_START",))
    assert collect_presets([Manifest(name="empty", version="1"), manifest]) == [DEMO_START]


def test_rejects_duplicate_keys_and_non_presets():
    import pytest

    duplicate = Manifest(
        name="duplicate", version="1", demo_presets=(f"{__name__}:DEMO_START",) * 2
    )
    with pytest.raises(ValueError, match="duplicate"):
        collect_presets([duplicate])
    wrong = Manifest(name="wrong", version="1", demo_presets=(f"{__name__}:datetime",))
    with pytest.raises(TypeError, match="Preset"):
        collect_presets([wrong])


def test_preset_requires_paris_aware_time_and_is_frozen():
    from dataclasses import FrozenInstanceError, replace
    from datetime import UTC

    import pytest

    for target in (datetime(2024, 4, 8, 6), datetime(2024, 4, 8, 4, tzinfo=UTC)):
        with pytest.raises(ValueError, match="Europe/Paris"):
            replace(DEMO_START, clock_target=target)
    with pytest.raises(FrozenInstanceError):
        DEMO_START.label = "changed"
