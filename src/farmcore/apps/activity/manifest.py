"""Manifest of app `activity`: the Activity page (no models, no jobs, no seed)."""

from farmcore.core.manifest import Manifest, ParcelView

MANIFEST = Manifest(
    name="activity",
    version="0.1.0",
    summary="Everything logged on the farm, newest first.",
    depends=("core",),
    scopes=("logs:read", "fields:read"),
    routes=("farmcore.apps.activity.routes:router",),
    title="Activity",
    category="Records",
    nav=("Activity", "/activity/"),
    nav_group="farmer",
    nav_order=40,
    nav_icon="history",
    maturity="alpha",
    parcel_views=(
        ParcelView(
            "activity",
            "Activity",
            "/activity/parcels/{id}/workspace",
            "/activity/?parcel_id={id}",
            30,
            ("type", "from", "to", "before", "before_id"),
        ),
    ),
)
