from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="assistant",
    version="0.1.0",
    title="Farm assistant",
    summary="Ask about recorded sprays, stored advice and forecast weather.",
    depends=("core",),
    scopes=("fields:read", "logs:read", "advice:read", "stations:read"),
    routes=("farmcore.apps.assistant.routes:router",),
    category="Your farm",
    icon="mic",
    color="brand",
    keywords={
        "en": ("assistant", "help", "question"),
        "fr": ("assistant", "aide", "question"),
        "es": ("asistente", "ayuda", "pregunta"),
        "it": ("assistente", "aiuto", "domanda"),
    },
)
