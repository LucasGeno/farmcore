"""Honesty guard: wording never claims safety or certainty, and assessments never rank products.

The wording scan reads what ships to a person: template text and the string literals in `src`
(docstrings and comments excluded), in English, French, Spanish and Italian. Matching is on word
boundaries, so `unsafe`, `safeguard` and CSS like `safe-area-inset` need no entry. Every
allowlisted use says why it is not a claim.
"""

from __future__ import annotations

import ast
import html
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path

from farmcore.apps.advisor import service
from farmcore.core import seed
from farmcore.core.clock import clock_row
from farmcore.core.models.canonical import Asset
from tests.apps.advisor.engine.test_tankmix import CONFIG, IN_PERIOD, PARIS, _downy_due
from tests.apps.operations.conftest import make_parcel

SRC = Path(__file__).parents[2] / "src" / "farmcore"

FORBIDDEN = (
    # en
    r"safe",
    r"safely",
    r"low[ -]risk",
    r"no(?: \w+){0,2} risk",
    r"risk[ -]free",
    r"harmless",
    r"guarantee[ds]?",
    r"guaranteeing",
    # fr
    r"sûre?s?",
    r"sans (?:risque|danger)",
    r"risque (?:faible|nul)",
    r"inoffensifs?|inoffensives?",
    r"garantis?|garanties?|garantit|garantir",
    # es: a bare "bajo" is plain "under"; only the risk phrases claim anything
    r"segur[oa]s?",
    r"sin riesgo",
    r"bajo riesgo|riesgo (?:bajo|nulo)",
    r"inofensiv[oa]s?",
    r"garantizad[oa]s?|garantiza|garantizar",
    # it
    r"sicur[oa]|sicuri|sicure",
    r"senza rischi[oa]",
    r"basso rischio|rischio basso",
    r"innocu[oa]|innocui|innocue",
    r"garantit[oa]|garantiti|garantite|garantisce|garantire",
)
CLAIM = re.compile(r"(?<!\w)(?:" + "|".join(FORBIDDEN) + r")(?!\w)", re.IGNORECASE)

# (path relative to src/farmcore, the match in context as `_occurrence` prints it) -> why this one
# occurrence claims nothing. Each entry allows exactly one occurrence: a second identical one,
# or the same phrase anywhere else in the file, fails; so does an entry nothing matches.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "apps/advisor/routes.py",
        "Gubler–Thomas low-risk band up to index",
    ): "names the Gubler–Thomas index band; a model tier, not a safety claim",
    (
        "apps/advisor/routes.py",
        "interval in the Gubler–Thomas low-risk band",
    ): "names the Gubler–Thomas index band; a model tier, not a safety claim",
    (
        "apps/advisor/engine/mildew/powdery_gubler_thomas.py",
        "interval 14–21 d at low risk;",
    ): "Gubler–Thomas parameter source citing UC IPM's low band; edits change config digests",
    (
        "apps/planning/templates/planning/index.html",
        "stays unknown rather than safe.",
    ): "says the opposite: an unknown availability stays unknown rather than safe",
    (
        "apps/onboarding/importers/parsers.py",
        "read this parcel file safely; check its format,",
    ): "a parser refuses a file it cannot read; a refusal, not a claim",
}

_JINJA_NOISE = re.compile(r"\|\s*safe\b")
_NOT_TEXT = re.compile(
    r"<style\b.*?</style>|<!--.*?-->|\{#.*?#\}|\bclass=\"[^\"]*\"|\bstyle=\"[^\"]*\"",
    re.DOTALL | re.IGNORECASE,
)


def _template_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    return html.unescape(_JINJA_NOISE.sub("", _NOT_TEXT.sub(" ", raw)))


def _python_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _occurrence(text: str, match: re.Match) -> str:
    """The match with up to 4 words either side, within its own text node."""
    before = re.search(r"(?:\S+\s+){0,4}$", text[: match.start()].rsplit(">", 1)[-1])
    after = re.match(r"(?:\s*\S+){0,4}", text[match.end() :].split("<", 1)[0])
    return " ".join((before.group(0) + match.group(0) + after.group(0)).split())


