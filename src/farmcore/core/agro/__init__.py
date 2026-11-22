"""Shared agronomic layers that models build on: degree-days, leaf wetness and BBCH phenology.

Pure, deterministic functions with no database or network access. They hold no agronomic
thresholds of their own except the leaf-wetness humidity proxy, which is a labelled
`Parameter` (`leaf_wetness.RH_WET_THRESHOLD`); a model passes its own sourced or assumed
values and declares them in its `ModelSpec`.
"""
