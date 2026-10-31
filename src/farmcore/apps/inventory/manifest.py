"""Manifest of app `inventory`: the selected farm's product stock, with this season's usage
computed from the logged sprays (no stock-movement ledger, no second entry).

Optional (switch it off in All apps: its nav entry goes at once). No widget, job or tool.
"""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="inventory",
    version="0.1.0",
    summary="What is left in the store: stock bought, minus what your logged sprays used.",
    depends=("fields", "operations"),
    # logs:read: the sprays that use stock; fields:read: parcel areas for per-hectare doses.
    scopes=("logs:read", "fields:read"),
    resource_scopes=(("inventory_stock", ("logs:read",)),),
    models=("farmcore.apps.inventory.models",),
    routes=("farmcore.apps.inventory.routes:router",),
    seed="farmcore.apps.inventory.seed:seed",
    demo_reset=("farmcore.apps.inventory.demo_purchases:revert",),
    title="Inventory",
    category="Essentials",
    nav=("Inventory", "/inventory/"),
    nav_group="apps",
    nav_order=25,
    nav_icon="leaf",
    maturity="alpha",
)
