"""Daily reference evapotranspiration (ET0).

Failure model (one test each):
- Formula: FAO-56 Penman-Monteith miscomputed (radiation, humidity or wind height wrong), or pyet
  and refet disagree, on the FAO-56 worked examples (published values, not recomputed).
- Missing inputs: a day lacking any hour of an input (or holding a NaN, or with no elevation)
  gets a number (zero, or one from the hours it has) instead of unknown; a calm, dark or dry
  hour (a true zero) is taken as missing.
- Choice and label: a computed value replaces an official one the farm holds for that day; an
  official value is read before its receipt arrived (as of); the source, licence, attribution
  or forecast flag is missing or wrong; forecast radiation is not stored or not used.
- Import: a bad grid file stores part of itself, a farm outside the grid gets a neighbour's
  values, a re-import duplicates the evidence.
"""

from __future__ import annotations

import gzip
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import refet
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select

from farmcore.apps.forecast import provider as open_meteo
from farmcore.apps.forecast import sources
from farmcore.apps.weather_meteofrance import etp
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.farm_location import LOCATION_KEY
from farmcore.core.models.weather import WeatherObservation, WeatherReceipt
from farmcore.core.seed import ensure_default_farm
from farmcore.core.weather import et0, merge
from farmcore.core.weather.contract import (
    BBox,
    Capabilities,
    Fetched,
    Licence,
    ObservationValue,
    Point,
    Request,
    Station,
    Tier,
    TimeRange,
)
from farmcore.core.weather.store import receive
from farmcore.core.weather.variables import BY_LEGACY, ET0, SHORTWAVE, VARIABLES

H = timedelta(hours=1)


def _es(t: float) -> float:  # FAO-56 eq. 11, for refet's actual vapour pressure (eq. 17)
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


# Allen et al. (1998), FAO-56 chapter 4: the inputs as printed and the published ET0 before its
# final rounding (Ex. 18: 2.81 + 1.07 = 3.88 mm/day; Ex. 20: 3.17 + 1.39 = 4.56 mm/day).
EXAMPLES = [
    pytest.param(
        # Uccle, 6 July (J = 187): wind 10 km/h at 10 m, Rs = 22.07 MJ m-2 day-1 from n = 9.25 h
        et0.Inputs(date(2026, 7, 6), 12.3, 21.5, 63, 84, None, 22.07, 50.80, 100),
        2.78,
        3.88,
        0.01,
        id="ex18-uccle-daily",
    ),
    pytest.param(
        # Lyon, July (J = 196): Tdew = Tmin so RHmax 100 %, RHmin 48 %; u2 2 m/s; Rs from eq. 50
        et0.Inputs(date(2026, 7, 15), 14.8, 26.6, 48, 100, 2.0, 22.29, 45.72, 200),
        None,
        4.56,
        0.02,  # FAO prints RHmin 48 %, rounded from its ea = 1.68 kPa
        id="ex20-lyon-missing-data",
    ),
]


@pytest.mark.parametrize(("inputs", "wind_10m", "published", "within"), EXAMPLES)
def test_fao56_worked_examples_reproduce_and_pyet_agrees_with_refet(
    inputs, wind_10m, published, within
):
    # FAO-56 Ex. 14: 3.2 m/s at 10 m is 2.4 m/s at 2 m; Ex. 18: 2.78 m/s is 2.078 m/s.
    assert et0.wind_2m(3.2) == pytest.approx(2.4, abs=0.01)
    if wind_10m is not None:
        inputs = et0.Inputs(**{**inputs.__dict__, "u2": et0.wind_2m(wind_10m)})
        # FAO prints 0.748 x 2.78 = 2.078; the product is 2.079
        assert inputs.u2 == pytest.approx(2.078, abs=0.002)

    [computed] = et0.penman_monteith([inputs])

    assert computed == pytest.approx(published, abs=within)
    ea = (_es(inputs.tmin) * inputs.rhmax / 100 + _es(inputs.tmax) * inputs.rhmin / 100) / 2
    asce = refet.Daily(
        tmin=inputs.tmin,
        tmax=inputs.tmax,
        ea=ea,
        rs=inputs.rs,
        uz=inputs.u2,
        zw=2,
        elev=inputs.elevation_m,
        lat=inputs.latitude,
        doy=inputs.day.timetuple().tm_yday,
        method="asce",
        input_units={"lat": "deg"},
    ).eto()[0]
    assert computed == pytest.approx(float(asce), abs=0.01)  # the stated bound


# --- missing inputs: a property ---------------------------------------------------------------

DAY = date(2026, 6, 1)
START = datetime(2026, 6, 1, tzinfo=UTC)
_hour = st.fixed_dictionaries(
    {
        "t_c": st.floats(-10, 45),
        "rh_pct": st.floats(5, 100),
        "ff_kmh": st.sampled_from([0.0]) | st.floats(0, 60),  # calm is a value
        SHORTWAVE: st.sampled_from([0.0]) | st.floats(0, 1000),  # so is night
    }
)


