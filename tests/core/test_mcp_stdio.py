"""`farmcore mcp`: a local agent uses the farm's tools over MCP stdio with an agent token.

Failure model (one owning test each; the token itself is owned by tests/apps/share/test_tokens.py):
- the token leaks: on stderr, in an MCP error or a tool result (stdio journey, CLI refusal);
- the client's self-reported name becomes the audit actor (stdio journey: it claims "owner");
- a call is not audited (stdio journey; a failing call keeps its row);
- scope escalation: a tool outside the token's scopes is listed or runs (`offered`), or a tool
  reading beyond them answers instead of failing closed;
- a propose tool is offered to a token without `drafts:propose` (`offered`);
- a switched-off module's tools are listed or callable without a restart;
- a revoked token keeps working in a long-lived server (stdio journey);
- arguments outside the tool's schema reach the tool function.
Cross-farm calls have no input here: the farm is the token's, never an argument.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anyio
import pytest
from mcp import Client, StdioServerParameters, stdio_client
from mcp.types import Implementation
from sqlalchemy import select
from typer.testing import CliRunner

from farmcore.cli import cli
from farmcore.core import mcp as farm_mcp
from farmcore.core import registry
from farmcore.core.access import DRAFTS_PROPOSE, issue_token
from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import AccessLog, DataGrant, Farm, Parcel
from farmcore.core.models.identity import FarmMembership, LocalIdentity
from farmcore.core.models.kernel import AppModule
from farmcore.core.tools import ToolSpec, object_schema
from farmcore.settings import get_settings

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"
WAIT = 30.0  # seconds; bounds a hung subprocess, never waited out


def _token(session, scopes: list[str], *, farm_name: str = "MCP farm") -> tuple[Farm, int, str]:
    """A farm with parcel P1 "Bel-Air", its owner, and an agent token of `scopes`: (farm, token
    id, token). Flushed, not committed."""
    farm = Farm(name=farm_name)
    session.add(farm)
    session.flush()
    session.add(
        Parcel(farm_id=farm.id, name="Bel-Air", alias="P1", area_ha=Decimal("1.25"), geom=SQUARE)
    )
    owner = LocalIdentity(login=f"mcp-owner-{farm.id}", password_hash=hash_password("x" * 16))
    session.add(owner)
    session.flush()
    session.add(FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"))
    now = datetime.now(UTC)
    grant = DataGrant(
        farm_id=farm.id,
        grantee="Claude Desktop",
        scopes=scopes,
        purpose="my agent",
        valid_from=now,
        valid_to=now + timedelta(days=30),
    )
    session.add(grant)
    session.flush()
    token, raw = issue_token(session, grant, kind="personal", created_by=owner.id)
    return farm, token.id, raw


def _switch_off(session, name: str) -> None:
    session.merge(AppModule(name=name, state=registry.UNINSTALLED, version="0"))
    session.flush()


@pytest.fixture(scope="module")
def manifests():
    return registry.discover()


def _spec(name: str, scope: str, effect: str) -> ToolSpec:
    return ToolSpec(name, "d", object_schema(), "x:y", scope, effect=effect)


@pytest.mark.parametrize(
    ("tool", "scopes", "offered"),
    [
        (_spec("read_held", "fields:read", "read"), {"fields:read"}, True),
        (_spec("read_not_held", "logs:read", "read"), {"fields:read"}, False),
        (_spec("propose_held", "fields:read", "propose"), {"fields:read", DRAFTS_PROPOSE}, True),
        # Holding the tool's own scope is not enough to write a draft.
        (_spec("propose_no_drafts", "fields:read", "propose"), {"fields:read"}, False),
    ],
)
def test_a_token_is_offered_reads_in_its_scopes_and_proposals_only_with_drafts_propose(
    tool, scopes, offered
):
    assert (tool in farm_mcp.offered([tool], scopes)) is offered


def test_a_switched_off_module_loses_its_tools_without_a_restart(
    session, session_factory, settings, manifests
):
    _farm, _id, raw = _token(session, ["fields:read", "logs:read"])
    tools = farm_mcp.FarmTools(session_factory, settings, manifests, raw)
    assert "fertilization_season" in {t.name for t in tools.list()}

    _switch_off(session, "fertilization")

    assert "fertilization_season" not in {t.name for t in tools.list()}
    with pytest.raises(farm_mcp.Refused):
        tools.call("fertilization_season", {})


@pytest.mark.parametrize(
    "args",
    [{"parcel": "P1", "ctx": "x"}, {"parcel": 5}, {"parcel": "P" * 81}],
    ids=["unknown-argument", "wrong-type", "too-long"],
)
def test_arguments_outside_the_schema_are_refused_before_the_tool_runs(
    session, session_factory, settings, manifests, args
):
    _farm, token_id, raw = _token(session, ["fields:read", "logs:read"])
    tools = farm_mcp.FarmTools(session_factory, settings, manifests, raw)

    with pytest.raises(farm_mcp.Refused):
        tools.call("operations_last_spray", args)
    assert (
        session.scalars(select(AccessLog).where(AccessLog.actor == f"token:{token_id}")).all() == []
    )


def test_a_tool_reading_beyond_the_token_fails_closed_and_the_call_is_still_audited(
    session, session_factory, settings, manifests
):
    # The last-spray tool is a `logs:read` tool, but resolving the parcel reads `fields:read`.
    farm, token_id, raw = _token(session, ["logs:read"])
    tools = farm_mcp.FarmTools(session_factory, settings, manifests, raw)

    with pytest.raises(farm_mcp.Refused) as refused:
        tools.call("operations_last_spray", {"parcel": "P1"})

    assert "Bel-Air" not in str(refused.value)
    calls = session.scalars(
        select(AccessLog.resource).where(
            AccessLog.actor == f"token:{token_id}", AccessLog.farm_id == farm.id
        )
    ).all()
    assert calls == ["mcp:operations_last_spray"]


@pytest.mark.parametrize(
    ("tool", "args"), [("advisor_today", {}), ("advisor_parcel_status", {"parcel": "P1"})]
)
def test_advice_is_withheld_from_a_token_as_on_every_web_surface(
    session, session_factory, settings, manifests, tool, args
):
    # A token cannot read the advisor's inputs, so the advisor page shows it no decision.
    _farm, _id, raw = _token(session, ["advice:read", "fields:read"])
    tools = farm_mcp.FarmTools(session_factory, settings, manifests, raw)

    result = tools.call(tool, args)

    assert set(result.facts) == {"unavailable"}
    assert result.text.startswith("Assessment limited")


def test_the_cli_refuses_to_start_without_a_valid_token_and_never_echoes_it(
    settings, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        monkeypatch.delenv(farm_mcp.TOKEN_ENV, raising=False)
        missing = CliRunner().invoke(cli, ["mcp"])
        forged = "fc_pat_" + "A" * 43
        monkeypatch.setenv(farm_mcp.TOKEN_ENV, forged)
        rejected = CliRunner().invoke(cli, ["mcp"])
    finally:
        get_settings.cache_clear()

    assert missing.exit_code == 2
    assert rejected.exit_code == 1, rejected.output
    assert forged not in rejected.output


def test_the_official_client_lists_and_calls_tools_over_stdio_as_the_token(
    committed_session_factory, settings, tmp_path
):
    with committed_session_factory() as session:
        farm, token_id, raw = _token(session, ["fields:read", "logs:read"])
        other, _, _ = _token(session, ["fields:read"], farm_name="Another farm")
        session.add(
            Parcel(farm_id=other.id, name="Only B", alias="P9", area_ha=Decimal("1"), geom=SQUARE)
        )
        session.commit()
    errlog = (tmp_path / "mcp.err").open("w+")
    server = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from farmcore.cli import main; main()", "mcp"],
        env={**os.environ, "DATABASE_URL": settings.database_url, farm_mcp.TOKEN_ENV: raw},
    )

    async def journey():
        with anyio.fail_after(WAIT):
            # The client claims to be the farm's owner: it must be audited as the token anyway.
            async with Client(
                stdio_client(server, errlog=errlog),
                client_info=Implementation(name="owner", version="1"),
                cache=None,
            ) as client:
                listed = {t.name: t for t in (await client.list_tools()).tools}
                parcels = await client.call_tool("fields_list_parcels", {})
                with committed_session_factory() as session:
                    grant = session.scalar(select(DataGrant).where(DataGrant.farm_id == farm.id))
                    grant.revoked_at = datetime.now(UTC)
                    session.commit()
                revoked = await client.call_tool("fields_list_parcels", {})
                return listed, parcels, revoked

    listed, parcels, revoked = anyio.run(journey)

    assert {"fields_list_parcels", "operations_last_spray"} <= set(listed)
    assert not any(name.startswith("advisor_") for name in listed)  # advice:read not held
    assert listed["fields_list_parcels"].annotations.read_only_hint is True
    assert not parcels.is_error
    assert "P1 Bel-Air" in parcels.content[0].text
    listed_parcels = parcels.structured_content["facts"]["parcels"]
    assert [p["alias"] for p in listed_parcels] == ["P1"]  # not the other farm's P1 or P9
    assert revoked.is_error
    with committed_session_factory() as session:
        actors = set(
            session.scalars(select(AccessLog.actor).where(AccessLog.farm_id == farm.id)).all()
        )
        resources = session.scalars(
            select(AccessLog.resource).where(AccessLog.farm_id == farm.id)
        ).all()
    assert actors == {f"token:{token_id}"}
    assert resources.count("mcp:fields_list_parcels") == 1  # the revoked call ran nothing
    errlog.seek(0)
    shown = errlog.read() + str(parcels) + str(revoked)
    assert raw not in shown
