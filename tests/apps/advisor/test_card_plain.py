"""The plain-language advice card — lead, ✅/❌ 3-10 checklist, protection bar, Details, debug.

Scene B on the demo farm (open IGN parcels + the St-Émilion station CSV), stepped with the replay
clock. Expected numbers are the hero-scene literals: the 8 Apr 22:00 UTC infection event followed
10.3 mm of rain at a 15.4 °C mean (summed from the station CSV, as in test_card_event_rain.py);
the 3-10 thresholds are 10 mm, 10 °C and 48 h.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from farmcore.core import jobs
from farmcore.core.models.canonical import Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

TOKEN = "u2-admin-token"


class _Elements(HTMLParser):
    """Text (and attributes) of every element carrying an `id` or a `data-check` attribute."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[dict] = []
        self._open: list[tuple[str, dict | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        record = None
        if "id" in a or "data-check" in a:
            record = {"tag": tag, "attrs": a, "text": ""}
            self.found.append(record)
        if tag not in {"br", "img", "input", "meta", "link", "hr", "source", "wbr"}:
            self._open.append((tag, record))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                del self._open[i:]
                return

    def handle_data(self, data: str) -> None:
        for _, record in self._open:
            if record is not None:
                record["text"] += data


def elements(page: str) -> list[dict]:
    parser = _Elements()
    parser.feed(page)
    for r in parser.found:
        r["text"] = " ".join(r["text"].split())
    return parser.found


def by_id(page: str, element_id: str) -> dict:
    return next(e for e in elements(page) if e["attrs"].get("id") == element_id)


def checks(page: str) -> dict[str, dict]:
    return {e["attrs"]["data-check"]: e for e in elements(page) if "data-check" in e["attrs"]}


def parcel_id(session, alias: str) -> int:
    return session.scalar(select(Parcel.id).where(Parcel.alias == alias))


@pytest.mark.data
def test_spray_card_leads_with_product_and_window_end_and_a_met_checklist(
    client, session, seeded, step
) -> None:
    seeded("B")
    step("2024-04-10 06:00")

    page = client.get(f"/advisor/parcels/{parcel_id(session, 'P1')}").text

    lead = by_id(page, "lead")["text"]
    assert "copper" in lead
    # The window runs to the end of the loaded 72 h forecast: 13 Apr 06:00 local. That is the
    # forecast's end, not a spray deadline, so the lead says so.
    assert "13 Apr" in lead and "06:00" in lead
    assert "open to the forecast end, Sat 13 Apr 06:00" in lead
    assert "before Sat 13 Apr" not in lead
    rows = checks(page)
    assert list(rows) == ["shoots", "temperature", "rain"]
    for row in rows.values():
        assert row["attrs"]["data-met"] == "true"
        text = row["text"].lower()
        assert "✅" in text and "met" in text and "not met" not in text
    assert "15.4 °C" in rows["temperature"]["text"]
    assert "≥ 10 °C" in rows["temperature"]["text"]
    assert "10.3 mm" in rows["rain"]["text"]
    assert "≥ 10 mm" in rows["rain"]["text"] and "48 h" in rows["rain"]["text"]
    assert "assumed" in rows["shoots"]["text"]  # the season gate is a dated default
    # The rain row's value is the headline's mm (one source: the infection event).
    headline_mm = re.search(r"after (\d+\.\d) mm rain", by_id(page, "headline")["text"]).group(1)
    assert rows["rain"]["attrs"]["data-value"] == headline_mm


@pytest.mark.data
def test_protected_card_leads_with_until_and_the_bar_shows_mm_since_spray(
    client, session, settings, session_factory, manifests, seeded, step
) -> None:
    seeded("B")
    step("2024-04-10 06:00")
    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok

    page = client.get(f"/advisor/parcels/{parcel_id(session, 'P1')}").text

    assert by_id(page, "decision-badge")["attrs"]["data-decision"] == "protected"
    assert by_id(page, "lead")["text"].startswith("Protected until")
    bar = by_id(page, "protection-bar")
    # No rain fell between the 05:30 spray and 06:00; copper washes off after 20 mm.
    label = bar["text"]
    assert "0.0 mm" in label and "20 mm" in label
    assert "cover left" in label.lower()  # a word plus a number, not colour only
    assert bar["attrs"]["data-mm"] == "0.0"


@pytest.mark.data
def test_washed_off_card_says_how_much_rain_fell_since_the_spray(client, session, seeded, step):
    seeded("B")
    step("2024-05-15 06:00")  # Scene B: the 9 May copper is washed off (demo click 5)
    p1 = parcel_id(session, "P1")

    page = client.get(f"/advisor/parcels/{p1}").text
    mm = client.get(f"/advisor/parcels/{p1}.json").json()["mm_since_spray"]

    card = by_id(page, "protection")
    assert card["attrs"]["data-protection"] == "washed off"
    line = card["text"]
    assert f"{mm:.1f} mm" in line  # the number, not only "washed off"
    assert "9 May" in line and "20 mm" in line
    assert "washed off" in line.lower()


def test_expired_protection_line_keeps_the_mm_since_the_spray() -> None:
    from farmcore.apps.advisor.routes import _protection

    reason = {
        "rule": "protection_downy",
        "verdict": "expired",
        "inputs": {
            "spray_ts": "2024-04-10T04:00:00+00:00",
            "until": "2024-04-20T04:00:00+00:00",
            "mm_since_spray": 4.2,
            "product_types": ["contact"],
        },
        "threshold": {"washoff_copper_mm": 20},
    }

    line = _protection([reason], "Europe/Paris")["line"]

    assert "ended Sat 20 Apr" in line and "4.2 mm" in line and "10 Apr" in line


@pytest.mark.data
def test_no_action_card_shows_rain_not_met_over_the_last_48_h(
    client, session, seeded, step
) -> None:
    seeded("B")
    step("2024-04-08 06:00")  # before the 8 Apr 22:00 UTC rain event

    page = client.get(f"/advisor/parcels/{parcel_id(session, 'P1')}").text

    assert by_id(page, "decision-badge")["attrs"]["data-decision"] == "none"
    assert by_id(page, "lead")["text"] == "Nothing to do today"
    rows = checks(page)
    rain = rows["rain"]
    assert rain["attrs"]["data-met"] == "false"
    assert "❌" in rain["text"] and "not met" in rain["text"].lower()
    # No event yet: the current 48 h (6.7 mm in the St-Émilion CSV), not event values.
    assert "last 48 h" in rain["text"] and "6.7 mm" in rain["text"]
    assert all("infection event" not in r["text"] for r in rows.values())


def _locked_client(settings, session_factory) -> TestClient:
    from farmcore.main import create_app

    locked = settings.model_copy(update={"admin_token": SecretStr(TOKEN)})
    return TestClient(create_app(locked, session_factory=session_factory))


def _debug_bits(page: str, pid: int) -> dict[str, bool]:
    return {
        "rule code": "<code" in page and ">downy_3_10</code>" in page,
        "json link": f'href="/advisor/parcels/{pid}.json"' in page,
        "raw inputs": "gate_start_utc" in page and "t48_c" in page,
        # 10 Apr 06:00 is a replay: the flag and the spray-window note say so, in debug only.
        "replay flag": 'data-flag="replay_forecast"' in page,
        "replay note": "observed weather used as forecast" in page.lower(),
    }


@pytest.mark.data
def test_rule_codes_raw_inputs_and_json_link_render_only_in_admin_debug(
    session, settings, session_factory, seeded, step
) -> None:
    seeded("B")
    step("2024-04-10 06:00")
    pid = parcel_id(session, "P1")
    url = f"/advisor/parcels/{pid}"
    nothing = dict.fromkeys(
        ["rule code", "json link", "raw inputs", "replay flag", "replay note"], False
    )
    everything = dict.fromkeys(nothing, True)

    with _locked_client(settings, session_factory) as anon:
        assert _debug_bits(anon.get(url).text, pid) == nothing
        assert _debug_bits(anon.get(url, params={"debug": "1"}).text, pid) == nothing
        anon.cookies.set("farmcore_debug", "1")
        assert _debug_bits(anon.get(url).text, pid) == nothing

    with _locked_client(settings, session_factory) as admin:
        admin.cookies.set("admin_token", TOKEN)
        assert _debug_bits(admin.get(url).text, pid) == nothing
        assert _debug_bits(admin.get(url, params={"debug": "1"}).text, pid) == everything
        admin.cookies.set("farmcore_debug", "1")
        assert _debug_bits(admin.get(url).text, pid) == everything
        assert _debug_bits(admin.get(url, params={"debug": "0"}).text, pid) == nothing


@pytest.mark.data
def test_details_keep_every_rule_and_its_published_sources_without_team_notes(
    session, settings, session_factory, seeded, step
) -> None:
    seeded("B")
    step("2024-04-10 06:00")
    url = f"/advisor/parcels/{parcel_id(session, 'P1')}"

    with _locked_client(settings, session_factory) as anon:
        page = anon.get(url).text
        resp = anon.get(f"{url}.json")
    reasons = resp.json()["reasons"]

    why = page.split('<details id="why"', 1)[1].split("</details>", 1)[0]
    assert why.split(">", 1)[0].strip().find("open") == -1  # closed until the script opens it
    for reason in reasons:
        assert f'data-rule="{reason["rule"]}"' in why
    # Published citations stay, each once; web addresses are links.
    for citation in ("Baldacci 1947", "Gessi et al. 2022", "Arrêté du 4 mai 2017"):
        assert citation in why
    assert why.count("Thomas, Gubler et al. 1994") == 1
    assert why.count("3-10 rule (Baldacci 1947)") == 1
    assert 'href="https://ge.ch/document/2937/annexe/22"' in why
    assert ">assumed</span>" in why
    assert "awaiting agronomist" in why
    # Internal document notes never reach a farmer, nor raw config keys.
    for internal in (
        "(P3)",
        "review-2026-10-16",
        "passes-analysis-2026-10-16",
        "research/agronomy.md",
        "decision R13",
        "log.md",
        "gt_delta_fail_hot",
        "downy_rain_min_mm",
    ):
        assert internal not in page, internal
    # The stored text is still one tap away for admins, in debug mode.
    with _locked_client(settings, session_factory) as admin:
        admin.cookies.set("admin_token", TOKEN)
        debug_page = admin.get(url, params={"debug": "1"}).text

    for reason in reasons:
        assert reason["source"] in html.unescape(debug_page)


@pytest.mark.data
def test_index_shows_one_chip_per_parcel_and_one_evaluated_line(client, seeded, step) -> None:
    seeded("B")
    step("2024-04-10 06:00")

    page = client.get("/advisor/").text

    # One chip per parcel in the by-decision list; the expert table repeats them.
    grouped = page.split('id="by-decision"', 1)[1].split("</section>", 1)[0]
    assert grouped.count("data-decision=") == 10
    assert page.lower().count("evaluated") == 1
    for n in range(1, 11):
        assert f">P{n}<" in page


@pytest.mark.data
def test_sulphur_pass_lead_gives_the_headline_reason_not_an_old_wash_off(
    client, session, seeded, step
) -> None:
    seeded("B")
    step("2024-06-10 06:00")  # Scene B: a sulphur-only pass, an old downy wash-off still flagged

    page = client.get(f"/advisor/parcels/{parcel_id(session, 'P1')}").text

    headline = by_id(page, "headline")["text"]
    lead = by_id(page, "lead")["text"]
    assert "powdery cover ended" in headline
    assert "sulphur" in lead.lower()
    assert "powdery mildew cover has ended" in lead
    assert "washed off" not in lead


def _texts(source: str) -> list[str]:
    from farmcore.apps.advisor.routes import plain_sources

    return ["".join(text for text, _ in entry) for entry in plain_sources(source)]


def test_plain_sources_keep_a_qualifier_that_followed_a_dropped_team_ref() -> None:
    gt = "UC IPM ipm.ucanr.edu/x/; Thomas, Gubler et al. 1994; research/agronomy.md §2e"
    one = _texts(f"{gt} (70 °F)")
    assert one == ["UC IPM ipm.ucanr.edu/x/", "Thomas, Gubler et al. 1994 (70 °F)"]
    # A reason citing both band edges lists the citation once, with both qualifiers.
    both = _texts(f"{gt} (70 °F); {gt} (85 °F); {gt}: ≥ 6 continuous hours in the band")
    assert both == [
        "UC IPM ipm.ucanr.edu/x/",
        "Thomas, Gubler et al. 1994 (70 °F; 85 °F)",
        "≥ 6 continuous hours in the band",
    ]


def _wait_without_window(horizon_h: int) -> dict:
    window = {"rule": "spray_window", "threshold": {"window_horizon_h": horizon_h}}
    return {
        "id": 1,
        "decision": "wait",
        "evaluated_at": "2024-04-10T06:00:00+02:00",
        "next_window": None,
        "reasons": [window],
        "products": [],
        "flags": [],
    }


@pytest.mark.parametrize(("horizon_h", "span"), [(72, "3 days"), (48, "2 days"), (36, "36 h")])
def test_no_window_text_follows_the_configured_horizon(horizon_h: int, span: str) -> None:
    from farmcore.apps.advisor.routes import _lead, _next_window

    advice = _wait_without_window(horizon_h)

    assert _lead(advice, "Europe/Paris") == f"Wait. No spray window in the next {span}"
    assert _next_window(advice, "Europe/Paris") == f"No spray window in the next {span}"


def _incubation(symptoms_at: str) -> str | None:
    from farmcore.apps.advisor.routes import _incubation as incubation

    reason = {
        "rule": "downy_incubation",
        "inputs": {"pct": 100.0, "event": "2024-05-20T22:00:00+00:00", "symptoms_at": symptoms_at},
        "threshold": {"incub_alert_pct": 50, "incub_symptoms_pct": 100},
    }
    return incubation([reason], "2024-06-10T04:00:00+00:00", "Europe/Paris")


def test_incubation_says_since_for_symptoms_already_due_and_due_for_later_ones() -> None:
    assert _incubation("2024-05-31T10:00:00+00:00").endswith("Symptoms expected since 31 May")
    assert _incubation("2024-06-12T10:00:00+00:00").endswith("Symptoms due 12 Jun")


@pytest.mark.data
def test_a_replay_window_is_disclosed_to_farmers_in_plain_words(
    session, settings, session_factory, seeded, step
) -> None:
    # The replay "forecast" is observed weather; farmers see that in plain words,
    # without the flag code (debug only).
    seeded("B")
    step("2024-04-10 06:00")
    pid = parcel_id(session, "P1")
    with _locked_client(settings, session_factory) as anon:
        page = anon.get(f"/advisor/parcels/{pid}").text
    [note] = re.findall(r'<p id="replay-note"[^>]*>(.*?)</p>', page, re.S)
    assert "Replay: the observed weather stands in for the forecast" in note
    assert "replay_forecast" not in page
