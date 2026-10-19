"""Manifest of app `operations`: product shortlist, Scene B sprays, parcel timeline, register,
ISOXML TaskData import."""

from farmcore.apps.operations.tools import TOOLS
from farmcore.core.manifest import Manifest, NavItem

MANIFEST = Manifest(
    data_sources="farmcore.apps.operations.data_sources:summary",
    name="operations",
    version="0.1.0",
    summary="Every spray you log, and the register for inspections.",
    depends=("core", "fields"),
    # fields:read: parcel names; logs:write: the ISOXML TaskData import.
    scopes=("logs:read", "logs:write", "register:read", "fields:read", "corrections:read"),
    routes=(
        "farmcore.apps.operations.routes:router",
        "farmcore.apps.operations.map_sections:router",
    ),
    seed="farmcore.apps.operations.seed:seed",
    demo_presets=(
        "farmcore.apps.operations.demo:DEMO_START",
        "farmcore.apps.operations.demo:SEASON_END",
    ),
    title="Records",
    category="Essentials",
    required=True,
    nav=("Diary", "/operations/"),
    nav_group="more",
    nav_order=5,
    nav_icon="diary",
    extra_nav=(NavItem("Register", "/operations/register", "more", 0, "register"),),
    # No Today widget: recent sprays show in the core Recent activity card.
    # Map panel sections: latest spray for Farm today, spray facts per parcel.
    farm_sections=("/operations/map/farm",),
    parcel_sections=("/operations/map/parcels/{id}",),
    tools=TOOLS,
    maturity="alpha",
)
