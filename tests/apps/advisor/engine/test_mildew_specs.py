"""The mildew rules' parameters live in their ModelSpec modules; the config reads them there."""

from datetime import date

import pytest

from farmcore.apps.advisor import specs
from farmcore.apps.advisor.engine import downy, gate, incubation, powdery
from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.mildew import (
    downy_3_10,
    downy_incubation,
    powdery_gubler_thomas,
)

MODELS = [
    (downy_3_10, (*gate.THRESHOLDS, *downy.THRESHOLDS)),
    (downy_incubation, ("incub_table", *incubation.THRESHOLDS)),
    (powdery_gubler_thomas, powdery.THRESHOLDS),
]


@pytest.mark.parametrize(("module", "rule_names"), MODELS, ids=lambda m: getattr(m, "__name__", ""))
@pytest.mark.parametrize(("region", "year"), [("FRI12", 2024), ("ITH55", 2025), ("XX000", 2026)])
def test_every_mildew_threshold_is_its_spec_parameter(module, rule_names, region, year):
    config = AdvisorConfig.for_region_year(region, year)
    spec = module.SPEC
    assert getattr(specs, spec.key.split(".")[1].upper()) is spec  # the manifest's path
    assert [p.name for p in spec.parameters] == list(rule_names)
    for p in spec.parameters:
        if p.name == "incub_table":
            assert config.incub_table is module.GOIDANICH_TABLE
            continue
        t = config.threshold(p.name)
        declared = AdvisorConfig.model_fields[p.name].annotation
        # Defaults are not validated: a 10 for a Threshold[float] would print "10", not "10.0".
        assert type(t.value) is declared.__pydantic_generic_metadata__["args"][0], p.name
        if p.unit == "date":  # dated per region and year: the config holds the date
            assert isinstance(t.value, date) and p.value is None and t.assumed
            continue
        assert (type(t.value), t.value, t.unit, t.source, t.assumed) == (
            type(p.value),
            p.value,
            p.unit,
            p.source,
            p.status == "assumed",
        ), p.name
