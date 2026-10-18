"""The advice card, the debug drawer and the map use the shell's tokens and macros, not local
stand-ins."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "farmcore"
FILES = [
    "apps/advisor/templates/advisor/_current_advice.html",
    "apps/advisor/templates/advisor/index.html",
    "apps/advisor/templates/advisor/_debug_panel.html",
    "core/templates/core/_debug_drawer.html",
    "apps/fields/templates/fields/_today_map.html",
    "apps/fields/templates/fields/map.html",
    "apps/fields/static/map.js",
    "apps/fields/service.py",
    "apps/advisor/templates/advisor/_card_body.html",
    "apps/advisor/templates/advisor/_workspace_protection.html",
]
# The pre-shell palette (Tailwind orange/teal/amber/stone) and its local font and classes.
OLD = [
    r"#C2410C",
    r"#0F766E",
    r"#B45309",
    r"#78716C",
    r"#1C1917",
    r"#57534E",
    r"\bInter\b",
    r"--fc-(?:spray|protected|wait|ink|muted|line|debug)",
    r"fc-chip",
    r"advisor/_chip\.html",
    r"text-stone-|bg-stone-|border-stone-|text-sky-",
]


@pytest.mark.parametrize("name", FILES)
def test_file_uses_the_shell_tokens_only(name):
    text = (SRC / name).read_text()
    for pattern in OLD:
        assert not re.search(pattern, text, re.IGNORECASE), (name, pattern)


@pytest.mark.parametrize("name", FILES[:2] + FILES[4:6])
def test_farmer_templates_take_the_chip_from_the_core_macros(name):
    text = (SRC / name).read_text()
    assert re.search(r'from "core/_ui\.html" import [^%]*decision_chip', text), name
    assert 'from "core/_icons.html" import icon' in text, name


@pytest.mark.parametrize("name", FILES[-2:])
def test_advice_callers_share_the_token_guarded_presentation(name):
    text = (SRC / name).read_text()
    assert text.count('{% include "advisor/_current_advice.html" %}') == 1, name
    assert "decision_chip(" not in text, name


# The expert pages: the same tokens, one page header, flat hairline cards and pills.
U11_PAGES = [
    "apps/operations/templates/operations/index.html",
    "apps/chat/templates/chat/index.html",
    "apps/stations/templates/stations/list.html",
]
U11_FILES = [*U11_PAGES, "apps/stations/templates/stations/_health.html"]
RAW_PALETTE = r"\b[\w:-]*(?:stone|emerald|amber|sky|red|white)-\d{2,3}\b|\bbg-white\b"


@pytest.mark.parametrize("name", U11_FILES)
def test_u11_templates_use_the_shell_tokens_not_the_raw_palette(name):
    text = (SRC / name).read_text()
    assert not re.findall(RAW_PALETTE, text), name


@pytest.mark.parametrize("name", U11_PAGES)
def test_u11_pages_open_with_the_shell_page_header(name):
    text = (SRC / name).read_text()
    assert re.search(r'from "core/_ui\.html" import [^%]*page_header', text), name
    assert "<h1" not in text, name  # the macro renders the one h1