def _hours(rows: list[dict]) -> dict[datetime, merge.MergedHour]:
    hours = {}
    for i, row in enumerate(rows, start=1):
        ts = START + i * H
        values = {**dict.fromkeys(et0.INPUTS), **row}
        hours[ts] = merge.MergedHour(ts, values, dict.fromkeys(row, "s"))
    return hours


@given(
    rows=st.lists(_hour, min_size=24, max_size=24),
    gap=st.tuples(
        st.sampled_from(et0.INPUTS), st.integers(0, 23), st.sampled_from(["drop", "nan"])
    ),
)
def test_a_day_lacking_any_input_hour_is_unknown_never_zero(rows, gap):
    whole, missing = et0.day_inputs(_hours(rows), DAY, 44.9, 50.0)
    [value] = et0.penman_monteith([whole])
    assert missing == () and value is not None and math.isfinite(value) and value >= 0

    name, index, how = gap
    holed = [dict(r) for r in rows]
    if how == "drop":
        del holed[index][name]
    else:
        holed[index][name] = math.nan
    inputs, missing = et0.day_inputs(_hours(holed), DAY, 44.9, 50.0)
    assert missing == (et0.NAMES[name],)
    assert et0.penman_monteith([inputs]) == [None]
    no_elevation, missing = et0.day_inputs(_hours(rows), DAY, 44.9, None)
    assert missing == ("elevation",) and et0.penman_monteith([no_elevation]) == [None]


# --- official first, then computed, labelled ---------------------------------------------------

OPEN = Licence("CC-BY-4.0", "Test national service", True)
UCCLE = Point(50.80, 4.35)


class National:
    name = "national-service"

    def discover_stations(self, bbox: BBox) -> list[Station]:
        return []

    def fetch_observations(self, station: Station, range: TimeRange) -> Fetched:
        raise NotImplementedError

    def fetch_forecast(self, point, models, run) -> Fetched:
        raise NotImplementedError

    def capabilities(self) -> Capabilities:
        return Capabilities(True, False, frozenset(), tier=Tier.NATIONAL)

    def licence(self) -> Licence:
        return OPEN

    def auth_schema(self):
        return ()


