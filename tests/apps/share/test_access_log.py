"""The access log on `/share/`: newest first, capped, this farm only, masked actors, R9 wording."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.share import config, service
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import AccessLog

RAW_CHAT = "123456788777"  # synthetic Telegram chat id


@pytest.mark.parametrize(
    ("actor", "shown"),
    [
        (f"chat:{RAW_CHAT}", "chat …8777"),
        ("chat:123", "chat …"),
        (f"tg:{RAW_CHAT}:42", "chat …8777"),
        ("tg:12:42", "chat …"),
        ("webhook 98765432 retry", "webhook …5432 retry"),
        ("owner", "owner"),
        ("job:advisor.evaluate", "job:advisor.evaluate"),
        ("cli:seed", "cli:seed"),
        ("seed", "seed"),
        ("import:isoxml", "import:isoxml"),
        ("grant:12", "grant:12"),
        ("grant:1234567", "grant:1234567"),
    ],
)
def test_mask_actor(actor: str, shown: str) -> None:
    assert service.mask_actor(actor) == shown


def _log(session, farm_id: int, grant_id: int | None, actor: str, **kw) -> AccessLog:
    row = AccessLog(
        actor=actor,
        farm_id=farm_id,
        grant_id=grant_id,
        resource=kw.get("resource", "log"),
        scope=kw.get("scope", "logs:read"),
        purpose=kw.get("purpose", "web"),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def adviser(session, owner_repo):
    g = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    return g


def test_access_rows_newest_first_capped_and_this_farm_only(
    session, owner_repo, demo, other_farm, adviser
) -> None:
    owner = ensure_owner_grant(session, demo.farm)
    foreign = _log(session, other_farm.id, None, "owner", resource="foreign-resource")
    for i in range(config.ACCESS_LOG_LIMIT + 5):
        _log(session, demo.farm.id, owner.id, "owner", resource=f"r{i}")
    newest = _log(
        session,
        demo.farm.id,
        adviser.id,
        f"grant:{adviser.id}",
        scope="register:read",
        purpose="spray advice",
    )

    rows = service.access_rows(owner_repo)

    assert len(rows) == config.ACCESS_LOG_LIMIT
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)
    assert foreign.id not in ids
    # The read that built these rows is itself logged (audit:read), after `newest`.
    assert rows[0]["scope"] == "audit:read"
    by_id = {r["id"]: r for r in rows}
    assert by_id[newest.id]["grant"] == f"#{adviser.id} Co-op adviser"
    assert by_id[newest.id]["actor"] == f"grant:{adviser.id}"
    assert by_id[ids[-1]]["grant"] == "owner"


def test_grantees_only_keeps_third_party_rows(session, owner_repo, demo, adviser) -> None:
    _log(session, demo.farm.id, adviser.id, f"grant:{adviser.id}", scope="register:read")

    rows = service.access_rows(owner_repo, grantees_only=True)

    assert rows and {r["grant"] for r in rows} == {f"#{adviser.id} Co-op adviser"}


def test_page_masks_chat_ids_and_toggles_third_parties(
    admin, anonymous, session, demo, adviser
) -> None:
    owner = ensure_owner_grant(session, demo.farm)
    _log(session, demo.farm.id, owner.id, f"chat:{RAW_CHAT}", purpose="chat")
    _log(
        session,
        demo.farm.id,
        adviser.id,
        f"grant:{adviser.id}",
        scope="register:read",
        purpose="spray advice",
        resource="quantity",
    )
    session.commit()

    for c in (admin, anonymous):
        page = c.get("/share/").text
        assert "chat …8777" in page
        assert RAW_CHAT not in page
        assert "tok-share" not in page
    only = anonymous.get("/share/", params={"who": "grantees"}).text
    log_html = only.split("Access log", 1)[1]
    assert "chat …8777" not in log_html
    assert f"grant:{adviser.id}" in log_html and "spray advice" in log_html
    assert 'href="/share/?who=grantees"' in only or "who=grantees" in only


def test_pages_use_data_act_framing_and_never_intermediary_words(
    anonymous, session, demo, adviser
) -> None:
    for path in ("/share/", f"/share/as/{adviser.id}/"):
        text = anonymous.get(path).text
        assert "Data Act" in text, path
        assert not re.search(r"intermediation|intermediary|\bDGA\b", text, re.IGNORECASE), path
