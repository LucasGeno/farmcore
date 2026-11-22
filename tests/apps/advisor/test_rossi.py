"""The Rossi et al. 2008 downy mildew primary infection model (`engine/rossi.py`), shown beside
the 3-10 rule on the parcel card.

Failure model (each named failure has the test that catches it):
- F1 a coefficient or stage rule drifts from the paper (HT, DOR, germination, survival, release,
  infection or incubation): the viticolR oracle season and the paper's own numbers;
- F2 more wetness or more warmth makes the model less ready to infect (a sign or bound slip),
  or it progresses at or below 0 °C: property tests;
- F3 missing weather (the advisor's usual 1 March series start, a variable missing, rows after
  now) gives a run that looks complete or reads the future;
- F4 the card hides the model's validation level, presents it as the decision, or changes the
  decision: HTTP smoke.

The oracle is viticolR 0.0.4 (GPL, run out of process; tests/fixtures/oracles/). It is an R
implementation, not ground truth: where it and the paper differ, FarmCore follows the paper.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from farmcore.apps.advisor.advice import Advice
from farmcore.apps.advisor.engine import rossi
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Asset
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather.hourly import HourlyObs
from tests.apps.operations.conftest import make_parcel
from tests.apps.stations.synth import observe

ORACLES = Path(__file__).parents[2] / "fixtures" / "oracles"
START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 7, 31, 23, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _season() -> tuple[list[HourlyObs], dict[datetime, float]]:
    """The oracles' synthetic hourly season and its declared leaf-wetness minutes."""
    with (ORACLES / "inputs" / "season-2025.csv").open() as f:
        rows = list(csv.DictReader(f))
    obs = [
        HourlyObs(_utc(r["time_utc"]), float(r["rain_mm"]), float(r["temp_c"]), float(r["rh_pct"]))
        for r in rows
    ]
    return obs, {_utc(r["time_utc"]): float(r["leaf_wet_min"]) for r in rows}


def _stages() -> tuple[dict, dict[int, dict[str, datetime | None]]]:
    fixture = json.loads((ORACLES / "viticolr" / "season-2025.json").read_text())
    by_cohort: dict[int, dict[str, datetime | None]] = {}
    for s in fixture["outputs"]["primary_infection_stages"]:
        hour = _utc(s["hour"]) if s["hour"] else None
        by_cohort.setdefault(s["cohort"], {})[s["primary_infection_stage"]] = hour
    return fixture, by_cohort


