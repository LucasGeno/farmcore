"""Advisor assistant tools on the seeded Scene B farm: `advisor_today` and
`advisor_parcel_status(parcel)`. Answers quote the stored advice (decision + headline); a tool
never decides. Expected decisions come from the hero scenes (test_job.py): 10 Apr 06:00 is
`spray` everywhere, and a copper spray at 05:30 + `run_now` protects only that parcel."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from farmcore.apps.advisor import tools
from farmcore.core import jobs
from farmcore.core.models.canonical import Advice, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local
from tests.apps.chat.assistant.toolkit import CHAT, assistant_reads, ctx_at, keys, write_counts

pytestmark = pytest.mark.data

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

NOW = "2024-04-10 06:00"
# The facts the model chooser may send, from the tool contract plus the "parcel" label ("P6
# Peyrot") and the "parcels" wrapper of the per-parcel list. Literal on purpose: widening the
# allowlist is a test change.
# Every advice also names its assessment status and parameter version (`id`, review `status`).
VERSION_FACTS = {"assessment_status", "config_version", "id", "status"}
TODAY_FACTS = {"parcels", "parcel", "decision", "headline", "advised"} | VERSION_FACTS
STATUS_FACTS = {"decision", "headline", "protected_until", "mm_since_spray", "next_window"}
STATUS_FACTS |= {"products", "flags", "parcel"} | VERSION_FACTS
# The unknown-parcel error fact : {"error": "unknown parcel", "parcels": [...]}.
ERROR_FACTS = {"error", "parcels"}


@pytest.fixture
def hero(session, settings, session_factory, manifests, seeded, step):
    """10 Apr 06:00: P5 and P6 sprayed with copper at 05:30 and confirmed, advice re-run."""
    seeded("B")
    step("2024-04-08 06:00")
    step(NOW)
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 05:30"), "copper-contact")
    run = jobs.run_now(session_factory, manifests, "advisor.evaluate", settings)
    assert run is not None and run.ok
    session.expire_all()


def stored(session, alias: str) -> Advice:
    return session.scalar(
        select(Advice)
        .join(Parcel, Parcel.id == Advice.parcel_id)
        .where(Parcel.alias == alias)
        .order_by(Advice.evaluated_at.desc(), Advice.id.desc())
        .limit(1)
    )


def test_today_lists_the_parcels_to_spray_with_the_advisors_headline(session, settings, hero):
    ctx = ctx_at(session, settings, NOW)

    result = tools.today(ctx)

    by_parcel = {p["parcel"]: p for p in result.facts["parcels"]}
    assert len(by_parcel) == 10
    assert by_parcel["P5 Les Granges"]["decision"] == "protected"
    assert by_parcel["P6 Peyrot"]["decision"] == "protected"
    assert {p["decision"] for k, p in by_parcel.items() if k.split()[0] not in ("P5", "P6")} == {
        "spray"
    }
    assert by_parcel["P1 Le Plateau"]["headline"] == stored(session, "P1").props["headline"]
    assert keys(result.facts) <= TODAY_FACTS
    lines = result.text.splitlines()
    assert len(lines) <= 3
    assert lines[0] == "8 parcels to spray: P1, P2, P3, P4, P7, P8, P9, P10."
    assert lines[1] == stored(session, "P1").props["headline"]
    assert lines[2] == "Others: 2 protected."
    assert result.links == (("Open map", "/fields/"),)


def test_today_with_few_parcels_to_spray_gives_one_card_button_each(session, settings, hero):
    # Step back: advice evaluated after the clock is hidden; 8 Apr is `none` everywhere.
    ctx = ctx_at(session, settings, "2024-04-09 06:00")

    result = tools.today(ctx)

    assert {p["decision"] for p in result.facts["parcels"]} == {"none"}
    assert result.text.splitlines()[0] == "Nothing to spray today."
    assert result.links == ()


def test_today_in_french(session, settings, hero):
    ctx = ctx_at(session, settings, NOW, lang="fr")

    lines = tools.today(ctx).text.splitlines()

    assert lines[0] == "8 parcelles à traiter : P1, P2, P3, P4, P7, P8, P9, P10."
    # The second line is built in French from the advice, not the stored English headline.
    assert lines[1] != stored(session, "P1").props["headline"]
    assert lines[1].startswith("À traiter") and "avr." in lines[1]
    assert lines[2] == "Autres : 2 protégées."


def test_parcel_status_in_french_builds_the_detail_from_the_facts(session, settings, hero):
    ctx = ctx_at(session, settings, NOW, lang="fr")
    row = stored(session, "P6")

    result = tools.parcel_status(ctx, parcel="Peyrot")

    lines = result.text.splitlines()
    until = row.protected_until.astimezone(ctx.tz)
    mm = f"{row.props['mm_since_spray']:.1f}".replace(".", ",")
    assert lines == [
        "P6 Peyrot : protégée.",
        f"Protégée jusqu'au {until.day} avr. {until:%H:%M} · {mm} mm de pluie depuis le "
        "traitement.",
    ]
    assert result.facts["headline"] == row.props["headline"]  # the facts are unchanged


def test_parcel_status_in_french_of_a_parcel_to_spray(session, settings, hero):
    ctx = ctx_at(session, settings, NOW, lang="fr")
    row = stored(session, "P1")

    lines = tools.parcel_status(ctx, parcel="Le Plateau").text.splitlines()

    assert lines[0] == "P1 Le Plateau : non protégée, à traiter."
    assert row.props["headline"] not in lines
    start = row.next_window_start.astimezone(ctx.tz)
    assert lines[1].startswith("À traiter") and f"{start.day} avr. {start:%H:%M}" in lines[1]


@pytest.mark.parametrize("said", ["Peyrot", "peyrot", "P6", "PÉYROT"])
def test_parcel_status_quotes_the_stored_advice(session, settings, hero, said):
    ctx = ctx_at(session, settings, NOW)
    row = stored(session, "P6")

    result = tools.parcel_status(ctx, parcel=said)

    assert result.facts["parcel"] == "P6 Peyrot"
    assert result.facts["decision"] == row.decision == "protected"
    assert result.facts["headline"] == row.props["headline"]
    assert result.facts["protected_until"] == row.protected_until.astimezone(ctx.tz).isoformat()
    assert result.facts["mm_since_spray"] == row.props["mm_since_spray"]
    assert result.facts["flags"] == row.props["flags"]
    assert keys(result.facts) <= STATUS_FACTS
    assert result.text.splitlines() == ["P6 Peyrot: protected.", row.props["headline"]]
    assert result.links == (("Open card P6", f"/advisor/parcels/{row.parcel_id}"),)


def test_parcel_status_of_a_parcel_to_spray(session, settings, hero):
    ctx = ctx_at(session, settings, NOW)
    row = stored(session, "P1")

    result = tools.parcel_status(ctx, parcel="Le Plateau")

    assert result.facts["decision"] == "spray"
    assert result.facts["products"] == [p["name"] for p in row.products]
    start, end = result.facts["next_window"]
    assert start == row.next_window_start.astimezone(ctx.tz).isoformat()
    assert end == row.next_window_end.astimezone(ctx.tz).isoformat()
    assert result.text.splitlines()[0] == "P1 Le Plateau: not protected, spray needed."


def test_parcel_status_is_bounded_by_the_clock(session, settings, hero):
    ctx = ctx_at(session, settings, "2024-04-09 06:00")

    result = tools.parcel_status(ctx, parcel="P6")

    assert result.facts["decision"] == "none"


def test_parcel_status_before_any_advice(session, settings, seeded):
    seeded("B")
    ctx = ctx_at(session, settings, NOW)

    result = tools.parcel_status(ctx, parcel="P6")

    assert result.facts == {"parcel": "P6 Peyrot", "decision": "none", "headline": None}
    assert result.text == "P6 Peyrot: no advice yet."


def test_parcel_status_unknown_parcel(session, settings, hero):
    ctx = ctx_at(session, settings, NOW)

    result = tools.parcel_status(ctx, parcel="Le Château")

    assert result.facts["error"] == "unknown parcel"
    assert "P6 Peyrot" in result.facts["parcels"]
    assert keys(result.facts) <= ERROR_FACTS


def test_tools_read_as_the_assistant_and_write_nothing(session, settings, hero):
    before = write_counts(session)
    ctx = ctx_at(session, settings, NOW)

    tools.today(ctx)
    tools.parcel_status(ctx, parcel="P5")

    reads = assistant_reads(session)
    assert reads.count((f"chat:{CHAT}", "advice", "advice:read")) >= 2
    assert write_counts(session) == before


def test_declared_tools():
    names = [(t.name, t.scope, t.params) for t in tools.TOOLS]
    assert names == [
        ("advisor_today", "advice:read", {}),
        ("advisor_parcel_status", "advice:read", {"parcel": "STRING"}),
    ]


# --- No advice is not "nothing to spray" (review R2) ----------------------------------------


def _without_advice(session, *aliases: str) -> None:
    """Delete every advice row of `aliases`: those parcels were never evaluated."""
    from sqlalchemy import delete

    ids = select(Parcel.id).where(Parcel.alias.in_(aliases))
    session.execute(delete(Advice).where(Advice.parcel_id.in_(ids)))
    session.commit()


def _all_latest(session, decision: str) -> None:
    from sqlalchemy import update

    session.execute(update(Advice).values(decision=decision))
    session.commit()


@pytest.mark.parametrize(
    ("lang", "first"),
    [
        ("en", "No advice yet: the advisor has not evaluated any of the 10 parcels up to now."),
        ("fr", "Pas encore de conseil : aucune des 10 parcelles n'a été évaluée à cette heure."),
    ],
)
def test_today_before_any_advice_says_no_advice_not_nothing_to_spray(
    session, settings, seeded, lang, first
):
    seeded("B")
    before = write_counts(session)

    result = tools.today(ctx_at(session, settings, NOW, lang=lang))

    assert result.text == first
    assert {(p["decision"], p["advised"]) for p in result.facts["parcels"]} == {("none", False)}
    assert result.links == ()
    assert write_counts(session) == before


def test_advice_evaluated_after_the_clock_counts_as_no_advice(session, settings, hero):
    # The hero's first advice is evaluated on 8 Apr 06:00: on 7 Apr nothing is visible yet.
    result = tools.today(ctx_at(session, settings, "2024-04-07 06:00"))

    assert result.text.startswith("No advice yet")
    assert not any(p["advised"] for p in result.facts["parcels"])


def test_evaluated_none_everywhere_keeps_nothing_to_spray(session, settings, hero):
    result = tools.today(ctx_at(session, settings, "2024-04-09 06:00"))

    assert result.text == "Nothing to spray today."
    assert all(p["advised"] for p in result.facts["parcels"])


@pytest.mark.parametrize(
    ("lang", "lines"),
    [
        ("en", ["No spray advised on the 8 evaluated parcels.", "Others: 2 awaiting advice."]),
        (
            "fr",
            [
                "Aucun traitement conseillé sur les 8 parcelles évaluées.",
                "Autres : 2 sans conseil.",
            ],
        ),
    ],
)
def test_mixed_evaluated_and_missing_never_reads_as_an_all_clear(
    session, settings, hero, lang, lines
):
    _without_advice(session, "P9", "P10")

    result = tools.today(ctx_at(session, settings, "2024-04-09 06:00", lang=lang))

    assert result.text.splitlines() == lines
    by = {p["parcel"].split()[0]: p for p in result.facts["parcels"]}
    assert (by["P9"]["advised"], by["P1"]["advised"]) == (False, True)


def test_spraying_with_some_parcels_missing_counts_them(session, settings, hero):
    _without_advice(session, "P9", "P10")

    lines = tools.today(ctx_at(session, settings, NOW)).text.splitlines()

    assert lines[0] == "6 parcels to spray: P1, P2, P3, P4, P7, P8."
    assert lines[2] == "Others: 2 protected, 2 awaiting advice."


ALL = "P1, P2, P3, P4, P5, P6, P7, P8, P9, P10"


@pytest.mark.parametrize(
    ("lang", "first"),
    [
        ("en", f"10 parcels at risk, waiting for a spray window: {ALL}."),
        ("fr", f"10 parcelles à risque, en attente d'une fenêtre de traitement : {ALL}."),
    ],
)
def test_an_all_wait_farm_leads_with_the_parcels_at_risk(session, settings, hero, lang, first):
    # `wait` is a parcel at risk whose spray window is not open yet: never "nothing to spray".
    _all_latest(session, "wait")

    result = tools.today(ctx_at(session, settings, NOW, lang=lang))

    lines = result.text.splitlines()
    assert lines[0] == first and len(lines) <= 3
    assert not any("Others" in line or "Autres" in line for line in lines)
    assert result.links == (("Open map", "/fields/"),)
