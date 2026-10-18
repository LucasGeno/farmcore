"""Literal template classes must have CSS, including Jinja choices. No Node at test time.

Rebuild with scripts/build_css.sh after changing templates. A superset build is valid, so
pending branches can contribute utilities before they merge. Runtime-composed names still
need complete literals or a Tailwind safelist entry, as documented in tailwind.config.js.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, nodes

SRC = Path(__file__).resolve().parents[2] / "src" / "farmcore"
# These are structural/JS hooks without their own style rule, plus Tailwind state markers.
UNSTYLED = {"fc-main", "fc-debug", "fc-more", "fc-card", "fc-count", "group", "peer"}
CSS_IDENT = r"(?:\\[0-9a-fA-F]{1,6}\s?|\\[^\r\n0-9a-fA-F]|[-_a-zA-Z0-9\u0080-\uffff])+"
CSS_ESCAPE = r"\\([0-9a-fA-F]{1,6}\s?|[^\r\n0-9a-fA-F])"
# Jinja strings outside class attributes include labels/URLs as well as utility choices.
UTILITY = re.compile(
    r"^(?:[\w-]+:)*(?:-?(?:text|bg|border|ring|outline|shadow|rounded|font|leading|tracking|"
    r"whitespace|break|overflow|overscroll|object|items|justify|content|self|place|grid|col|row|"
    r"gap|space|flex|basis|grow|shrink|order|h|w|min-h|max-h|min-w|max-w|p|px|py|pt|pb|pl|pr|"
    r"ps|pe|m|mx|my|mt|mb|ml|mr|ms|me|inset|top|bottom|left|right|z|opacity|transition|"
    r"duration|ease|translate|rotate|scale|cursor|decoration|underline|list|stroke|fill|align|"
    r"table|animate|fc)-\S+|\[\S+\]|block|inline|flex|grid|hidden|sticky|fixed|relative|absolute|"
    r"underline|italic|antialiased|truncate|border|rounded|shadow|sr-only|not-sr-only)$"
)


class ClassNames(HTMLParser):
    def __init__(self):
        super().__init__()
        self.classes = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "class" and value:
                self.classes.update(value.split())


def _unescape(match):
    value = match[1]
    if re.fullmatch(r"[0-9a-fA-F]{1,6}\s?", value):
        return chr(int(value.strip(), 16))
    return value


def test_literal_template_classes_have_css():
    templates = {p: p.read_text() for p in SRC.rglob("*.html")}
    css = "\n".join(p.read_text() for p in SRC.rglob("*.css"))
    css += "\n".join(
        block
        for text in templates.values()
        for block in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S)
    )
    selectors = {
        re.sub(CSS_ESCAPE, _unescape, name) for name in re.findall(r"\.(" + CSS_IDENT + ")", css)
    }
    missing = {}
    for path, raw in templates.items():
        # Preserve macro-generated tag names so their literal class attributes are parsed.
        html = re.sub(r"<(/?){{.*?}}", r"<\1div", raw, flags=re.S)
        html = re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", " ", html, flags=re.S)
        parser = ClassNames()
        parser.feed(html)
        for node in Environment().parse(raw).find_all(nodes.Const):
            if isinstance(node.value, str):
                parser.classes.update(t for t in node.value.split() if UTILITY.fullmatch(t))
        absent = parser.classes - selectors - UNSTYLED
        if absent:
            missing[str(path.relative_to(SRC))] = sorted(absent)
    assert not missing, f"Missing CSS; run scripts/build_css.sh and commit its output: {missing}"