def test_reproduces_the_viticolr_oracle_where_both_read_the_same_wetness():
    """HT and DOR every 30 days match to 1e-9. Every cohort germinates in the oracle's hour. The
    sporangia die in its hour or one later: the oracle crosses SUS = 1 one hour earlier, as it
    does for incubation (where the sums start, ε/φ/ι, is ambiguous in the print; FarmCore starts
    each at the stage's own hour). Infections: viticolR ignores the season's leaf-wetness column
    and derives wetness itself, so its wet periods, and with them its release and zoospore-death
    hours, differ. Where both release and disperse in the same hour, infection is the same hour
    and the symptoms window within 1 h; the rest is listed, so any drift fails, and each
    difference is traced to the reading that causes it (below)."""
    obs, wet = _season()
    fixture, oracle = _stages()
    days = fixture["outputs"]["end_of_day"]
    for day in [*days[::30], days[-1]]:
        now = datetime.fromisoformat(day["date"]).replace(hour=23, tzinfo=UTC)
        partial = rossi.run(obs, START, now, measured=wet)
        assert partial.ht == pytest.approx(day["Hyd_t"], abs=1e-9), day["date"]
        assert rossi.dor(partial.ht) == pytest.approx(day["PMO"], abs=1e-9), day["date"]

    run = rossi.run(obs, START, END, measured=wet)
    assert run.complete and run.wetness == "measured" and run.season == "ended"
    # Zoospores released but not dispersed die at the first dry hour after release.
    dry = sorted(ts for ts, minutes in wet.items() if minutes < 30)
    stranded = [c for c in run.cohorts if c.released and not c.dispersed]
    assert stranded
    for c in stranded:
        assert c.zoospores_dead == next(ts for ts in dry if ts > c.released)
    by_geo = {s["GEO_h"]: s for s in oracle.values() if s["GEO_h"]}
    assert len(run.cohorts) == 28  # the wet events inside the primary inoculum season
    for c in run.cohorts:
        theirs = by_geo[c.germinated]
        assert timedelta(0) <= c.sporangia_dead - theirs["SUS_death_h"] <= HOUR

    ours = {c.infection: c for c in run.infections}
    theirs = {s["ZIN_ind"]: s for s in oracle.values() if s["ZIN_ind"]}
    shared = ours.keys() & theirs.keys()
    assert sorted(shared) == [
        datetime(2025, 5, 26, 23, tzinfo=UTC),
        datetime(2025, 6, 16, 0, tzinfo=UTC),
        datetime(2025, 7, 15, 23, tzinfo=UTC),
        datetime(2025, 7, 20, 23, tzinfo=UTC),
    ]
    for hour in shared:
        c, o = ours[hour], theirs[hour]
        assert (c.released, c.dispersed) == (o["ZRE_ind"], o["ZDI_ind"])
        assert timedelta(0) <= c.symptoms_from - o["INC_h_lower"] <= HOUR
        assert timedelta(0) <= c.symptoms_to - o["INC_h_upper"] <= HOUR
    # viticolR's wetter nights carry these two infections past the hours wet here.
    assert sorted(theirs.keys() - shared) == [
        datetime(2025, 4, 17, 5, tzinfo=UTC),
        datetime(2025, 5, 2, 4, tzinfo=UTC),
    ]
    # Here zoospores wait for the next rain's wet hours; viticolR releases them earlier,
    # in hours it counts wet, where they die before rain disperses them.
    assert [h.strftime("%m-%d %H") for h in sorted(ours.keys() - shared)] == [
        "05-17 00",
        "05-31 23",
        "06-05 23",
        "06-10 23",
        "06-20 23",
        "06-25 23",
    ]

    # Where the differences come from (paper reading first, one oracle choice switched at a
    # time; FarmCore ships only the paper's reading). viticolR reads wetness as litter moisture
    # M for release: the 5 later FarmCore-only infections go (their zoospores are released in
    # moist dry-leaf hours and die before rain). It also counts release over all hours since
    # germination (a cumulative M count and mean temperature) instead of per wet period: the
    # 17 May infection goes. And M for infection: its 17 Apr and 2 May infections appear.
    def infections(reading: rossi.Reading) -> set[datetime]:
        return {
            c.infection
            for c in rossi.run(obs, START, END, measured=wet, reading=reading).infections
        }

    litter = rossi.Reading(release_wetness="litter")
    assert infections(litter) == shared | {datetime(2025, 5, 17, 0, tzinfo=UTC)}
    cumulative = replace(litter, release_window="cumulative")
    assert infections(cumulative) == shared
    viticolr = replace(cumulative, infection_wetness="litter")
    emulated = rossi.run(obs, START, END, measured=wet, reading=viticolr)
    assert {c.infection for c in emulated.infections} == theirs.keys()
    for c in emulated.cohorts:  # every stage up to infection, every cohort: the same hour
        o = by_geo[c.germinated]
        assert (c.released, c.dispersed, c.infection) == (o["ZRE_ind"], o["ZDI_ind"], o["ZIN_ind"])


def _scenario(t_c: float, storm_hours: int) -> tuple[list[HourlyObs], datetime]:
    """Constant `t_c` and 85 % humidity (moist for HT, dry for wetness) from 1 January; one
    1 mm rain hour at HT 1.5 starts a cohort; `storm_hours` of 0.5 mm rain start 12 h after
    it germinates. Returns the series and the cohort's start."""
    f_t = 1330.1 - 116.19 * t_c + 2.6256 * t_c**2  # hours of moist T for HT += 1
    event = START + math.ceil(1.5 * f_t) * HOUR
    storm = event + (math.ceil(f_t) + 11) * HOUR  # germination is the ceil(f_t)-th hour
    end = storm + 40 * 24 * HOUR
    rows, ts = [], START
    while ts <= end:
        rain = 1.0 if ts == event else 0.5 if storm <= ts < storm + storm_hours * HOUR else 0.0
        rows.append(HourlyObs(ts, rain, t_c, 85.0))
        ts += HOUR
    return rows, event


