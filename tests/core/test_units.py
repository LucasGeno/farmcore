"""FarmCore units: typed quantities never pass through a float and never convert silently.

Failure modes owned here:
- A float enters the quantity or stock path, or the entered value/unit is not kept exactly.
- An incompatible dimension, or a conversion missing its density, area or composition, is
  silently converted instead of refused.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from farmcore.core import units

# Entered unit -> (measure, standard unit, factor into it). Factors from SI prefixes.
UNITS = {
    "kg/ha": ("mass_rate", "kg/ha", Decimal(1)),
    "g/ha": ("mass_rate", "kg/ha", Decimal("0.001")),
    "L/ha": ("volume_rate", "l/ha", Decimal(1)),
    "ml/ha": ("volume_rate", "l/ha", Decimal("0.001")),
    "t": ("mass", "kg", Decimal(1000)),
    "hl": ("volume", "l", Decimal(100)),
    "m2": ("area", "ha", Decimal("0.0001")),
    "count": ("count", "count", Decimal(1)),
}
AMOUNTS = st.decimals(min_value=0, max_value=10**9, places=4, allow_nan=False)


@given(value=AMOUNTS, unit=st.sampled_from(sorted(UNITS)))
def test_quantities_stay_decimal_and_keep_what_was_entered(value, unit):
    typed = units.measure(value, unit)

    measure, standard, factor = UNITS[unit]
    assert (typed.measure, typed.unit) == (measure, standard)
    assert (typed.entered_value, typed.entered_unit) == (value, unit)
    assert type(typed.value) is Decimal and typed.value == value * factor
    back = units.convert(typed, unit)
    assert type(back) is Decimal and back == value


DOSE = units.measure(Decimal("800"), "g/ha")


@pytest.mark.parametrize(
    ("refused", "factored", "expected"),
    [
        # 800 g/ha over 1.25 ha is 1 kg; at 1.6 kg/l it is 0.5 l/ha; 20 % copper is 0.16 kg/ha.
        (
            lambda: units.convert(DOSE, "kg"),
            lambda: units.convert(DOSE, "kg", area_ha=Decimal("1.25")),
            Decimal(1),
        ),
        (
            lambda: units.convert(DOSE, "l/ha"),
            lambda: units.convert(DOSE, "l/ha", density_kg_per_l=Decimal("1.6")),
            Decimal("0.5"),
        ),
        (
            lambda: units.component(DOSE, None),
            lambda: units.component(DOSE, Decimal("0.2")).value,
            Decimal("0.16"),
        ),
        (lambda: units.convert(DOSE, "count/ha"), None, units.IncompatibleUnits),
        (lambda: units.measure(1.5, "kg"), None, units.UnitError),  # a float
        (lambda: units.measure(Decimal("NaN"), "kg"), None, units.UnitError),
        (lambda: units.measure(Decimal(1), "lb"), None, units.UnknownUnit),
        (lambda: units.measure(Decimal(1), "kg*l"), None, units.UnknownUnit),
        # Malformed units are refused, never a crash (a legacy write mirrors any unit string).
        (lambda: units.measure(Decimal(1), "kg/(ha"), None, units.UnknownUnit),
        (lambda: units.measure(Decimal(1), "10 kg"), None, units.UnknownUnit),
    ],
)
def test_conversions_need_their_factor_and_never_guess(refused, factored, expected):
    refusal = units.MissingFactor if factored else expected
    with pytest.raises(refusal):
        refused()
    if factored:
        assert factored() == expected
