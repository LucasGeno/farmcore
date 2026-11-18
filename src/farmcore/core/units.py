"""FarmCore's unit registry: typed quantities with `Decimal` arithmetic only.

An explicit `pint` registry (no default definitions file) holds the units FarmCore accepts, with
`non_int_type=Decimal`, so a stock or dose never passes through a float. `measure(value, unit)`
keeps the value and unit exactly as entered and adds the value in the measure's standard unit.
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
    "litre = [volume] = l = L",
    "millilitre = litre / 1000 = ml = mL",
    "hectolitre = 100 * litre = hl = hL",
    "cubic_metre = 1000 * litre = m3",
    "hectare = [area] = ha",
    "are = hectare / 100",
    "square_metre = hectare / 10000 = m2",
    "item = [count] = count",
)
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


def _decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise UnitError(f"quantities take Decimal values, not {type(value).__name__}")
    if not value.is_finite():
        raise UnitError("a quantity value must be finite")
    return value


# Units are a registry name, or a name per name ("kg/ha"). pint's own expression parser is not
# used: it accepts scaling factors and powers, and crashes on malformed input.
_UNIT = re.compile(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:/\s*([A-Za-z][A-Za-z0-9_]*)\s*)?")
_NAMES = frozenset(
    name
    for definition in DEFINITIONS
    for name in (part.strip() for part in definition.split("="))
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)
)


def _units(unit: str) -> pint.Unit:
    match = _UNIT.fullmatch(unit) if isinstance(unit, str) else None
    names = [name for name in match.groups() if name] if match else []
    if not names or any(name not in _NAMES for name in names):
        raise UnknownUnit(f"unknown unit {unit!r}")
    result = registry.Unit(names[0])
    return result / registry.Unit(names[1]) if len(names) == 2 else result


def _quantity(value: Decimal, unit: str) -> pint.Quantity:
    return registry.Quantity(_decimal(value), _units(unit))


def _measured(quantity: pint.Quantity, entered_value: Decimal, entered_unit: str) -> Measured:
    name = _MEASURES.get(quantity.dimensionality)
    if name is None:
        raise UnknownUnit(f"{entered_unit!r} is not a FarmCore measure")
    standard = STANDARD_UNITS[name]
    return Measured(name, quantity.to(standard).magnitude, standard, entered_value, entered_unit)


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
        "area": None if area_ha is None else registry.Quantity(_decimal(area_ha), "ha"),
        "density": (
            None
            if density_kg_per_l is None
            else registry.Quantity(_decimal(density_kg_per_l), "kg/l")
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
    (0..1). Without the composition the amount is unknown: `MissingFactor`."""
    if fraction is None:
        raise MissingFactor("the component amount needs the product composition")
    share = _decimal(fraction)
    if not Decimal(0) <= share <= Decimal(1):
        raise UnitError("a composition fraction lies between 0 and 1")
    return Measured(
        quantity.measure,
        quantity.value * share,
        quantity.unit,
        quantity.entered_value * share,
        quantity.entered_unit,
    )