def claims_in(root: Path) -> Counter[tuple[str, str]]:
    """(relative path, occurrence in context) of every forbidden phrase under `root`, counted."""
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(root.rglob("*")):
        if path.suffix == ".html":
            texts = [_template_text(path)]
        elif path.suffix == ".py":
            texts = _python_strings(path)
        else:
            continue
        rel = path.relative_to(root).as_posix()
        found.update((rel, _occurrence(t, m)) for t in texts for m in CLAIM.finditer(t))
    return found


def test_shipped_text_makes_no_safety_or_certainty_claim():
    found = claims_in(SRC)
    allowed = Counter(ALLOWED.keys())

    assert not found - allowed, (
        f"forbidden claim wording (fix it, or allow it with a reason): {found - allowed}"
    )
    assert not allowed - found, f"stale allowlist entry: remove it: {allowed - found}"


def test_the_scan_catches_a_planted_claim_in_every_locale_and_nothing_legitimate(tmp_path):
    planted = {
        "en.html": "<p>This spray plan is safe for bees.</p>",
        "en2.html": "<p>Low-risk week; results are guaranteed.</p>",
        "en3.html": "<p>No infection risk right now.</p>",
        "fr.html": "<p>Ce traitement est sans danger et garanti.</p>",
        "es.html": "<p>Aplicación sin riesgo, resultado garantizado.</p>",
        "it.html": "<p>Trattamento sicuro e innocuo.</p>",
    }
    legitimate = {
        "ok.html": '<div class="pb-[env(safe-area-inset-bottom)] motion-safe:x">{{ v|safe }} '
        "Unsafe path; safeguard; infection risk unknown.</div><!-- safe -->",
        "es_ok.html": "<p>Guardar bajo otro nombre; precio bajo; riesgo de mildiu.</p>",
    }
    for name, body in {**planted, **legitimate}.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    (tmp_path / "msg.py").write_text('MESSAGE = "harmless to bees"\n', encoding="utf-8")

    hit_files = {rel for rel, _ in claims_in(tmp_path)}

    assert hit_files == {*planted, "msg.py"}


# --- assessments never rank products ---------------------------------------------------------

RANKING = re.compile(
    r"(?<!\w)(?:best|better|top|rank\w*|score\w*|preferred|first choice|recommended first|"
    r"most effective|#1)(?!\w)",
    re.IGNORECASE,
)
RANK_KEYS = re.compile(r"rank|score|order|preference|best|position|weight", re.IGNORECASE)


def test_a_two_product_assessment_never_ranks_its_products(client, session, settings):
    advice = _downy_due(IN_PERIOD, sulphur_days_ago=17)  # copper pass carrying sulphur
    composed = [p.name for p in advice.products_recommended]
    assert sorted(composed) == ["copper", "sulphur"]
    clock = clock_row(session, settings)
    clock.mode, clock.replay_now = "replay", IN_PERIOD + timedelta(minutes=10)
    farm = seed.ensure_default_farm(session, settings)
    parcel = make_parcel(session, farm, 1, area="1", props={})
    session.add(Asset(farm_id=farm.id, kind="station", name="Roof station"))
    row = service.advice_row(
        advice,
        series=[],
        config=CONFIG,
        farm_id=farm.id,
        parcel_id=parcel.id,
        now=IN_PERIOD,
        tz=str(PARIS),
    )
    session.add(row)
    session.commit()
    result = row.assessment.result

    advice_json = client.get(f"/advisor/parcels/{parcel.id}.json").json()
    page = re.sub(r"<[^>]+>", "\n", html.unescape(client.get(f"/advisor/parcels/{parcel.id}").text))

    # The whole recommendation (envelope result, stored row, advice.json) lists the products as
    # plain entries in the order the engine composed the tank mix, with no ranking field beside
    # them or inside them.
    assert not [k for k in result if RANK_KEYS.search(k)]
    for products in (result["products"], row.products, advice_json["products"]):
        assert [p["name"] for p in products] == composed
        assert all(set(p) == {"name", "product_type", "targets"} for p in products)
    # Wherever the recommendation names a product (headline, reasons, the page lines that show
    # it), nothing calls one better than another.
    texts = [result["headline"], *(r["verdict"] + " " + r["note"] for r in result["reasons"])]
    texts += [line for line in page.splitlines() if any(n in line.lower() for n in composed)]
    named = [t for t in texts if any(n in t.lower() for n in composed)]
    assert named
    assert not [t for t in named if RANKING.search(t)]
