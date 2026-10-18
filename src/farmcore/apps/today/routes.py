"""`GET /today/`: one self-loading htmx slot per `Widget` that a visible app (loaded and not
switched off, `registry.visible_manifests`) declares in its manifest `widgets` field, sorted by
order then load order. The page knows no app: each widget sits in the shared fragment slot
(`core/_fragment.html`), so a slot that fails says which part failed and offers a Retry of the same
URL while the others still load."""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farmcore.core.manifest import Widget
from farmcore.core.registry import visible_manifests
from farmcore.core.tour import is_tour
from farmcore.web import is_expert, render

router = APIRouter()
DATA_SOURCES_URL = "/data-sources/today"


def fragments(request: Request) -> list[Widget]:
    """The visible apps' widgets in order, each with its slot `label` (the widget's own, else the
    owning app's `display_name`), which names the slot's error and Retry."""
    found = [
        (widget.order, position, replace(widget, label=widget.label or manifest.display_name))
        for position, manifest in enumerate(visible_manifests(request))
        for widget in getattr(manifest, "widgets", ())
    ]
    ordered = [widget for _, _, widget in sorted(found, key=lambda f: f[:2])]
    if not is_expert(request):
        ordered = [widget for widget in ordered if widget.url != DATA_SOURCES_URL]
        # Keep the answer first even when calm/unevaluated. Extra tools remain in closed
        # disclosures after the brief. Unknown app widgets retain their declared order.
        first = ("/advisor/today?view=attention", "/advisor/today?view=calm")
        secondary = ("/fields/today?desktop=1", "/forecast/today")
        ordered = [replace(w, span="full") if w.url in secondary else w for w in ordered]
        ordered.sort(key=lambda w: 0 if w.url in first else (2 if w.url in secondary else 1))
    return _paired(ordered)


def _paired(widgets: list[Widget]) -> list[Widget]:
    """A half widget with no half neighbour to pair with goes full, so no column stays empty
    (e.g. Weather switched off leaves Recent activity alone)."""
    out = list(widgets)
    i = 0
    while i < len(out):
        if out[i].span == "half":
            if i + 1 < len(out) and out[i + 1].span == "half":
                i += 2
                continue
            out[i] = replace(out[i], span="full")
        i += 1
    return out


@router.get("/", response_class=HTMLResponse)
def today(request: Request) -> HTMLResponse:
    bot = None if is_tour(request) else request.app.state.settings.telegram_bot_username
    return render(
        request,
        "today/index.html",
        fragments=fragments(request),
        bot_url=f"https://t.me/{bot.lstrip('@')}" if bot else None,
    )