def _ex18_day(day: date, skip_radiation_hour: int | None = None) -> list[ObservationValue]:
    """FAO-56 Ex. 18's day as 24 station hours: Tmin/Tmax 12.3/21.5 °C, RHmin/RHmax 63/84 %,
    10 km/h at 10 m, radiation averaging 22.07 MJ m-2 day-1."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    out = []
    for h in range(1, 25):
        end = start + h * H
        warm = 6 < h <= 18
        for name, value, period in (
            ("t_c", "21.5" if warm else "12.3", timedelta(0)),
            ("rh_pct", "63" if warm else "84", timedelta(0)),
            ("ff_kmh", "10", timedelta(minutes=10)),
        ):
            v = BY_LEGACY[name]
            out.append(ObservationValue(v.name, v.unit, Decimal(value), end - period, end))
        if h != skip_radiation_hour:
            rad = Decimal("22.07") / Decimal("0.0864") / 12 * 24 if warm else Decimal(0)
            out.append(
                ObservationValue(SHORTWAVE, "W m-2", rad.quantize(Decimal("0.0001")), end - H, end)
            )
    return out


def _grid(tmp_path, rows: list[str]):
    path = tmp_path / "QUOT_ETPgrille_2026.csv.gz"
    path.write_bytes(
        gzip.compress(("longitude;latitude;date;ETP_grille\n" + "".join(rows)).encode())
    )
    return path


def test_official_et0_is_preferred_labelled_and_read_as_of_else_computed_or_unknown(
    session, settings, tmp_path
):
    farm = ensure_default_farm(session, settings)
    farm.props = {**(farm.props or {}), LOCATION_KEY: {"latitude": 50.80, "longitude": 4.35}}
    session.flush()
    uccle = Station("UCC", "Uccle", 50.80, 4.35)
    observed = [
        *_ex18_day(date(2026, 7, 6)),
        *_ex18_day(date(2026, 7, 7)),
        *_ex18_day(date(2026, 7, 8), skip_radiation_hour=13),
    ]
    at = datetime(2026, 7, 9, tzinfo=UTC)
    receive(
        session,
        National(),
        Fetched(Request("hourly"), at - H, b"obs", observed),
        station=uccle,
        farm_id=farm.id,
    )
    # Open-Meteo's live forecast for 9 July, its radiation included (the 10th has none).
    times = [(at + i * H).strftime("%Y-%m-%dT%H:%M") for i in range(1, 49)]
    warm = [6 < (i - 1) % 24 + 1 <= 18 for i in range(1, 49)]
    rad = round(22.07 / 0.0864 * 2, 4)
    doc = {
        "hourly_units": {"shortwave_radiation": "W/m²"},
        "hourly": {
            "time": times,
            "temperature_2m": [21.5 if w else 12.3 for w in warm],
            "relative_humidity_2m": [63 if w else 84 for w in warm],
            "wind_speed_10m": [10.0] * 48,
            "shortwave_radiation": [rad if w else 0 for w in warm[:24]] + [None] * 24,
        },
    }
    live = open_meteo.OpenMeteo(base_url="https://api.open-meteo.com")
    receive(
        session,
        live,
        Fetched(
            Request(open_meteo.LIVE_DATASET),
            at - timedelta(minutes=5),
            b"forecast",
            open_meteo.forecast_values(sources.parse_hourly(doc["hourly"]), "best_match", None)
            + open_meteo.radiation_values(doc, "best_match", None),
        ),
        farm_id=farm.id,
    )
    # The grid: the cell holding Uccle (4.375, 50.750) has 7 July and an empty 8 July; its
    # neighbour (4.500, 50.750) other values.
    path = _grid(
        tmp_path,
        [
            "4.500;50.750;20260707;9.9\n",
            "4.375;50.750;20260707;4.1\n",
            "4.375;50.750;20260708;\n",
            "4.500;50.750;20260708;9.9\n",
        ],
    )
    imported_at = at + timedelta(hours=6)
    etp.import_file(session, farm.id, path, last_update=date(2026, 7, 21), fetched_at=imported_at)
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "et0")

    days = et0.daily(repo, date(2026, 7, 6), date(2026, 7, 10), UCCLE, imported_at, 100.0)

    method = et0._method()
    grid = (
        "Météo-France, Données ETP spatialisées quotidiennes (data.gouv.fr), Licence Ouverte / "
        "Etalab 2.0, last updated 2026-07-21"
    )
    assert [(d.day.day, d.mm, d.method, d.source, d.forecast, d.missing) for d in days] == [
        (6, pytest.approx(3.88, abs=0.01), "fao56-pm", method, False, ()),
        (7, 4.1, "official", "meteofrance-etp/ETP_QUOT/QUOT_ETPgrille", False, ()),
        (8, None, None, None, False, ("radiation",)),
        # 9 July read at 06:00: its first hours are past and nothing observed them (a
        # forecast never fills a past hour); 10 July has no forecast radiation.
        (9, None, None, None, True, ("temperature", "humidity", "wind", "radiation")),
        (10, None, None, None, True, ("radiation",)),
    ]
    assert (days[1].licence, days[1].attribution) == ("etalab-2.0", grid)
    # A point 50 km north is not in that cell: 7 July is computed there, never the cell's.
    north = et0.daily(
        repo, date(2026, 7, 7), date(2026, 7, 7), Point(51.25, 4.35), imported_at, 100.0
    )
    assert north[0].method == "fao56-pm"
    # Read before the grid arrived (9 July 00:00, the forecast already in hand): 7 July is
    # computed; 9 July is the forecast's.
    before = et0.daily(repo, date(2026, 7, 7), date(2026, 7, 9), UCCLE, at, 100.0)
    assert [(d.mm, d.method, d.forecast) for d in before] == [
        (pytest.approx(3.88, abs=0.01), "fao56-pm", False),
        (None, None, False),
        (pytest.approx(3.88, abs=0.01), "fao56-pm", True),
    ]
    assert et0.daily(repo, date(2026, 7, 6), date(2026, 7, 6), None, at, 100.0)[0].missing == (
        "latitude",
    )


# --- the grid import's failure model ----------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "refusal"),
    [
        (["4.375;50.750;20260707;-0.1\n"], "is not a finite number of mm >= 0"),
        (["4.375;50.750;20260707;NaN\n"], "is not a finite number of mm >= 0"),
        (["4.375;50.750;2026-07-07;1.0\n"], "is not AAAAMMJJ"),
        (["4.375;50.750;20260707;1.0;x\n"], "expected 4 fields"),
        # the nearest cell's centre is 0.075 degrees north: a neighbour, not the pin's cell
        (["4.375;50.875;20260707;1.0\n"], "no grid cell of this file contains the farm pin"),
        ([], "no grid cell of this file contains the farm pin"),
    ],
)
def test_a_bad_grid_file_stores_nothing_and_a_reimport_is_the_same_evidence(
    session, settings, tmp_path, rows, refusal
):
    farm = ensure_default_farm(session, settings)
    farm.props = {**(farm.props or {}), LOCATION_KEY: {"latitude": 50.80, "longitude": 4.35}}
    session.flush()
    good = ["4.375;50.750;20260706;3.0\n"]  # a good row first: nothing of it is kept either

    def count():
        return session.scalar(
            select(func.count())
            .select_from(WeatherObservation)
            .where(WeatherObservation.variable == ET0)
        )

    with pytest.raises(etp.EtpFileError, match=refusal):
        bad = rows if "grid cell" in refusal else good + rows
        etp.import_file(session, farm.id, _grid(tmp_path, bad), last_update=date.today())
    assert count() == 0

    ok = _grid(tmp_path, good)
    first = etp.import_file(session, farm.id, ok, last_update=date(2026, 7, 21))
    again = etp.import_file(session, farm.id, ok, last_update=date(2026, 7, 21))
    assert (first.receipt_id, first.days, count()) == (again.receipt_id, 1, 1)
    receipt = session.get(WeatherReceipt, first.receipt_id)
    assert (receipt.redistribution, receipt.request["tier"]) == (True, "national")
    assert VARIABLES[ET0].unit == "kg m-2"
