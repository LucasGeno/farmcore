"""Tool contract: every tool declares its effect and real JSON Schema params; the Vertex subset
sent to the model is derived from them and stays what it was."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from farmcore.apps.assistant.llm import declarations
from farmcore.apps.assistant.tools import PLAN
from farmcore.core.tools import ToolSpec, collect_tools, object_schema
from tests.apps.advisor import scenes

manifests = scenes.manifests

# What the model was sent before params became JSON Schema (literal, not recomputed).
VERTEX_PARCEL = {"type": "OBJECT", "properties": {"parcel": {"type": "STRING"}}}
VERTEX_BEFORE = {
    "advisor_today": None,
    "advisor_parcel_status": VERTEX_PARCEL,
    "assistant_plan": None,
    "fields_list_parcels": None,
    "operations_last_spray": VERTEX_PARCEL,
    "operations_recent_sprays": None,
    "review_assessment_status": VERTEX_PARCEL,
    "stations_weather": {"type": "OBJECT", "properties": {"day": {"type": "STRING"}}},
}


def test_every_tool_is_a_read_tool_with_a_valid_json_schema(manifests):
    tools = [*collect_tools(manifests), PLAN]
    assert {t.name for t in tools} == set(VERTEX_BEFORE)
    for tool in tools:
        Draft202012Validator.check_schema(tool.params)
        assert tool.effect == "read", tool.name


def test_vertex_declarations_of_existing_tools_are_unchanged(manifests):
    sent = {d["name"]: d.get("parameters") for d in declarations([*collect_tools(manifests), PLAN])}
    assert sent == VERTEX_BEFORE


def test_vertex_subset_of_a_rich_schema_keeps_types_enums_required_and_nesting():
    spec = ToolSpec(
        name="demo",
        description="d",
        params=object_schema(
            {
                "kind": {"type": "string", "enum": ["a", "b"], "description": "which"},
                "limit": {"type": "integer"},
                "box": object_schema({"x": {"type": "number"}}, required=("x",)),
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("kind",),
        ),
        func="x:y",
        scope="logs:read",
        effect="propose",
    )
    Draft202012Validator.check_schema(spec.params)
    [decl] = declarations([spec])
    assert decl["parameters"] == {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING", "enum": ["a", "b"], "description": "which"},
            "limit": {"type": "INTEGER"},
            "box": {"type": "OBJECT", "properties": {"x": {"type": "NUMBER"}}, "required": ["x"]},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["kind"],
    }


def test_a_tool_must_say_read_or_propose_and_carry_a_schema_vertex_can_take():
    base = {"name": "t", "description": "d", "func": "x:y", "scope": "logs:read"}
    with pytest.raises(ValueError, match="effect"):
        ToolSpec(params=object_schema(), effect="write", **base)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ToolSpec(params=object_schema(), **base)  # type: ignore[call-arg]
    bad = ToolSpec(
        params={"type": "object", "properties": {"a": {"anyOf": []}}}, effect="read", **base
    )
    with pytest.raises(ValueError, match="unsupported"):
        collect_tools([type("M", (), {"name": "m", "tools": (bad,)})()])
