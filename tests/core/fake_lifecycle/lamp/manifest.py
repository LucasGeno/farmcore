from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest

HERE = "tests.core.fake_lifecycle.lamp.app"

MANIFEST = Manifest(
    name="lamp",
    version="1.0.0",
    jobs=(JobSpec("lamp.sweep", f"{HERE}:sweep", timedelta(0)),),
    delivery_providers=(("note", f"{HERE}:provider"),),
)
