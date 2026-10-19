"""Retained raw correction evidence needs its own explicit read scope."""

import pytest

from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import DataGrant


@pytest.mark.parametrize("scope", ["logs:read", "register:read", "audit:read", "stations:read"])
def test_ordinary_grant_scope_cannot_authorize_raw_correction_receipts(scope):
    # Public authorization policy before any query; no correction table/migration required.
    repo = ScopedRepository(
        None,
        1,
        DataGrant(farm_id=1, scopes=[scope], purpose="test", grantee="recipient"),
        "recipient",
        "test",
    )
    with pytest.raises(ScopeDenied, match="does not cover"):
        repo.check(scope, "operation_correction")
