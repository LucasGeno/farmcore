"""Private parameter/correction histories never inherit ordinary read permissions."""

import pytest

from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import DataGrant


@pytest.mark.parametrize(
    "scope",
    ["fields:read", "advice:read", "logs:read", "register:read", "audit:read", "stations:read"],
)
@pytest.mark.parametrize(
    "resource",
    ["operation_correction", "parameter_version", "parameter_review", "parameter_activation"],
)
def test_ordinary_grant_scope_cannot_authorize_raw_correction_receipts(scope, resource):
    # Public authorization policy before any query; no correction table/migration required.
    repo = ScopedRepository(
        None,
        1,
        DataGrant(farm_id=1, scopes=[scope], purpose="test", grantee="recipient"),
        "recipient",
        "test",
    )
    with pytest.raises(ScopeDenied, match="does not cover"):
        repo.check(scope, resource)
