"""FarmCore's unit registry: typed quantities with `Decimal` arithmetic only.

An explicit `pint` registry (no default definitions file) holds the units FarmCore accepts, with
`non_int_type=Decimal`, so a stock or dose never passes through a float. A unit is exactly one
registry name or alias, or a name per name (`kg/ha`), with no spaces; the database's frozen
`unit_definition` table (core_0006) accepts exactly the same strings with the same factors.
`measure(value, unit)` keeps the value and unit exactly as entered and adds the value in the
measure's standard unit, as a plain decimal (never exponent form).
`convert` changes units only with a known factor: a density between mass and volume, an area
between a rate and a total. `component` needs the composition fraction. A missing factor raises
`MissingFactor`; a dimension that no factor can bridge raises `IncompatibleUnits`. Nothing is
converted silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

import pint

DEFINITIONS = (
    "kilogram = [mass] = kg",
    "gram = kilogram / 1000 = g",
    "milligram = gram / 1000 = mg",
    "tonne = 1000 * kilogram = t",
    "litre = [volume] = l",
    "millilitre = litre / 1000 = ml",
    "hectolitre = 100 * litre = hl",
    "cubic_metre = 1000 * litre = m3",
    "hectare = [area] = ha",
    "are = hectare / 100",
    "square_metre = hectare / 10000 = m2",
    "item = [count] = count",
)
# Spellings people enter for a registry name. Explicit only: case carries meaning (`mL`/`ml` is
# fine, `t`/`T` is not), so there is no blanket case-folding.
ALIASES: dict[str, str] = {
    "L": "l",
    "mL": "ml",
    "hL": "hl",
    "lt": "l",
    "Ha": "ha",
    "KG": "kg",
    "Kg": "kg",
}
# measure -> standard unit (farmOS-shaped measures; rates are per hectare).
STANDARD_UNITS: dict[str, str] = {
    "mass": "kg",
    "volume": "l",
    "area": "ha",
    "count": "count",
    "mass_rate": "kg/ha",
    "volume_rate": "l/ha",
    "count_rate": "count/ha",
}

registry = pint.UnitRegistry(None, non_int_type=Decimal)
for _definition in DEFINITIONS:
    registry.define(_definition)
_MEASURES = {registry.Unit(unit).dimensionality: name for name, unit in STANDARD_UNITS.items()}


class UnitError(ValueError):
    """A value or unit FarmCore cannot type."""


class UnknownUnit(UnitError):
    """The unit is not in FarmCore's registry or names no FarmCore measure."""


class IncompatibleUnits(UnitError):
    """No factor can bridge the two dimensions."""


class MissingFactor(UnitError):
    """The conversion needs a density, area or composition that was not given."""


@dataclass(frozen=True)
class Measured:
    """A typed quantity: the entered value and unit, plus the value in the standard unit."""

    measure: str
    value: Decimal
    unit: str
    entered_value: Decimal
    entered_unit: str


def decimal_value(value: object) -> Decimal:
    """`value` when it is a finite Decimal, else `UnitError`."""
    if not isinstance(value, Decimal):
        raise UnitError(f"quantities take Decimal values, not {type(value).__name__}")
    if not value.is_finite():
        raise UnitError("a quantity value must be finite")
    return value


# pint's own expression parser is not used: it accepts scaling factors and powers, and crashes
# on malformed input.
_NAME = r"[A-Za-z][A-Za-z0-9_]*"
_UNIT = re.compile(f"({_NAME})(?:/({_NAME}))?")
NAMES = frozenset(
    name
    for definition in DEFINITIONS
    for name in (part.strip() for part in definition.split("="))
    if re.fullmatch(_NAME, name)
) | frozenset(ALIASES)


def _units(unit: str) -> pint.Unit:
    match = _UNIT.fullmatch(unit) if isinstance(unit, str) else None
    names = [name for name in match.groups() if name] if match else []
    if not names or any(name not in NAMES for name in names):
        raise UnknownUnit(f"unknown unit {unit!r}")
    result = registry.Unit(ALIASES.get(names[0], names[0]))
    if len(names) == 2:
        result = result / registry.Unit(ALIASES.get(names[1], names[1]))
    return result


def plain(value: Decimal) -> Decimal:
    """`value` without exponent form, so `str()` in a CSV or JSON reads `10000`, not `1E+4`."""
    return Decimal(format(value, "f"))


def _quantity(value: Decimal, unit: str) -> pint.Quantity:
    return registry.Quantity(decimal_value(value), _units(unit))


def _measured(quantity: pint.Quantity, entered_value: Decimal, entered_unit: str) -> Measured:
    name = _MEASURES.get(quantity.dimensionality)
    if name is None:
        raise UnknownUnit(f"{entered_unit!r} is not a FarmCore measure")
    standard = STANDARD_UNITS[name]
    value = plain(quantity.to(standard).magnitude)
    return Measured(name, value, standard, entered_value, entered_unit)


def measure(value: Decimal, unit: str) -> Measured:
    """Type an entered `value unit`. Raises `UnitError` for floats, non-finite values and units
    outside the registry."""
    return _measured(_quantity(value, unit), value, unit)


def convert(
    quantity: Measured,
    unit: str,
    *,
    density_kg_per_l: Decimal | None = None,
    area_ha: Decimal | None = None,
) -> Decimal:
    """`quantity` in `unit`. Mass and volume convert only with a density, a rate and a total
    only with an area; a factor that is not needed is ignored."""
    source = registry.Quantity(quantity.value, quantity.unit)
    target = _quantity(Decimal(1), unit)
    given = {
        "area": None if area_ha is None else registry.Quantity(decimal_value(area_ha), "ha"),
        "density": (
            None
            if density_kg_per_l is None
            else registry.Quantity(decimal_value(density_kg_per_l), "kg/l")
        ),
    }
    # A missing factor is a unit placeholder, so its dimension still matches and is reported.
    area = given["area"] if given["area"] is not None else registry.Quantity(Decimal(1), "ha")
    density = (
        given["density"] if given["density"] is not None else registry.Quantity(Decimal(1), "kg/l")
    )
    routes = (
        ((), source),
        (("area",), source * area),
        (("area",), source / area),
        (("density",), source * density),
        (("density",), source / density),
        (("area", "density"), source * area * density),
        (("area", "density"), source * area / density),
        (("area", "density"), source / area * density),
        (("area", "density"), source / area / density),
    )
    for needs, candidate in routes:
        if candidate.dimensionality != target.dimensionality:
            continue
        missing = [name for name in needs if given[name] is None]
        if missing:
            raise MissingFactor(f"converting to {unit!r} needs {' and '.join(missing)}")
        return candidate.to(target.units).magnitude
    raise IncompatibleUnits(f"{quantity.unit!r} cannot become {unit!r}")


def component(quantity: Measured, fraction: Decimal | None) -> Measured:
    """The amount of one component (e.g. copper in a product) from its composition fraction
    (0..1). Without the composition the amount is unknown: `MissingFactor`. The entered value
    and unit stay the product's: nobody entered the component amount."""
    if fraction is None:
        raise MissingFactor("the component amount needs the product composition")
    share = decimal_value(fraction)
    if not Decimal(0) <= share <= Decimal(1):
        raise UnitError("a composition fraction lies between 0 and 1")
    return Measured(
        quantity.measure,
        plain(quantity.value * share),
        quantity.unit,
        quantity.entered_value,
        quantity.entered_unit,
    )
