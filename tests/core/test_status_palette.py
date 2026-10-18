"""N1: one traffic-light palette. Spray red, wait amber, protected green, no action grey; the
tokens, CSS variables and map colours agree, and text/chips/fills meet WCAG AA."""

from __future__ import annotations

import re
from pathlib import Path

from farmcore.apps.fields.service import DECISION_STYLES

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "src/farmcore/core/static/farmcore.css").read_text()


def _lum(hex_: str) -> float:
    rgb = [int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _hex(name: str) -> str:
    m = re.search(rf"--{name}-rgb: (\d+) (\d+) (\d+)", CSS)
    return "#{:02x}{:02x}{:02x}".format(*(int(g) for g in m.groups()))


def _token(name: str) -> tuple[str, str]:
    return _hex(name), _hex(f"{name}-soft")


def _hue(hex_: str) -> str:
    r, g, b = (int(hex_[i : i + 2], 16) for i in (1, 3, 5))
    if max(r, g, b) - min(r, g, b) < 40:
        return "grey"
    if r >= 0xB0 and g < 0.3 * r and b < 0.3 * r:
        return "red"
    if r >= 0xA0 and 0.5 * r <= g <= 0.75 * r and b < 0.25 * r:
        return "amber"
    if g > r and g > b and b < 0.6 * g:
        return "green"
    return "other"


def test_status_hues_are_red_amber_green_grey():
    assert _hue(DECISION_STYLES["spray"][1]) == "red"
    assert _hue(DECISION_STYLES["wait"][1]) == "amber"
    assert _hue(DECISION_STYLES["protected"][1]) == "green"
    assert _hue(DECISION_STYLES["none"][1]) == "grey"


def test_tokens_css_vars_and_map_colours_agree():
    for key, name in (("spray", "spray"), ("wait", "wait"), ("protected", "protected")):
        default, soft = _token(name)
        assert default == DECISION_STYLES[key][1]
        assert f"--{name}: rgb(var(--{name}-rgb))" in CSS


def test_chips_and_solid_fills_meet_aa():
    for name in ("spray", "wait", "protected"):
        default, soft = _token(name)
        assert _ratio(default, soft) >= 4.5, (name, "text on soft chip")
        assert _ratio(default, "#ffffff") >= 4.5, (name, "white text on solid fill")
        assert _ratio(default, "#fdfdf9") >= 4.5, (name, "text on surface")
    assert _ratio(DECISION_STYLES["none"][1], "#ffffff") >= 4.5


def test_status_text_on_the_page_ground_meets_aa():
    """Text sits straight on the ground (#eef1ea) too, e.g. "7 parcels protected" on Home."""
    ground = _hex("ground")
    assert _ratio(_hex("spray"), ground) >= 4.5
    assert _ratio(_hex("wait"), ground) >= 4.5
    assert _ratio(_hex("protected-ink"), ground) >= 4.5
    assert _ratio(_hex("protected-ink"), _hex("surface")) >= 4.5


def test_home_protected_lines_use_the_ink_token_not_the_fill_green():
    text = (ROOT / "src/farmcore/apps/advisor/templates/advisor/_today_home.html").read_text()
    # Copy inflects parcel/ parcels; identify the count independently of its plural form.
    lines = [ln for ln in text.splitlines() if "{{ protected|length }} parcel" in ln]
    assert len(lines) == 2
    assert all("text-protected-ink" in ln and "text-protected " not in ln for ln in lines)
