# Oracle fixture harness

FarmCore implements its agronomic models clean-room from the published papers. Several open
implementations of the same methods exist, but they are copyleft (GPL, AGPL), so their code
must never enter this repository. This harness runs them **out of process**, in an optional
container, only to produce golden numbers that FarmCore's own implementations are tested against.

- Nothing here is packaged (`pyproject.toml` builds only `src/farmcore`), and the package
  imports nothing from it (`tests/test_oracle_fixtures.py` checks this).
- The container installs the copyleft packages at build time from their upstream source,
  pinned by version and checksum. No copyleft source is vendored. The image is local only and
  never pushed.
- CI never runs the container. It reads the stored fixtures under `tests/fixtures/oracles/`.
- The generator scripts in this directory are FarmCore's own (Apache-2.0); they only call the
  oracle's public functions and write JSON.

## Regenerating fixtures

```sh
tools/oracles/run.sh            # every oracle
tools/oracles/run.sh degday     # one oracle
```

This builds `farmcore-oracles:local` from `tools/oracles/Dockerfile`, then runs each generator
with no network, as your user, writing `tests/fixtures/oracles/<oracle>/*.json`. Review the diff
and commit it. A version bump is a Dockerfile change (version and checksum) plus regenerated
fixtures in the same commit.

## Fixture format (`farmcore-oracle-fixture/1`)

One JSON file per oracle run:

- `generator`: `name`, `version`, `licence` (as the upstream package declares it), `source`,
  `source_sha256`, `authors`, `runtime`, and how it was run;
- `method_reference`: the paper the oracle implements;
- `call`: the oracle function that produced the numbers;
- `parameters`: every argument that shapes the result (thresholds, method, cutoff, rounding);
- `inputs`: the input series with units, plus a label per row naming the case it exercises;
- `outputs`: the oracle's results with units.

Generated numbers are facts, not copyrightable expression, but the provenance block is
mandatory: a fixture without generator, version, licence, parameters, inputs and outputs fails
`tests/test_oracle_fixtures.py`.

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

### viticolR (Rossi et al. 2008 downy mildew primary infection): not yet generated

- Source: https://github.com/PaulMelloy/viticolR, `DESCRIPTION` version 0.0.4, licence
  `GPL (>= 3)`; no tags or releases, so a run must pin a commit (latest seen:
  `f32eb0abe02d49882eefe2c957e90776fac74b59`, 2026-06-18). Depends on `epiphytoolR` from the same
  author on GitHub.
- Obtainable; not generated in this change.

### plasmopy (Agroscope downy mildew infection models): not yet generated

- Source: https://github.com/agroscope-ch/plasmopy, licence AGPL-3.0; no tags or releases, so a
  run must pin a commit (latest seen: `2fea0cb3dfc9e5892f231328a03447ea381dd3f8`, 2026-08-27).
- Obtainable; not generated in this change.

### NIBIO VIPS: not yet generated

- Not investigated in this change.
