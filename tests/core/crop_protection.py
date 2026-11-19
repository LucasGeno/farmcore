"""Crop protection and the apps documented as extending it (docs/architecture.md, "Bundles
without crop protection"): every other app must work in a bundle that leaves these out."""

CROP_PROTECTION = frozenset({"advisor", "kpi", "planning", "review"})
