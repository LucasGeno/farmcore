"""`farmcore.core.tools.collect_tools`: the active manifests' tools, in load order.

Stub manifests (`SimpleNamespace`) keep these unit tests off the real apps."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from farmcore.core.tools import ToolResult, ToolSpec, call_tool, collect_tools


def spec(name: str, func: str = "tests.apps.chat.assistant.stub_tools:echo") -> ToolSpec:
    return ToolSpec(name=name, description=name, params={}, func=func, scope="fields:read")


def test_tools_come_in_manifest_order() -> None:
    a, b, c = spec("alpha_one"), spec("beta_one"), spec("beta_two")
    manifests = [
        SimpleNamespace(name="alpha", tools=(a,)),
        SimpleNamespace(name="beta", tools=(b, c)),
    ]

    assert collect_tools(manifests) == [a, b, c]


def test_an_app_that_is_not_active_brings_no_tools() -> None:
    a, b = spec("alpha_one"), spec("beta_one")
    beta = SimpleNamespace(name="beta", tools=(b,))

    assert collect_tools([SimpleNamespace(name="alpha", tools=(a,))]) == [a]
    assert collect_tools([beta]) == [b]


def test_a_manifest_without_tools_brings_none() -> None:
    assert collect_tools([SimpleNamespace(name="nothing_here")]) == []


def test_two_tools_with_one_name_fail() -> None:
    manifests = [
        SimpleNamespace(name="alpha", tools=(spec("same"),)),
        SimpleNamespace(name="beta", tools=(spec("same"),)),
    ]
    with pytest.raises(ValueError, match="same"):
        collect_tools(manifests)


def test_call_tool_imports_func_and_passes_the_context_and_arguments() -> None:
    ctx = SimpleNamespace(marker="ctx")

    result = call_tool(spec("echo"), ctx, {"parcel": "P1"})

    assert result == ToolResult(facts={"parcel": "P1"}, text="ctx P1")


def test_the_real_apps_declare_six_tools_in_load_order() -> None:
    # Each app declares them in its manifest's `tools=`, read through the registry.
    from farmcore.core import registry

    names = [t.name for t in collect_tools(registry.discover())]

    assert names == [
        "fields_list_parcels",
        "operations_last_spray",
        "operations_recent_sprays",
        "stations_weather",
        "advisor_today",
        "advisor_parcel_status",
    ]


def test_an_uninstalled_app_loses_its_tools() -> None:
    from farmcore.core import registry

    active = [m for m in registry.discover() if m.name != "stations"]

    assert "stations_weather" not in [t.name for t in collect_tools(active)]
