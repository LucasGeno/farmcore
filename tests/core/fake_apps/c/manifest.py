from farmcore.core.manifest import Manifest
from tests.core.fake_apps import make_seed

seed = make_seed("c")

MANIFEST = Manifest(
    name="c",
    version="1.0.0",
    models=("tests.core.fake_apps.c.models",),
    seed="tests.core.fake_apps.c.manifest:seed",
    maturity="stable",
)
