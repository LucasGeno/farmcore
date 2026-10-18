from farmcore.core.manifest import Manifest
from tests.core.fake_apps import make_seed

seed = make_seed("a")

MANIFEST = Manifest(
    name="a",
    version="1.0.0",
    depends=("b",),
    routes=("tests.core.fake_apps.a.routes:router",),
    seed="tests.core.fake_apps.a.manifest:seed",
    nav=("A", "/a/ping"),
    maturity="beta",
)
