"""The optional manifest fields of the UX pass default to empty, and every real app loads."""

from __future__ import annotations

import dataclasses

import pytest

from farmcore.core import registry
from farmcore.core.manifest import Manifest, MapLayer, NavItem, Widget


def test_bare_manifest_has_every_new_field_empty() -> None:
    m = Manifest(name="x", version="0.1.0")
    assert m.nav_group == "more"
    assert m.nav_order == 100
    assert m.extra_nav == ()
    assert m.widgets == ()
    assert m.map_layers == ()
    assert (m.title, m.category, m.required) == ("", "", False)
    assert m.display_name == "X"
    assert m.parcel_sections == ()
    assert m.farm_sections == ()
    assert m.debug_panels == ()
    assert m.tools == ()


def test_nav_item_defaults_to_the_more_group() -> None:
    item = NavItem("Stations", "/stations/")
    assert (item.group, item.order, item.icon) == ("more", 100, "")


@pytest.mark.parametrize("group", ["farmer", "apps", "more", "admin"])
def test_nav_group_accepts_the_four_groups(group: str) -> None:
    assert Manifest(name="x", version="0.1.0", nav_group=group).nav_group == group  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["expert", "", "Farmer"])
def test_nav_group_rejects_anything_else(bad: str) -> None:
    with pytest.raises(ValueError, match="nav_group"):
        Manifest(name="x", version="0.1.0", nav_group=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="group"):
        NavItem("X", "/x/", group=bad)  # type: ignore[arg-type]


def test_every_real_app_manifest_loads_with_the_new_fields() -> None:
    manifests = registry.discover()
    names = {m.name for m in manifests}
    assert {"core", "fields", "advisor", "operations", "stations"} <= names
    for m in manifests:
        fields = {f.name for f in dataclasses.fields(m)}
        assert {
            "nav_group",
            "nav_order",
            "extra_nav",
            "widgets",
            "map_layers",
            "parcel_sections",
            "farm_sections",
            "debug_panels",
            "tools",
        } <= fields


def test_widget_and_map_layer_defaults() -> None:
    assert Widget("/x/today") == Widget("/x/today", 100, "full")
    layer = MapLayer("advice", "Spray advice", "/advisor/layer.geojson")
    assert (layer.kind, layer.order, layer.default_on, layer.icon) == ("parcels", 100, False, "")


@pytest.mark.parametrize(
    "change",
    [
        {"key": "overview"},
        {"key": "bad key"},
        {"label": ""},
        {"url": "https://evil.example/{id}"},
        {"url": "//evil.example/{id}"},
        {"url": "/provider/action"},
        {"url": "/provider/../action/{id}"},
        {"url": "/provider/{id}#fragment"},
        {"fallback_url": "/provider/{id}?parcel_id=7"},
        {"fallback_url": "/provider/{id}#bad:fragment"},
        {"fallback_url": "/provider/{id}?return_to=/"},
        {"query_keys": ("farm",)},
        {"query_keys": ("view",)},
        {"query_keys": ("parent",)},
        {"query_keys": ("return_to",)},
        {"query_keys": ("season", "season")},
        {"query_keys": ("bad-key",)},
    ],
)
def test_parcel_view_rejects_invalid_configuration_with_diagnostic(change):
    from farmcore.core.manifest import ParcelView

    values = dict(
        key="archive", label="Archive", url="/provider/{id}", fallback_url="/provider/{id}"
    )
    with pytest.raises(ValueError, match="parcel view"):
        ParcelView(**(values | change))


def test_duplicate_parcel_view_keys_rejected_across_loaded_apps():
    from farmcore.core.manifest import ParcelView

    view = ParcelView("archive", "Archive", "/provider/{id}", "/provider/{id}")
    with pytest.raises(ValueError, match="duplicate parcel view"):
        registry.resolve_order(
            [
                Manifest(name="a", version="1", parcel_views=(view,)),
                Manifest(name="b", version="1", parcel_views=(view,)),
            ]
        )


def test_duplicate_parcel_view_keys_rejected_inside_one_manifest():
    from farmcore.core.manifest import ParcelView

    view = ParcelView("archive", "Archive", "/provider/{id}", "/provider/{id}")
    with pytest.raises(ValueError, match="duplicate parcel view"):
        Manifest(name="provider", version="1", parcel_views=(view, view))
