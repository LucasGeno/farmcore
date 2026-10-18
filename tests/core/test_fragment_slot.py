"""The shared fragment slot (`core/_fragment.html` `fragment_slot`) keeps a named local
error and a Retry that reissues the same GET into the same body; the page never loses its retry
seam. The lifecycle script (`core/static/fragments.js`) is loaded once from the base page.

HTTP/template level only: the browser lifecycle (events, focus, announcements) is not exercised
here."""

from __future__ import annotations

import html
import re
from pathlib import Path

from farmcore.core.manifest import Widget
from farmcore.web import templates

CORE = Path(__file__).resolve().parents[2] / "src" / "farmcore" / "core"


def render(slot_id: str, label: str, url: str, **kw) -> str:
    macro = templates.env.from_string(
        '{% from "core/_fragment.html" import fragment_slot %}'
        "{{ fragment_slot(slot_id, label, url, **kw) }}"
    )
    return macro.render(slot_id=slot_id, label=label, url=url, kw=kw)


def tags(markup: str, marker: str) -> list[str]:
    return re.findall(rf"<[a-z]+\b[^>]*\s{marker}(?=[\s>])[^>]*>", markup)


def attr(tag: str, name: str) -> str | None:
    match = re.search(rf'\b{name}="([^"]*)"', tag) or re.search(rf"\b{name}='([^']*)'", tag)
    return html.unescape(match[1]) if match else None


def test_slot_has_a_stable_wrapper_body_status_and_a_hidden_named_retry() -> None:
    page = render("today-1", "Weather", "/stations/today?farm=2&x=a%20b")

    (wrapper,) = tags(page, "data-fragment")
    assert attr(wrapper, "id") == "today-1"
    (body,) = tags(page, "data-fragment-body")
    assert attr(body, "id") == "today-1-body"
    assert attr(body, "hx-get") == "/stations/today?farm=2&x=a%20b"
    assert attr(body, "hx-trigger") == "load"
    assert attr(body, "aria-busy") == "true"
    assert attr(body, "hx-swap") == "innerHTML"
    assert attr(body, "hx-target") == "this"
    assert attr(body, "hx-sync") == "closest [data-fragment]:drop"
    assert attr(body, "hx-disinherit") == "*"
    assert attr(body, "tabindex") == "-1"
    assert "outerHTML" not in page

    (status,) = tags(page, "data-fragment-status")
    assert attr(status, "role") == "status" and attr(status, "aria-live") == "polite"
    assert re.search(r"data-fragment-status[^>]*>\s*</", page), "the live node starts empty"

    (error,) = tags(page, "data-fragment-error")
    assert " hidden" in error
    assert "Weather could not load." in page
    (retry,) = tags(page, "data-fragment-retry")
    assert attr(retry, "type") == "button"
    assert attr(retry, "hx-get") == attr(body, "hx-get")
    assert attr(retry, "hx-target") == "#today-1-body"
    assert attr(retry, "hx-swap") == "innerHTML"
    assert attr(retry, "hx-sync") == "closest [data-fragment]:drop"
    assert "min-h-[44px]" in attr(retry, "class")
    assert re.search(r"data-fragment-retry[^>]*>\s*Retry Weather\s*</button>", page)


def test_slot_requests_carry_an_explicit_timeout() -> None:
    page = render("s", "Weather", "/w")
    for tag in tags(page, "data-fragment-body") + tags(page, "data-fragment-retry"):
        assert re.fullmatch(r'\{"timeout": ?\d+\}', attr(tag, "hx-request") or "")


def test_label_is_escaped_and_inert() -> None:
    nasty = """Bad "label" <script>alert('x')</script> & co"""
    page = render("s-1", nasty, '/w?a="b"&c=<d>')

    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    (wrapper,) = tags(page, "data-fragment")
    assert attr(wrapper, "data-fragment-label") == nasty
    (retry,) = tags(page, "data-fragment-retry")
    assert attr(retry, "hx-get") == '/w?a="b"&c=<d>'


def test_the_macro_does_not_rebuild_the_farm_url() -> None:
    """The caller passes a farm-qualified URL; the macro emits it verbatim (no request needed)."""
    page = render("s", "Weather", "/stations/today")
    assert 'hx-get="/stations/today"' in page
    assert "farm=" not in page


def test_trigger_and_classes_are_caller_options() -> None:
    page = render(
        "m", "Map", "/fields/today", trigger="load[wide]", wrapper_class="hidden lg:block"
    )
    (wrapper,) = tags(page, "data-fragment")
    assert "hidden lg:block" in attr(wrapper, "class")
    (body,) = tags(page, "data-fragment-body")
    assert attr(body, "hx-trigger") == "load[wide]"


def test_widget_label_is_an_optional_trailing_field() -> None:
    assert Widget("/x") == Widget("/x", 100, "full")
    assert Widget("/x").label == ""
    assert Widget("/x", 10, "half", "Recent activity").label == "Recent activity"


def test_fragments_script_is_included_once_from_base() -> None:
    base = (CORE / "templates" / "core" / "base.html").read_text()
    assert base.count('src="/static/core/fragments.js"') == 1


def test_fragments_script_handles_every_terminal_event_once() -> None:
    js = (CORE / "static" / "fragments.js").read_text()
    for event in (
        "htmx:beforeSend",
        "htmx:afterRequest",
        "htmx:responseError",
        "htmx:sendError",
        "htmx:timeout",
        "htmx:sendAbort",
    ):
        assert f'"{event}"' in js, event
    assert "__farmcoreFragments" in js  # install guard: one listener set per document
    assert "isConnected" in js
    assert "innerHTML" not in js  # never builds markup from strings


def _function(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n  }\n", start)]


def test_slot_ownership_is_bound_by_xhr_at_send_not_by_the_rebroadcast_element() -> None:
    """Review R1 (static contract, not a browser run): htmx 2.0.4 rewrites detail.elt on every
    dispatch and rebroadcasts afterRequest from a surviving ancestor when a nested issuer removed
    itself. Ownership is therefore recorded once at beforeSend (the dispatch target is the true
    issuer, its target must be the slot's own body) in an xhr-keyed map, and every terminal
    handler looks the request up by detail.xhr only, so a nested request inside a loaded slot
    can neither announce "loaded" nor clear the slot's error."""
    js = (CORE / "static" / "fragments.js").read_text()
    sending = _function(js, "sending")
    assert "partsFor(evt.target)" in sending
    assert "d.target !== p.body" in sending and "d.elt !== evt.target" in sending
    assert "owned.set(d.xhr, p)" in sending
    finish = _function(js, "finish")
    assert "evt.detail.xhr" in finish and "owned.get(xhr)" in finish
    assert "owned.delete(xhr)" in finish and "isConnected" in finish
    for name in ("loaded", "failed", "aborted"):
        body = _function(js, name)
        assert "finish(evt)" in body, name
        assert "detail.elt" not in body and "partsFor" not in body, name
    assert "new WeakMap()" in js
