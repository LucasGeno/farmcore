"""Demo presets use existing Scene B records and the existing product shortlist."""

from datetime import datetime
from zoneinfo import ZoneInfo

from farmcore.core.demo import Preset

DEMO_START = Preset(
    key="demo-start",
    label="Demo start · 8 Apr 06:00",
    description="Demo data: start before the infection event, with no parcels needing a spray.",
    clock_target=datetime(2024, 4, 8, 6, tzinfo=ZoneInfo("Europe/Paris")),
    scene="farmcore.apps.operations.demo:apply_scene_b",
)
SEASON_END = Preset(
    key="season-end",
    label="Season end · 15 May 06:00",
    description="Demo data: replay the later season with the existing Scene B copper sprays.",
    clock_target=datetime(2024, 5, 15, 6, tzinfo=ZoneInfo("Europe/Paris")),
    scene="farmcore.apps.operations.demo:apply_scene_b",
)


def apply_scene_b(session, farm, now) -> None:
    """Recreate the existing seeded demo sprays without touching farm identity/bindings."""
    from sqlalchemy import select

    from farmcore.apps.operations import seed
    from farmcore.core import ext_ids
    from farmcore.core.models.canonical import Log, LogSource, Product

    copper = ext_ids.lookup(session, seed.PRODUCT_SOURCE, seed.SCENE_B_PRODUCT)
    if not isinstance(copper, Product):
        raise ValueError("Seed the demo product catalogue before staging Scene B")
    seed._scene_b(session, farm.id, copper, "Europe/Paris")
    rows = session.scalars(
        select(Log).join(LogSource).where(Log.farm_id == farm.id, LogSource.source_type == "seed")
    ).unique()
    for log in rows:
        if not log.name.startswith("Demo data"):
            log.name = f"Demo data · {log.name}"
        log.notes = "Demo data · Scene B."
        log.props = {**(log.props or {}), "demo_data": True}
