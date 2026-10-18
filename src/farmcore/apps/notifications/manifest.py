from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="notifications",
    version="0.1.0",
    title="Notifications",
    summary="Current farm advice and data issues, with browser-local read preferences.",
    depends=("core",),
    scopes=("advice:read", "fields:read", "stations:read"),
    routes=("farmcore.apps.notifications.routes:router",),
    category="Your farm",
)
