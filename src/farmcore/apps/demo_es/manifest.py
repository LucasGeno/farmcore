from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="demo_es",
    version="0.1.0",
    title="Spanish demo archive",
    summary="SIGPAC vineyard plots and the InfoRiego VA07 archive; no spray advice.",
    category="Data",
    depends=("fields", "stations", "operations"),
    scopes=("fields:read", "stations:read"),
    seed="farmcore.apps.demo_es.seed:seed",
    routes=("farmcore.apps.demo_es.routes:router",),
)
