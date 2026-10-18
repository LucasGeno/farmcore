from farmcore.core.manifest import Manifest
from tests.core.fake_apps import make_seed

seed = make_seed("b")

MANIFEST = Manifest(
    name="b",
    version="1.0.0",
    depends=("c",),
    seed="tests.core.fake_apps.b.manifest:seed",
)
