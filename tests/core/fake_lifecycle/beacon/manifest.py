from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest
from farmcore.core.tools import ToolSpec, object_schema

HERE = "tests.core.fake_lifecycle.beacon.app"

MANIFEST = Manifest(
    name="beacon",
    version="1.0.0",
    routes=(f"{HERE}:router",),
    jobs=(
        JobSpec("beacon.ping", f"{HERE}:plan", timedelta(0), task=f"{HERE}:ping"),
        JobSpec("beacon.sweep", f"{HERE}:sweep", timedelta(0)),
    ),
    tools=(
        ToolSpec(
            name="beacon_status",
            description="Beacon status",
            params=object_schema(),
            func=f"{HERE}:status",
            scope="fields:read",
            effect="read",
        ),
    ),
)
