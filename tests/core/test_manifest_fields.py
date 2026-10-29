"""The optional manifest fields default to empty, the new contract fields validate, and every
real app loads with the values the app grid, search and Record sheet need."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from farmcore.core import registry
from farmcore.core.manifest import (
    CORE_NAME,
    ICON_NAMES,
    DraftKind,
    Manifest,
    MapLayer,
    NavItem,
    RecordType,
    Tab,
    Widget,
)


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


@pytest.mark.parametrize("group", ["apps", "more", "admin"])
def test_nav_group_accepts_the_non_chrome_groups(group: str) -> None:
    assert Manifest(name="x", version="0.1.0", nav_group=group).nav_group == group  # type: ignore[arg-type]


def test_only_core_may_place_nav_in_the_farmer_group() -> None:
    Manifest(name=CORE_NAME, version="0.1.0", nav=("Home", "/"), nav_group="farmer")
    with pytest.raises(ValueError, match="farmer"):
        Manifest(name="x", version="0.1.0", nav=("X", "/x/"), nav_group="farmer")
    with pytest.raises(ValueError, match="farmer"):
        Manifest(
            name="x",
            version="0.1.0",
            extra_nav=(NavItem("X", "/x/", "farmer"),),
        )


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


_VALID = {
    "icon": "map",
    "color": "brand",
    "keywords": {"en": ("spray", "treatment")},
    "tabs": (Tab("Plan", "/x/plan"),),
    "search": "farmcore.apps.x.search:find",
    "attention": "farmcore.apps.x.attention:items",
    "record_types": (RecordType("spray", "Spray", form="/x/record/spray"),),
    "draft_kinds": (DraftKind("spray", "farmcore.apps.x.drafts:validate", "/x/drafts/spray"),),
}


@pytest.mark.parametrize(
    "override",
    [
        pytest.param(lambda: {"icon": "no-such-icon-name"}, id="unknown-icon-name"),
        pytest.param(lambda: {"icon": "https://evil.example/x.svg"}, id="remote-icon"),
        pytest.param(lambda: {"icon": "/static/other/logo.svg"}, id="foreign-icon"),
        pytest.param(lambda: {"icon": "/static/x/logo.png"}, id="non-svg-icon"),
        pytest.param(lambda: {"icon": "/static/x/logo.svg?q=1"}, id="icon-query"),
        pytest.param(lambda: {"color": "chartreuse"}, id="bad-colour"),
        pytest.param(lambda: {"keywords": {"en": "spray"}}, id="str-keywords"),
        pytest.param(lambda: {"keywords": {"english": ("spray",)}}, id="bad-locale"),
        pytest.param(lambda: {"keywords": {"en": ("spray", "spray")}}, id="dup-synonym"),
        pytest.param(
            lambda: {"tabs": (Tab("Plan", "https://evil.example/"),)}, id="unsafe-tab-path"
        ),
        pytest.param(lambda: {"tabs": (Tab("Plan", "/other/plan"),)}, id="foreign-tab-prefix"),
        pytest.param(lambda: {"search": "not-a-provider"}, id="bad-provider-shape"),
        pytest.param(
            lambda: {
                "record_types": (RecordType("overview", "Overview", form="/x/record/overview"),)
            },
            id="reserved-record-key",
        ),
        pytest.param(
            lambda: {"record_types": (RecordType("spray", "Spray", form="/other/spray"),)},
            id="foreign-form-prefix",
        ),
        pytest.param(
            lambda: {
                "record_types": (
                    RecordType("spray", "Spray", form="/x/record/spray", draft_kind="missing"),
                )
            },
            id="undeclared-draft-kind",
        ),
        pytest.param(
            lambda: {"record_types": (RecordType("spray", "Spray", form="/x/record/spray"),) * 2},
            id="dup-record-key",
        ),
        pytest.param(
            lambda: {
                "draft_kinds": (
                    DraftKind("spray", "farmcore.apps.x.drafts:validate", "/other/drafts/spray"),
                )
            },
            id="foreign-review-prefix",
        ),
    ],
)
def test_invalid_manifest_field_fails_construction(override) -> None:
    with pytest.raises(ValueError):
        Manifest(name="x", version="0.1.0", **(_VALID | override()))


def test_full_manifest_with_every_new_field_is_valid() -> None:
    m = Manifest(name="x", version="0.1.0", **_VALID)
    assert m.icon == "map"
    assert m.color == "brand"
    assert m.keywords["en"] == ("spray", "treatment")
    assert m.tabs == (Tab("Plan", "/x/plan"),)
    assert m.record_types[0].key == "spray"
    assert m.draft_kinds[0].key == "spray"
    hash(m)  # the frozen manifest stays hashable
    with pytest.raises(TypeError):
        m.keywords["en"] = ("",)  # type: ignore[index]


def test_duplicate_record_type_keys_rejected_across_loaded_apps() -> None:
    rt = RecordType("spray", "Spray")
    with pytest.raises(ValueError, match="duplicate record type"):
        registry.resolve_order(
            [
                Manifest(name="a", version="1", record_types=(rt,)),
                Manifest(name="b", version="1", record_types=(rt,)),
            ]
        )


def test_every_in_tree_app_declares_icon_color_and_the_four_locales() -> None:
    for m in registry.discover():
        assert m.icon, m.name
        assert m.color, m.name
        assert {"fr", "es", "it", "en"} <= set(m.keywords), m.name


def test_icon_names_match_the_core_template() -> None:
    root = Path(__file__).resolve().parents[2]
    template = (root / "src/farmcore/core/templates/core/_icons.html").read_text()
    names = set(re.findall(r'name == "([a-z0-9-]+)"', template))
    assert names == set(ICON_NAMES)


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


def test_resource_scopes_default_to_empty_and_accept_declarations() -> None:
    assert Manifest(name="x", version="1").resource_scopes == ()
    declared = Manifest(
        name="x", version="1", resource_scopes=(("widget", ("fields:read", "logs:read")),)
    )
    assert declared.resource_scopes == (("widget", ("fields:read", "logs:read")),)


def test_resource_scopes_reject_duplicates_and_empty_declarations() -> None:
    with pytest.raises(ValueError, match="duplicate resource scopes"):
        Manifest(
            name="x",
            version="1",
            resource_scopes=(("widget", ("fields:read",)), ("widget", ("logs:read",))),
        )
    with pytest.raises(ValueError, match="non-empty scopes"):
        Manifest(name="x", version="1", resource_scopes=(("widget", ()),))
