"""Seeded public-HTTP navigation properties; synthetic fixtures, no new dependency.

Seeds are fixed and each assertion reports its seed/case for reproduction.
"""

import random
import re
import string
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from sqlalchemy import select

from farmcore.core.access import FARM_COOKIE
from farmcore.core.models.canonical import DataGrant, Event, Farm, Log, Parcel
from farmcore.core.models.kernel import AppModule, ClockState
from tests.core.test_navigation_context import farms as farms
from tests.core.test_navigation_context import links

SEEDS = (20261017, 180104, 714029)
BUSINESS_MODELS = (Farm, Parcel, DataGrant, Log, Event, AppModule, ClockState)


def _snapshot(session):
    # AccessLog is intentionally excluded: normal authorized reads are audited.
    return {m.__tablename__: session.execute(select(m.__table__)).all() for m in BUSINESS_MODELS}


def _back(markup):
    matches = re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', markup, re.S)
    return [url for url, body in matches if re.search(r"Back to |All advice", body)]


def _assert_rejected(client, session, first, mine, origins, seed):
    # Warm the permitted clock initialization before taking the immutable-state baseline.
    client.get(f"/advisor/parcels/{mine.id}", params={"farm": first.id})
    snapshot, cookies = _snapshot(session), dict(client.cookies)
    for case, origin in enumerate(origins):
        page = client.get(
            f"/advisor/parcels/{mine.id}",
            params={"farm": first.id, "return_to": origin},
        )
        marker = f"seed={seed}, case={case}, origin={origin[:180]!r}"
        assert page.status_code == 200, marker
        assert "Back to " not in page.text, marker
        assert f"/advisor/?farm={first.id}" in links(page.text), marker
        # An offered local action is a failure even if this test never executes that action.
        assert len(_back(page.text)) == 1, marker
        assert dict(client.cookies) == cookies, marker
    assert _snapshot(session) == snapshot, f"business state changed for seed={seed}"


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_valid_read_origins_preserve_scope_and_semantic_state(client, farms, seed):
    first, second, mine, _ = farms
    client.cookies.set(FARM_COOKIE, str(second.id))
    rng = random.Random(seed)
    for case in range(24):
        kind = rng.choice(("map", "model", "activity"))
        pairs = [("farm", str(first.id))]
        fragment = ""
        if kind == "map":
            path = "/fields/"
            pairs += [("parcel", str(mine.id))]
        elif kind == "model":
            path = "/advisor/"
            pairs += [("model", rng.choice(("all", "downy", "powdery")))]
        else:
            path = "/activity/"
            start = date(2024, 1, 1) + timedelta(days=rng.randrange(60))
            end = start + timedelta(days=rng.randrange(30))
            pairs += [
                ("parcel_id", str(mine.id)),
                ("type", rng.choice(("all", "spray", "advice", "sharing", "demo"))),
                ("from", start.isoformat()),
                ("to", end.isoformat()),
            ]
            if rng.choice((True, False)):
                pairs += [("before", "2024-04-01T06:00:00+00:00"), ("before_id", str(case + 1))]
        if rng.choice((True, False)):
            fragment = "#parcel-" + str(mine.id)
        rng.shuffle(pairs)
        origin = path + "?" + urlencode(pairs) + fragment
        page = client.get(
            f"/advisor/parcels/{mine.id}", params={"farm": first.id, "return_to": origin}
        )
        marker = f"seed={seed}, case={case}, origin={origin!r}"
        assert page.status_code == 200 and "Back to " in page.text, marker
        expected = dict((key, [value]) for key, value in pairs)
        candidates = [urlsplit(u) for u in links(page.text) if urlsplit(u).path == path]
        assert any(
            parse_qs(u.query) == expected and u.fragment == fragment.removeprefix("#")
            for u in candidates
        ), marker
        for url in _back(page.text):
            # html entities are decoded by links(); inspect those actual URLs below instead.
            assert not urlsplit(url).scheme and not urlsplit(url).netloc, marker
        assert client.cookies[FARM_COOKIE] == str(second.id), marker


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_foreign_duplicate_and_privilege_contexts_fall_back_without_effects(
    client, farms, session, seed
):
    first, second, mine, other = farms
    rng = random.Random(seed)
    origins = []
    for _ in range(12):
        local_pairs = [("farm", str(first.id)), ("parcel", str(mine.id))]
        duplicate = rng.choice(local_pairs)
        duplicated = [*local_pairs, duplicate]
        rng.shuffle(duplicated)
        origins += [
            "/fields/?" + urlencode(duplicated),
            "/fields/?" + urlencode({"farm": second.id, "parcel": mine.id}),
            "/fields/?" + urlencode({"farm": first.id, "parcel": other.id}),
            "/activity/?" + urlencode({"farm": first.id, "parcel_id": other.id}),
        ]
        key = rng.choice(("return_to", "token", "debug", "admin_token", "grant"))
        value = "".join(rng.choices(string.ascii_lowercase, k=rng.randrange(1, 20)))
        origins.append("/fields/?" + urlencode([*local_pairs, (key, value)]))
    _assert_rejected(client, session, first, mine, origins, seed)


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_action_authority_and_encoding_origins_are_not_offered(
    client, farms, session, seed
):
    first, _, mine, _ = farms
    rng = random.Random(seed)
    origins = []
    actions = (
        ("/prefs/expert", "on", "1"),
        ("/farms/select", "farm", str(first.id)),
        ("/clock/step", "hours", "1"),
        ("/forecast/busy", "busy", "1"),
        ("/activity/columns", "show", "source"),
        ("/demo/apply", "preset", "reset"),
        ("/notifications/read-all", "farm", str(first.id)),
    )
    for case in range(36):
        path, key, value = rng.choice(actions)
        origins.append(path + "?" + urlencode([(key, value), ("farm", str(first.id))]))
        host = "".join(rng.choices(string.ascii_lowercase, k=12)) + ".invalid"
        origins.append(rng.choice(("https://", "//")) + host + "/fields/")
        # Grammar-derived malformed/ambiguous encodings, not merely random unrelated text.
        escape = rng.choice(("%00", "%0a", "%09", "%5c", "%252f", "%GG", "%C3%28"))
        origins.append("/fields/?parcel=" + str(mine.id) + "&farm=" + str(first.id) + escape)
        origins.append("/fields/" + escape + str(case))
    _assert_rejected(client, session, first, mine, origins, seed)


@pytest.mark.parametrize("seed", SEEDS)
def test_generated_oversized_otherwise_parseable_origins_fall_back(client, farms, session, seed):
    first, _, mine, _ = farms
    rng = random.Random(seed)
    # Existing Pydantic datetime parser accepts long leading-zero epoch strings.
    # This isolates the URL budget from unrelated path/anchor/type validators.
    origins = ["/activity/?before=" + "0" * rng.randrange(2049, 2600) for _ in range(10)]
    _assert_rejected(client, session, first, mine, origins, seed)
