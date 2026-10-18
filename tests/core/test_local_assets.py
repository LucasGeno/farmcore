"""Demo pages load their scripts, styles and fonts without third-party asset hosts."""

import re
from html.parser import HTMLParser
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "farmcore"


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "link" and attrs.get("rel") in {"stylesheet", "preconnect", "preload"}:
            self.urls.append(attrs.get("href", ""))


def test_templates_load_only_local_scripts_styles_and_fonts():
    for template in SRC.rglob("*.html"):
        links = AssetLinks()
        links.feed(template.read_text())
        for url in links.urls:
            assert url.startswith("/static/"), (template.relative_to(SRC), url)


def test_local_assets_and_font_files_are_served(client):
    urls = set()
    for template in SRC.rglob("*.html"):
        links = AssetLinks()
        links.feed(template.read_text())
        urls.update(links.urls)
    font_css = client.get("/static/core/vendor/dm-sans.css")
    assert font_css.status_code == 200
    fonts = re.findall(r"url\(([^)]+)\)", font_css.text)
    assert fonts
    for font in fonts:
        assert ":" not in font and "/" not in font
        urls.add(f"/static/core/vendor/{font}")
    for url in sorted(urls):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.content, url
        if url.endswith(".woff2"):
            assert response.content.startswith(b"wOF2"), url
