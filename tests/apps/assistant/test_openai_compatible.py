"""OpenAI-compatible provider for the web assistant: recorded responses at the HTTP boundary
(httpx.MockTransport). No test here reaches a network or a real model."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from farmcore.apps.assistant import llm
from farmcore.apps.assistant.routes import chooser_for
from farmcore.core.tools import ToolSpec, object_schema
from farmcore.settings import Settings

KEY = "sk-test-secret-0123456789"


def _tools():
    from farmcore.apps.fields.tools import TOOLS as FIELD_TOOLS
    from farmcore.apps.operations.tools import TOOLS as OPS_TOOLS

    propose = ToolSpec(
        "demo_propose", "writes a draft", object_schema(), "x:y", "logs:read", effect="propose"
    )
    return [*FIELD_TOOLS, *OPS_TOOLS, propose]


def _chooser(handler, records, base_url="https://llm.example/v1"):
    return llm.OpenAICompatibleToolChooser(
        base_url=base_url,
        api_key=SecretStr(KEY),
        model="gpt-test",
        audit=records.append,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _reply(message: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": message}]})


def test_tool_call_sends_json_schemas_for_read_tools_only_and_audits_without_the_key():
    sent, records = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _reply(
            {
                "content": "P5 was sprayed.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "operations_last_spray",
                            "arguments": '{"parcel": "P5"}',
                        },
                    }
                ],
            }
        )

    choice = _chooser(handler, records).choose("When was P5 last sprayed?", _tools())

    assert choice == llm.Choice("operations_last_spray", {"parcel": "P5"}, text="P5 was sprayed.")
    request = sent[0]
    assert request.url == "https://llm.example/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-test"
    declared = {t["function"]["name"]: t["function"] for t in body["tools"]}
    assert "demo_propose" not in declared and "operations_last_spray" in declared
    assert declared["operations_last_spray"]["parameters"] == {
        "type": "object",
        "properties": {"parcel": {"type": "string"}},
    }
    [record] = records
    assert record.adapter == "openai_compatible_assistant" and record.error is None
    assert record.request_body == body and record.status == 200
    assert KEY not in repr(record)


def test_plain_answer_without_a_tool_call_is_no_choice():
    records = []
    chooser = _chooser(lambda r: _reply({"content": "I do not know."}), records)

    assert chooser.choose("poem please", _tools()) is None
    assert records[0].error is None


def test_rejected_key_raises_a_chooser_error_that_does_not_leak_the_key():
    records = []
    chooser = _chooser(lambda r: httpx.Response(401, json={"error": {"message": KEY}}), records)

    with pytest.raises(llm.ChooserError) as raised:
        chooser.choose("anything", _tools())

    assert "401" in str(raised.value) and KEY not in str(raised.value)
    assert records[0].status == 401 and records[0].error == "HTTP 401"


def test_malformed_tool_arguments_raise_a_chooser_error():
    records = []
    bad = {"tool_calls": [{"function": {"name": "operations_last_spray", "arguments": "{nope"}}]}
    chooser = _chooser(lambda r: _reply(bad), records)

    with pytest.raises(llm.ChooserError):
        chooser.choose("anything", _tools())

    assert records[0].error


@pytest.mark.parametrize(
    "url",
    [
        "ftp://llm.example/v1",
        "https://user:pw@llm.example/v1",
        "https://llm.example/v1?key=1",
        "https://llm.example:abc/v1",
    ],
)
def test_bad_base_urls_are_refused_at_startup(url):
    with pytest.raises(ValidationError, match="openai_compatible_base_url"):
        Settings(
            _env_file=None,
            assistant_provider="openai-compatible",
            openai_compatible_base_url=url,
            openai_compatible_model="m",
        )


def test_a_key_is_refused_over_plain_http_except_to_loopback():
    base = {"_env_file": None, "assistant_provider": "openai-compatible"}
    base |= {"openai_compatible_model": "llama3", "openai_compatible_api_key": SecretStr(KEY)}
    with pytest.raises(ValidationError, match="https"):
        Settings(**base, openai_compatible_base_url="http://llm.internal:8000/v1")
    for ok in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "https://llm.example/v1"):
        Settings(**base, openai_compatible_base_url=ok)


def test_no_provider_means_no_model_so_the_keyword_router_answers(settings):
    class App:
        class state:  # noqa: N801
            pass

    App.state.settings = settings.model_copy(update={"assistant_provider": "none"})
    assert chooser_for(App) is None
