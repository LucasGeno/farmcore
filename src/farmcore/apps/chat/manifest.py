"""Manifest of app `chat`: the chat handlers and the advice push job."""

from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest

MANIFEST = Manifest(
    data_sources="farmcore.apps.chat.data_sources:summary",
    name="chat",
    demo_reset=("farmcore.apps.chat.conversation:reset",),
    version="0.1.0",
    summary="Log a spray by voice or text in Telegram and get the advice there.",
    depends=("fields", "operations", "advisor"),
    scopes=("logs:write",),
    # Runs after `advisor.evaluate` in the same replay step (chat loads after advisor).
    jobs=(
        JobSpec(
            name="chat.push_advice",
            func="farmcore.apps.chat.push:push_advice",
            interval=timedelta(hours=1),
        ),
    ),
    delivery_providers=(("telegram.message", "farmcore.apps.chat.push:delivery_provider"),),
    routes=("farmcore.apps.chat.routes:router",),
    title="Telegram assistant",
    category="Voice & chat",
    nav=("AI log", "/chat/"),
    nav_group="more",
    nav_order=70,
    nav_icon="chat",
    icon="chat",
    color="spray",
    keywords={
        "en": ("chat", "telegram", "voice", "assistant"),
        "fr": ("discussion", "telegram", "assistant"),
        "es": ("chat", "telegram", "asistente"),
        "it": ("chat", "telegram", "assistente"),
    },
    maturity="alpha",
)
