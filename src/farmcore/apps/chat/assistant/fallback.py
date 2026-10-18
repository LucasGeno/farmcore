"""No-LLM keyword fallback: a question → one tool → its plain-words answer, offline.

It picks the tool whose `ToolSpec.keywords` match the question (whole words, case and accents
ignored), preferring a tool that takes a `parcel` when the question names one and a tool that
does not when it names none, then the longest keyword, then load order. Arguments: `parcel` is
the parcel named in the question (alias or name), `day` is today / tomorrow / yesterday, the day
after tomorrow / before yesterday, or a `YYYY-MM-DD` date. The reply is the tool's `text` (3
lines max) with its `links` as URL buttons (absolute via `public_base_url`; on a local base URL,
which Telegram rejects, in the text).

Covers the four demo questions; no follow-ups, combined questions or free phrasing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from farmcore.apps.chat import push
from farmcore.apps.chat.dto import Outgoing
from farmcore.apps.fields import tools as fields_tools
from farmcore.core.tools import ToolContext, ToolSpec, call_tool
from farmcore.core.urls import with_farm

logger = logging.getLogger(__name__)

MAX_LINES = 3
PARCEL = "parcel"
DAY = "day"
EXAMPLES = (
    "“When did I last spray Le Plateau?”, “What do I spray today?”, “Is Peyrot protected?” "
    "or “Weather tomorrow?”"
)
EXAMPLES_FR = (
    "« Quand ai-je traité Le Plateau ? », « Que traiter aujourd'hui ? », "
    "« Est-ce que Peyrot est protégé ? » ou « Météo demain ? »"
)
REPLY_NO_ANSWER = {
    "en": f"I can't answer that yet; try {EXAMPLES}.",
    "fr": f"Je ne sais pas encore répondre à ça ; essayez {EXAMPLES_FR}.",
}
# A message that is neither a question nor a spray (a greeting, "thanks"): how to log, what to ask.
REPLY_HELP = {
    "en": (
        "Tell me what you sprayed, for example “P1 copper, 2 kg, this morning”.\n"
        f"Or ask: {EXAMPLES}."
    ),
    "fr": (
        "Dites-moi ce que vous avez traité, par exemple « P1 cuivre, 2 kg, ce matin ».\n"
        f"Ou demandez : {EXAMPLES_FR}."
    ),
}
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# Longest first: "après-demain" folds to "apres demain", which contains " demain ".
DAYS = (
    ("day after tomorrow", "day after tomorrow"),
    ("apres demain", "day after tomorrow"),
    ("day before yesterday", "day before yesterday"),
    ("avant hier", "day before yesterday"),
    ("tomorrow", "tomorrow"),
    ("demain", "tomorrow"),
    ("yesterday", "yesterday"),
    ("hier", "yesterday"),
)


def _has(padded: str, phrase: str) -> bool:
    key = fields_tools.fold(phrase)
    return bool(key) and f" {key} " in padded


def choose(tools: Sequence[ToolSpec], text: str, names_parcel: bool) -> ToolSpec | None:
    padded = f" {fields_tools.fold(text)} "
    best: tuple[tuple[bool, int, int], ToolSpec] | None = None
    for order, tool in enumerate(tools):
        matched = [len(k) for k in tool.keywords if _has(padded, k)]
        if not matched:
            continue
        fits = (PARCEL in tool.params) == names_parcel
        score = (fits, max(matched), -order)
        if best is None or score > best[0]:
            best = (score, tool)
    return best[1] if best else None


def _day(text: str) -> str:
    m = _DATE.search(text)
    if m:
        return m.group(1)
    padded = f" {fields_tools.fold(text)} "
    for word, day in DAYS:
        if f" {word} " in padded:
            return day
    return "today"


def _reply(text: str, links: Sequence[tuple[str, str]], base_url: str, farm_id: int) -> Outgoing:
    lines = text.splitlines()[:MAX_LINES]
    absolute = [(label, base_url.rstrip("/") + with_farm(path, farm_id)) for label, path in links]
    if push.button_ok(base_url):
        return Outgoing(text="\n".join(lines), url_buttons=absolute)
    return Outgoing(text="\n".join([*lines, *(f"{label}: {url}" for label, url in absolute)]))


def answer(text: str, tools: Sequence[ToolSpec], ctx: ToolContext, base_url: str) -> Outgoing:
    no_answer = Outgoing(text=REPLY_NO_ANSWER.get(ctx.lang, REPLY_NO_ANSWER["en"]))
    if not tools:
        return no_answer
    parcel = fields_tools.mentioned_parcel(ctx.repo, text)
    tool = choose(tools, text, names_parcel=parcel is not None)
    if tool is None:
        return no_answer
    args: dict[str, str] = {}
    if PARCEL in tool.params:
        args[PARCEL] = parcel.alias if parcel is not None else ""
    if DAY in tool.params:
        args[DAY] = _day(text)
    try:
        # A savepoint: a failing tool leaves the chat's transaction (and its access_log) usable.
        with ctx.repo.session.begin_nested():
            result = call_tool(tool, ctx, args)
    except Exception as exc:
        logger.warning("assistant tool %s failed (%s)", tool.name, type(exc).__name__)
        return no_answer
    return _reply(result.text, result.links, base_url, ctx.repo.farm_id)
