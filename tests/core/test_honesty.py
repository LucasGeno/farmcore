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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from farmcore.apps.advisor import assessment as spray_assessment
from farmcore.apps.advisor.advice import Advice
from farmcore.core import assessments, seed
from farmcore.core.clock import clock_row
from farmcore.core.models.canonical import Asset
from tests.apps.operations.conftest import make_parcel

SRC = Path(__file__).parents[2] / "src" / "farmcore"

FORBIDDEN = (
    # en
    r"safe",
    r"safely",
    r"low[ -]risk",
    r"no[ -]risk",
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
    # es
    r"segur[oa]s?",
    r"sin riesgo",
    r"(?:bajo|riesgo bajo|riesgo nulo)",
    r"inofensiv[oa]s?",
    r"garantizad[oa]s?|garantiza|garantizar",
    # it
    r"sicur[oa]|sicuri|sicure",
    r"senza rischi[oa]",
    r"(?:basso rischio|rischio basso)",
    r"innocu[oa]|innocui|innocue",
    r"garantit[oa]|garantiti|garantite|garantisce|garantire",
)
# The bare Spanish "bajo" is plain "under"; only the risk phrases claim anything.
FORBIDDEN = tuple(p for p in FORBIDDEN if p != r"(?:bajo|riesgo bajo|riesgo nulo)") + (
    r"bajo riesgo",
    r"riesgo (?:bajo|nulo)",
)
CLAIM = re.compile(r"(?<!\w)(?:" + "|".join(FORBIDDEN) + r")(?!\w)", re.IGNORECASE)

# (path relative to src/farmcore, matched text lowercased) -> why this use claims nothing.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "apps/advisor/routes.py",
        "low risk",
    ): "label of the model's own 'low' risk band (Gubler-Thomas index tier), not a safety claim",
    (
        "apps/planning/templates/planning/index.html",
        "safe",
    ): "says the opposite: an unknown availability 'stays unknown rather than safe'",
    (
        "apps/advisor/engine/config.py",
        "low risk",
    ): "source note on a parameter: the model's 'low' risk band, not a claim about a farm",
    (
        "apps/advisor/engine/decision.py",
        "no risk",
    ): "engine verdict string, engine-owned (crop-models 03); reword there, see PR notes",
    (
        "apps/advisor/engine/daily.py",
        "no risk",
    ): "engine verdict string, engine-owned (crop-models 03); reword there, see PR notes",
    (
        "apps/advisor/models.py",
        "no risk",
    ): "compares against the engine verdict string above; changes with it",
    (
        "apps/onboarding/importers/parsers.py",
        "safely",
    ): "a parser says it cannot read a file safely; it is a refusal, not a claim",
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


def claims_in(root: Path) -> set[tuple[str, str]]:
    """(relative path, matched text) of every forbidden phrase under `root`; no allowlist."""
    found: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*")):
        if path.suffix == ".html":
            texts = [_template_text(path)]
        elif path.suffix == ".py":
            texts = _python_strings(path)
        else:
            continue
        rel = path.relative_to(root).as_posix()
        found.update((rel, m.group(0).lower()) for t in texts for m in CLAIM.finditer(t))
    return found


def test_shipped_text_makes_no_safety_or_certainty_claim():
    stray = claims_in(SRC) - ALLOWED.keys()

    assert not stray, f"forbidden claim wording (fix it, or allowlist it with a reason): {stray}"


def test_every_allowlisted_use_still_exists():
    assert ALLOWED.keys() <= claims_in(SRC), "stale allowlist entry: remove it"


def test_the_scan_fails_on_a_planted_phrase_in_every_locale(tmp_path):
    planted = {
        "en.html": "<p>This spray plan is safe for bees.</p>",
        "en2.html": "<p>Low-risk week; results are guaranteed.</p>",
        "fr.html": "<p>Ce traitement est sans danger et garanti.</p>",
        "es.html": "<p>Aplicación sin riesgo, resultado garantizado.</p>",
        "it.html": "<p>Trattamento sicuro e innocuo.</p>",
    }
    for name, body in planted.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    (tmp_path / "msg.py").write_text('MESSAGE = "harmless to bees"\n', encoding="utf-8")

    hit_files = {rel for rel, _ in claims_in(tmp_path)}

    assert hit_files == {*planted, "msg.py"}


def test_the_scan_ignores_security_and_css_senses_of_the_word(tmp_path):
    (tmp_path / "ok.html").write_text(
        '<div class="pb-[env(safe-area-inset-bottom)] motion-safe:x">{{ v|safe }} '
        "Unsafe path; safeguard.</div><!-- safe -->",
        encoding="utf-8",
    )

    assert claims_in(tmp_path) == set()


# --- assessments never rank products ---------------------------------------------------------

RANKING = re.compile(
    r"(?<!\w)(?:best|better|top|rank\w*|score\w*|preferred|first choice|recommended first|"
    r"most effective|#1)(?!\w)",
    re.IGNORECASE,
)
RANK_KEYS = re.compile(r"rank|score|order|priority|preference|best|position|weight", re.IGNORECASE)
TANK_MIX = [
    {"name": "Sulphur", "product_type": "contact", "targets": ["powdery"]},
    {"name": "Copper", "product_type": "contact", "targets": ["downy"]},
]


def _spray_advice(session, farm):
    parcel = make_parcel(session, farm, 1, area="1", props={})
    session.add(Asset(farm_id=farm.id, kind="station", name="Roof station"))
    result = {
        "decision": "spray",
        "headline": "Spray: infection risk after 11.0 mm rain",
        "reasons": [],
        "products": TANK_MIX,
        "next_window": None,
        "protected_until": None,
        "flags": [],
        "mm_since_spray": None,
        "incubation_pct": None,
        "passes": 1,
        "treatments": 2,
    }
    env = assessments.envelope(
        spray_assessment.KIND,
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=datetime.now(UTC) - timedelta(minutes=10),
        status="evaluable",
        config_version={"id": None, "status": "unreviewed", "accuracy_claim": False},
        input_snapshot={},
        result=result,
    )
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=env.evaluated_at,
        decision="spray",
        reasons=[],
        products=TANK_MIX,
        props={"headline": result["headline"], "assessment_status": "evaluable", "passes": 1},
        assessment=env,
    )
    session.add(row)
    session.flush()
    return parcel, env, row


def _keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_spray_assessment_output_never_ranks_products(client, session, settings):
    clock_row(session, settings).mode = "live"  # tests default to a 2024 replay clock
    farm = seed.ensure_default_farm(session, settings)
    parcel, env, row = _spray_advice(session, farm)
    session.commit()

    advice_json = client.get(f"/advisor/parcels/{parcel.id}.json").json()
    pages = {
        path: html.unescape(client.get(path).text)
        for path in (f"/advisor/parcels/{parcel.id}", "/advisor/")
    }

    # Envelope, stored advice and advice.json carry products as plain entries, in the order the
    # engine composed the tank mix, with no rank, score or preference field anywhere.
    for products in (env.result["products"], advice_json["products"], row.products):
        assert [p["name"] for p in products] == ["Sulphur", "Copper"]
        assert not any(RANK_KEYS.search(k) for p in products for k in p)
    # Pages name the products and no ranking wording appears near them.
    for path, text in pages.items():
        assert not RANKING.search(re.sub(r"<[^>]+>", " ", text)), path
    assert "Sulphur + Copper" in pages[f"/advisor/parcels/{parcel.id}"]
