"""Manifest of app `share`: grants, revoke, grantee view, access log, NGSI-LD (no seed, no jobs)."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="share",
    version="0.1.0",
    summary="Let your adviser or co-op read your records. Stop it any time.",
    depends=("fields", "operations"),
    scopes=("logs:read", "register:read", "audit:read"),
    routes=("farmcore.apps.share.routes:router",),
    title="Sharing",
    category="Data sharing",
    nav=("Sharing", "/share/"),
    nav_group="apps",
    nav_order=40,
    nav_icon="share",
    icon="share",
    color="protected",
    keywords={
        "en": ("share", "sharing", "grant", "access"),
        "fr": ("partage", "accès", "autorisation"),
        "es": ("compartir", "acceso"),
        "it": ("condivisione", "accesso"),
    },
    maturity="alpha",
)
