"""Coverage for validated observation/irrigation props and their invalid guard."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from farmcore.apps.operations import corrections, facts


def test_irrigation_amount_must_be_positive():
    with pytest.raises(ValidationError, match="must be positive"):
        facts.IrrigationFact(amount="0", unit="mm")


def test_props_that_are_not_a_fact_shape_read_as_absent():
    assert facts.irrigation(None) is None
    assert facts.observation(None) is None
    assert facts.observation({facts.OBSERVATION: {"text": 123}}) is None
    assert facts.irrigation({facts.IRRIGATION: "20 mm"}) is None


def test_dose_and_preview_validators_refuse_unsupported_inputs():
    huge = "1" * (corrections.NUMERIC_INTEGER_DIGITS + corrections.NUMERIC_FRACTION_DIGITS + 3)
    with pytest.raises(ValidationError, match="numeric storage"):
        corrections.Dose(product_id=1, value=huge, unit="kg/ha")
    with pytest.raises(ValidationError, match="must be rates"):
        corrections.Dose(product_id=1, measure="volume", value=1, unit="kg/ha")
    with pytest.raises(ValidationError, match="explicit spray rate"):
        corrections.Dose(product_id=1, value=1, unit="cups")
    with pytest.raises(ValidationError, match="timezone"):
        corrections.Changes(ts_start=datetime(2024, 4, 9, 6))
    with pytest.raises(ValidationError, match="Must not be blank"):
        corrections.Preview(
            request_key="  ", reason="because", changes=corrections.Changes(notes="x")
        )
