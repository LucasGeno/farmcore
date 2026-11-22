# Oracle fixture harness

FarmCore implements its agronomic models clean-room from the published papers. Several open
implementations of the same methods exist, but they are copyleft (GPL, AGPL), so their code
must never enter this repository. This harness runs them **out of process**, in an optional
container, only to produce golden numbers that FarmCore's own implementations are tested against.

- Nothing here is packaged (`pyproject.toml` builds only `src/farmcore`), and the package
  imports nothing from it. `tests/test_oracle_fixtures.py` checks this two ways: a static scan
  of `src/farmcore` for imports of `tools`, and (marked `slow`) a fresh interpreter that imports
  every `farmcore` module and records any attempt to load `tools`, however the name was built.
- The container installs the copyleft packages at build time from their upstream source,
  pinned by version (or commit) and checksum. No copyleft source is vendored. The images are
  local only and never pushed.
- CI never runs the container. It reads the stored fixtures under `tests/fixtures/oracles/`.
- The generator scripts in this directory are FarmCore's own (Apache-2.0); they only call the
  oracle's public functions (or, for plasmopy, run its program) and write JSON.

## Regenerating fixtures

```sh
tools/oracles/run.sh            # degday, viticolr and plasmopy, one after another
tools/oracles/run.sh degday     # one oracle
```

This first rewrites the synthetic season (`inputs/make_season.py`, host `python3`, stdlib only)
to `tests/fixtures/oracles/inputs/season-2025.csv`. Then, one oracle at a time, it builds that
oracle's target of `tools/oracles/Dockerfile` as `farmcore-oracles-<oracle>:local` and runs its
generator with no network, as your user, with the inputs mounted read-only, writing
`tests/fixtures/oracles/<oracle>/*.json`. Review the diff and commit it. A version bump is a
Dockerfile change (version or commit, and checksum) plus regenerated fixtures in the same commit.

## Fixture format (`farmcore-oracle-fixture/1`)

One JSON file per oracle run:

- `generator`: `name`, `version`, `licence` (as the upstream package declares it), `source`,
  `source_sha256`, `authors`, `runtime`, and how it was run;
- `method_reference`: the paper the oracle implements;
- `call`: the oracle function that produced the numbers;
- `parameters`: every argument that shapes the result (thresholds, method, cutoff, rounding);
- `inputs`: the input series with units, plus a label per row naming the case it exercises;
  or, for the hourly downy mildew runs, `file` (relative to `tests/fixtures/oracles/`) and its
  `sha256`, so the shared season is stored once;
- `outputs`: the oracle's results with units.

Generated numbers are facts, not copyrightable expression, but the provenance block is
mandatory: a fixture without generator, version, licence, parameters, inputs and outputs, or
whose input file no longer matches its checksum, fails `tests/test_oracle_fixtures.py`.

## The synthetic season (`inputs/make_season.py`)

FarmCore's own deterministic hourly weather, 1 January to 31 July 2025 (UTC, 5088 hours):
temperature, relative humidity, rain and leaf-wetness minutes, shaped to exercise oospore
maturation, germination, dispersal and infection (a seasonal temperature ramp, a rain event
every 5 days with every third one heavy, wet nights after rain). The docstring states every
rule. It is not a real site and not an agronomic wetness estimate; no third-party observations
are used, so it is free to commit.

## Oracles

### degday (Zalom et al. 1983 degree-days): fixtures committed

- Package: degday 0.4.0 (CRAN, 2022-09-13), Andy Lyons / Regents of the University of
  California. Licence `GPL (>= 3)`. Tarball sha256
  `1e0ca4504d92c78641defe36d4df067e17d9e49ea969aa48a2bb40680afd451d`.
- Runtime: R 4.4.2 (`rocker/r-ver:4.4.2`, CRAN pinned to its dated snapshot).
- Generator: `degday/generate.R` calls `degday::dd_calc` on nine crafted days, one per Zalom case
  against a 10/30 degC window (below, crossing lower, between, crossing upper, crossing both,
  above) plus edge days (min and max on the thresholds, a flat day, the last day of a series).
  The 10/30 thresholds are harness case parameters, not agronomic claims.
- Fixtures: `tests/fixtures/oracles/degday/{sng_sine,dbl_sine,sng_tri,dbl_tri}-horizontal.json`
  and `-upper_200.json`, rounded by degday to 6 decimals.
