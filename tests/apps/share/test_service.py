"""Share service: presets, create / revoke a grant, status, the grant-bound repository, masking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.share import config, service
from farmcore.core import access
from farmcore.core.models.canonical import DataGrant, Event


def _future(days: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def test_every_preset_holds_known_read_scopes_only() -> None:
    assert next(iter(config.PRESETS)) == "adviser_logs"
    assert config.PRESETS["adviser_logs"][1] == ("logs:read", "register:read")
    for _label, scopes in config.PRESETS.values():
        assert scopes and set(scopes) <= access.SCOPES
        assert not {"logs:write", "audit:read", access.ALL_SCOPES} & set(scopes)
        # Each preset scope covers at least one resource the core contract lists.
        assert all(any(s in allowed for allowed in access.RESOURCE_SCOPES.values()) for s in scopes)


def test_create_grant_writes_the_preset_scopes_on_the_repo_farm_and_one_event(
    session, owner_repo, demo
) -> None:
    until = _future()
    before = datetime.now(UTC)

    grant = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=until,
    )

    assert grant.id is not None
    assert grant.farm_id == demo.farm.id
    assert grant.grantee == "Co-op adviser"
    assert grant.scopes == ["logs:read", "register:read"]
    assert grant.purpose == "spray advice"
    assert grant.valid_to == until
    assert before - timedelta(seconds=5) <= grant.valid_from <= datetime.now(UTC)
    events = session.scalars(select(Event).where(Event.kind == "share.granted")).all()
    assert len(events) == 1
    ev = events[0]
    assert (ev.res_model, ev.res_id, ev.actor, ev.source_app, ev.farm_id) == (
        "data_grant",
        grant.id,
        "owner",
        "share",
        demo.farm.id,
    )
    assert "Co-op adviser" in ev.body and "spray logs" in ev.body


@pytest.mark.parametrize(
    ("grantee", "preset", "days", "words"),
    [
        ("", "adviser_logs", 30, "name"),
        ("   ", "adviser_logs", 30, "name"),
        ("x" * (config.GRANTEE_MAX_LEN + 1), "adviser_logs", 30, "characters"),
        ("Owner", "adviser_logs", 30, "owner"),
        ("Co-op adviser", "everything", 30, "preset"),
        ("Co-op adviser", "adviser_logs", -1, "future"),
    ],
)
def test_create_grant_rejects_invalid_input_and_writes_nothing(
    session, owner_repo, grantee, preset, days, words
) -> None:
    count = session.scalar(select(DataGrant.id).order_by(DataGrant.id.desc()).limit(1))

    with pytest.raises(ValueError, match=words):
        service.create_grant(
            owner_repo, grantee=grantee, preset=preset, purpose="p", valid_to=_future(days)
        )

    assert session.scalar(select(DataGrant.id).order_by(DataGrant.id.desc()).limit(1)) == count
    assert session.scalars(select(Event).where(Event.kind == "share.granted")).all() == []