def _first(t_c: float, storm_hours: int) -> rossi.Cohort:
    rows, event = _scenario(t_c, storm_hours)
    run = rossi.run(rows, START, rows[-1].ts)
    return next(c for c in run.cohorts if c.start == event)


def test_the_papers_numbers():
    """The PIS bounds are 3 % and 97 % of the dose (Eq. 3 at HT 1.3 and 8.6). At a constant
    20 °C: germination takes 1330.1 - 116.19·20 + 2.6256·400 = 56.5 moist hours, release
    exp(18.612/20) = 2.5 wet hours, infection 60/20 = 3 wet hours from dispersal, and symptoms
    5.3 to 6.9 days after infection (45.1 - 69 + 29.2; 59.9 - 91 + 38)."""
    assert rossi.dor(1.3) == pytest.approx(0.03, abs=0.001)
    assert rossi.dor(8.6) == pytest.approx(0.97, abs=0.001)
    c = _first(20.0, 12)
    assert c.germinated - c.start == 56 * HOUR  # the 57th hour, counting the first, reaches 1
    assert c.released == c.dispersed  # released in a rain hour: dispersed at once
    assert c.released - (c.germinated + 12 * HOUR) == 2 * HOUR  # 3rd wet hour of the storm
    assert c.infection - c.dispersed == 2 * HOUR  # 3 wet hours at 20 °C = 60 °C·h
    assert (c.symptoms_from - c.infection) / timedelta(days=1) == pytest.approx(5.3, abs=1 / 24)
    assert (c.symptoms_to - c.infection) / timedelta(days=1) == pytest.approx(6.9, abs=1 / 24)
    # Eq. 7: zoospores released in a wet hour without rain die at the first dry hour after it.
    rows, event = _scenario(20.0, 0)
    mist = c.germinated + 12 * HOUR
    misty = [replace(o, rh_pct=95.0) if mist <= o.ts < mist + 3 * HOUR else o for o in rows]
    m = next(c for c in rossi.run(misty, START, rows[-1].ts).cohorts if c.start == event)
    assert (m.released, m.dispersed, m.zoospores_dead) == (mist + 2 * HOUR, None, mist + 3 * HOUR)


@settings(max_examples=30, deadline=None)
@given(
    t_c=st.integers(20, 44).map(lambda half: half / 2),  # 10-22 °C, where every rate rises
    storm=st.integers(1, 23),
)
def test_more_wetness_or_warmth_never_makes_infection_less_likely(t_c, storm):
    shorter, longer = _first(t_c, storm), _first(t_c, storm + 1)
    if shorter.infection is not None:  # wetness: one more wet hour keeps the infection hour
        assert longer.infection == shorter.infection
    # Warmth, near the threshold too: with the same storm, a warmer cohort that the cooler one
    # infects in time also infects, releases and infects no later after germinating, and its
    # incubation is no longer.
    cooler, warmer = shorter, _first(t_c + 0.5, storm)
    if cooler.infection is not None:
        assert warmer.infection is not None
        assert warmer.released - warmer.germinated <= cooler.released - cooler.germinated
        assert warmer.infection - warmer.germinated <= cooler.infection - cooler.germinated
        assert warmer.symptoms_from - warmer.infection <= cooler.symptoms_from - cooler.infection
    # A germinated cohort's wet period at -1 °C releases no zoospores (assumed: no release
    # at or below 0 °C; the printed rule would release at once).
    rows, event = _scenario(20.0, 0)
    germinated = _first(20.0, 0).germinated
    cold = [
        replace(o, t_c=-1.0, rh_pct=95.0) if germinated < o.ts <= germinated + 10 * HOUR else o
        for o in rows
    ]
    chilled = next(c for c in rossi.run(cold, START, rows[-1].ts).cohorts if c.start == event)
    assert chilled.germinated == germinated and chilled.released is None
    # At or below 0 °C nothing matures, whatever the rain.
    frozen = rossi.run(
        [
            HourlyObs(START + i * HOUR, 1.0 if i % 50 == 0 else 0.0, -t_c / 10, 95.0)
            for i in range(2000)
        ],
        START,
        START + 1999 * HOUR,
    )
    assert frozen.ht == 0 and frozen.cohorts == () and frozen.season == "not started"