- **Finding: degday 0.4.0 supports only the horizontal cutoff.** `cutoff = "vertical"` and
  `"intermediate"` stop with "not yet supported" for every method. The generator still asks
  for them, so a future version that adds them produces fixtures on regeneration. Until then
  the vertical cutoff that the crop-models spec requires has no degday oracle; its golden cases
  must come from the paper's worked examples or another source.
- **Finding: degday needs an upper threshold** for the sine and triangle methods. Its own error
  message says to pass a large number such as 200 when there is none; the `-upper_200` fixtures
  are that "no upper threshold" case, with 200 recorded in `parameters`.
- For the double methods, `nextday_min` is the next row's minimum; the last day uses its own
  minimum (degday's documented default).

### viticolR (Rossi et al. 2008 downy mildew primary infection): fixtures committed

- Package: https://github.com/PaulMelloy/viticolR, `DESCRIPTION` version 0.0.4, licence
  `GPL (>= 3)`. No tags or releases: pinned to commit `f32eb0abe02d49882eefe2c957e90776fac74b59`
  (2025-11-09), GitHub tarball sha256
  `03a06d5073bdac85a6510dc1c5812dfdf19bc4983e5102cb6c391aa557f9693b`. It says it is
  "translated and adapted" from the paper, so differences from Rossi 2008 must be checked
  case by case before treating a mismatch as FarmCore's bug.
- Dependency: `epiphytoolR` 0.0.4 (same author, `MIT + file LICENSE`), pinned to commit
  `3cab18e878f819f882e25a2c5c51112910864967` (2026-06-18), tarball sha256
  `c05a07a111fd659879d379c656b793b44ae704c1f453e7edb2211fafe0c6c309`. It imports `sf`, so the
  image adds GDAL, GEOS, PROJ and udunits.
- Generator: `viticolr/generate.R` formats the season with `epiphytoolR::format_weather()`
  (wind constants and a nominal station at 46 N 7 E, which the model does not use), runs
  `estimate_DM_PI(Start = 2025-01-01, End = 2025-07-31, SOD = 1)` and records
  `get_PI_dates()` (each oospore cohort's stage times) plus end-of-day hydrothermal time and
  physiologically mature oospores.
- Fixture: `tests/fixtures/oracles/viticolr/season-2025.json`.
- **Finding: the season is southern-hemisphere by default.** `Start`/`End = "detect"` runs
  from 1 July and reads the system clock. The generator passes explicit dates.
- Its bundled `nt_weather` data is Bureau of Meteorology observations with their own licence;
  the fixtures use the synthetic season instead.

### plasmopy (Agroscope downy mildew infection model): fixtures committed

- Program: https://github.com/agroscope-ch/plasmopy, licence AGPL-3.0, `pyproject.toml` version
  1.0. No tags or releases: pinned to commit `2fea0cb3dfc9e5892f231328a03447ea381dd3f8`
  (2026-08-27), GitHub tarball sha256
  `924133672435ac5209bbf9cb0f89d1c520cb5f7e5031020bdbb0d21cd26d1a36`. Upstream has no lock
  file; the image pins the runtime imports of `src/` (versions in the Dockerfile).
- **Finding: plasmopy is not a Rossi 2008 implementation.** It is "a Python version of the
  predictive model in use at Agroscope" (VitiMeteo Plasmopara). Its primary infection finishes
  on leaf wetness and degree-hour accumulation. Use it as a cross-check of event timing, not as
  a golden oracle for Rossi outputs.
- Generator: `plasmopy/generate.py` writes the season in plasmopy's input layout (each hour as
  six 10-minute rows, its default interval), runs `src/main.py` with Hydra overrides (no spore
  trap, a nominal site for sunrise and sunset, oospore maturation computed rather than
  upstream's preset 2025 date; every other parameter as shipped) and records its
  `infection_datetimes` table. The fixture's `parameters` carry the overrides and the full
  resolved model configuration.
- Fixture: `tests/fixtures/oracles/plasmopy/season-2025.json`.

### NIBIO VIPS: not obtainable from this host

- **Finding:** its source host `gitlab.nibio.no` timed out from the build host on two checks
  (2026-11-22), while `www.vips-landbruk.no` answered. Only `nibio-org/vips-observation-app` (a
  mobile app, no licence declared) is on GitHub. Neither the licence nor a version of a
  grapevine downy mildew model could be confirmed, so there are no fixtures.
