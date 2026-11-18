"""The optional manifest fields default to empty, the new contract fields validate, and every
real app loads with the values the app grid, search and Record sheet need."""

from __future__ import annotations

import dataclasses

import pytest

from farmcore.core import registry
from farmcore.core.manifest import (
    COLOR_TOKENS,
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
    assert m.icon == ""
    assert m.color == ""
    assert m.keywords == {}
    assert m.tabs == ()
    assert m.search == ""
    assert m.record_types == ()
    assert m.draft_kinds == ()
    assert m.attention == ""


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
            "icon",
            "color",
            "keywords",
            "tabs",
            "search",
            "record_types",
            "draft_kinds",
            "attention",
        } <= fields


def test_widget_and_map_layer_defaults() -> None:
    assert Widget("/x/today") == Widget("/x/today", 100, "full")
    layer = MapLayer("advice", "Spray advice", "/advisor/layer.geojson")
    assert (layer.kind, layer.order, layer.default_on, layer.icon) == ("parcels", 100, False, "")


def test_icon_is_a_known_name_or_a_safe_svg_path() -> None:
    Manifest(name="x", version="0.1.0", icon="map")
    Manifest(name="x", version="0.1.0", icon="/static/x/icons/foo.svg")
    with pytest.raises(ValueError, match="icon"):
        Manifest(name="x", version="0.1.0", icon="no-such-icon-name")
    with pytest.raises(ValueError, match="icon"):
        Manifest(name="x", version="0.1.0", icon="https://evil.example/x.svg")
    with pytest.raises(ValueError, match="icon"):
        Manifest(name="x", version="0.1.0", icon="//evil.example/x.svg")


def test_color_is_a_palette_token() -> None:
    Manifest(name="x", version="0.1.0", color="brand")
    with pytest.raises(ValueError, match="color"):
        Manifest(name="x", version="0.1.0", color="chartreuse")


def test_keywords_are_per_locale_synonyms() -> None:
    Manifest(name="x", version="0.1.0", keywords={"en": ("spray", "treatment")})
    with pytest.raises(ValueError, match="keyword"):
        Manifest(name="x", version="0.1.0", keywords={"english": ("spray",)})
    with pytest.raises(ValueError, match="keyword"):
        Manifest(name="x", version="0.1.0", keywords={"en": ("spray", "spray")})
    with pytest.raises(ValueError, match="keyword"):
        Manifest(name="x", version="0.1.0", keywords={"en": ("",)})


def test_tab_paths_and_providers_are_validated() -> None:
    Manifest(
        name="x",
        version="0.1.0",
        tabs=(Tab("Plan", "/x/plan"),),
        search="farmcore.apps.x.search:find",
        attention="farmcore.apps.x.attention:items",
    )
    with pytest.raises(ValueError, match="tab"):
        Tab("", "/x/")
    with pytest.raises(ValueError, match="tab"):
        Tab("Plan", "https://evil.example/")
    with pytest.raises(ValueError, match="search"):
        Manifest(name="x", version="0.1.0", search="not-a-provider")
    with pytest.raises(ValueError, match="attention"):
        Manifest(name="x", version="0.1.0", attention="farmcore.apps.x.attention")


def test_record_types_validate_key_label_icon_and_form() -> None:
    Manifest(
        name="x",
        version="0.1.0",
        record_types=(RecordType("spray", "Spray", icon="spray", form="/x/record/spray"),),
    )
    with pytest.raises(ValueError, match="record type"):
        RecordType("Bad Key", "Spray", form="/x/")
    with pytest.raises(ValueError, match="record type"):
        RecordType("overview", "Overview", form="/x/")
    with pytest.raises(ValueError, match="record type"):
        RecordType("spray", "", form="/x/")
    with pytest.raises(ValueError, match="record type"):
        RecordType("spray", "Spray", form="https://evil.example/x")


def test_duplicate_record_type_keys_are_rejected() -> None:
    rt = RecordType("spray", "Spray", form="/x/record/spray")
    with pytest.raises(ValueError, match="duplicate record type"):
        Manifest(name="x", version="0.1.0", record_types=(rt, rt))
    with pytest.raises(ValueError, match="duplicate record type"):
        registry.resolve_order(
            [
                Manifest(name="a", version="1", record_types=(rt,)),
                Manifest(name="b", version="1", record_types=(rt,)),
            ]
        )


def test_record_type_draft_kind_must_be_declared() -> None:
    rt = RecordType("spray", "Spray", form="/x/record/spray", draft_kind="missing")
    with pytest.raises(ValueError, match="draft_kind"):
        Manifest(name="x", version="0.1.0", record_types=(rt,))
    Manifest(
        name="x",
        version="0.1.0",
        record_types=(rt,),
        draft_kinds=(DraftKind("missing", "farmcore.apps.x.drafts:validate", "/x/drafts/missing"),),
    )


def test_draft_kinds_validate_validator_and_review_fragment() -> None:
    DraftKind("spray", "farmcore.apps.x.drafts:validate", "/x/drafts/spray")
    with pytest.raises(ValueError, match="draft kind"):
        DraftKind("spray", "not-a-provider", "/x/drafts/spray")
    with pytest.raises(ValueError, match="draft kind"):
        DraftKind("spray", "farmcore.apps.x.drafts:validate", "https://evil.example/")
    with pytest.raises(ValueError, match="draft kind"):
        DraftKind("Bad Key", "farmcore.apps.x.drafts:validate", "/x/drafts/spray")


def test_every_in_tree_app_declares_icon_color_and_the_four_locales() -> None:
    for m in registry.discover():
        assert m.icon and (m.icon in ICON_NAMES or m.icon.startswith("/")), m.name
        assert m.color in COLOR_TOKENS, m.name
        assert {"fr", "es", "it", "en"} <= set(m.keywords), m.name


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