def test_missing_weather_makes_a_limited_run_and_the_future_is_never_read():
    obs, _ = _season()
    full = rossi.run(obs, START, END)
    assert full.complete and full.missing_hours == 0 and full.wetness == "estimated"

    march = datetime(2025, 3, 1, tzinfo=UTC)
    late = rossi.run([o for o in obs if o.ts >= march], START, END)
    assert not late.complete and late.missing_hours == 59 * 24
    assert late.ht < full.ht

    may = datetime(2025, 5, 1, tzinfo=UTC)
    gap = rossi.run(
        [replace(o, rh_pct=None) if may <= o.ts < may + 24 * HOUR else o for o in obs], START, END
    )
    assert not gap.complete and gap.missing_hours == 24

    now = datetime(2025, 5, 20, tzinfo=UTC)
    flooded = [replace(o, rr1_mm=5.0, rh_pct=99.0) if o.ts > now else o for o in obs]
    assert rossi.run(flooded, START, now) == rossi.run([o for o in obs if o.ts <= now], START, now)

    # A missing input pauses every stage: with humidity missing at the hour this cohort would
    # infect (sensor wetness kept), it infects one hour later and the trace says why. Each step
    # carries its inputs' sources and the wetness origin, in a partial run too.
    _, wet = _season()
    hole = datetime(2025, 5, 17, 0, tzinfo=UTC)
    cohort_start = datetime(2025, 4, 21, 18, tzinfo=UTC)
    sources = {o.ts: {"t_c": "st-1 (on-farm)", "rh_pct": "st-1 (on-farm)"} for o in obs}
    holed = rossi.run(
        [replace(o, rh_pct=None) if o.ts == hole else o for o in obs],
        START,
        END,
        measured=wet,
        sources=sources,
    )
    whole = rossi.run(obs, START, END, measured=wet)
    before = next(c for c in whole.cohorts if c.start == cohort_start)
    after = next(c for c in holed.cohorts if c.start == cohort_start)
    assert before.infection == hole and after.infection == hole + HOUR
    assert not holed.complete and holed.missing_hours == 1
    step = next(s for s in after.trace if s.stage == "infection")
    assert (step.ts, step.missing_before, step.wetness) == (hole + HOUR, 1, "measured")
    assert step.sources == {"t_c": "st-1 (on-farm)", "rh_pct": "st-1 (on-farm)"}
    assert [s.stage for s in after.trace][:4] == ["start", "germinated", "released", "dispersed"]
    assert holed.sources == ("st-1 (on-farm)",)

    with pytest.raises(ValueError, match="timezone-aware"):
        rossi.run(obs, datetime(2025, 1, 1), END)


def test_the_parcel_card_shows_the_rossi_infections_beside_an_unchanged_decision(
    client, session, settings
):
    now = datetime(2025, 6, 1, tzinfo=UTC)
    obs = [o for o in _season()[0] if o.ts <= now]
    farm = ensure_default_farm(session, settings)
    station = Asset(farm_id=farm.id, kind="station", name="Synthetic season", props={})
    session.add(station)
    session.flush()
    observe(
        session,
        station,
        [(o.ts, v, getattr(o, v)) for o in obs for v in ("rr1_mm", "t_c", "rh_pct")],
    )
    parcel = make_parcel(session, farm, 305, area="1.0", props={})
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=now,
            decision="wait",
            props={"headline": "Stored advice", "assessment_status": "evaluable"},
            reasons=[],
        )
    )
    set_replay(session, now, settings)
    session.commit()

    page = client.get(f"/advisor/parcels/{parcel.id}", params={"farm": farm.id})

    assert page.status_code == 200
    body = page.text
    section = body[body.index('id="rossi"') : body.index("</section>", body.index('id="rossi"'))]
    assert 'data-model="advisor.downy_rossi"' in body
    assert 'data-validation="unvalidated"' in section
    assert "still uses the 3-10 rule" in section
    expected = [c.infection.isoformat() for c in rossi.run(obs, START, now).infections]
    assert expected  # the season has infections by 1 June
    assert [f'data-infection="{h}"' in section for h in expected] == [True] * len(expected)
    assert "data-rossi-trace" in section and "(on-farm)" in section  # each step's source, tier
    assert 'data-decision="wait"' in body
