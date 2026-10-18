# Contributing

Thanks for helping. Bug reports, agronomic corrections (with a source) and pull requests are
welcome. For larger changes, open an issue first so we can agree on the approach.

Report security problems privately: see [SECURITY.md](SECURITY.md).

## Setup

Python 3.12, [uv](https://docs.astral.sh/uv/), Docker; Node only to rebuild CSS.

```sh
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d db   # PostGIS on localhost:55442
uv sync
uv run farmcore migrate
uv run uvicorn farmcore.main:create_app --factory --reload
```

`DATABASE_URL` defaults to `postgresql+psycopg://farmcore:farmcore@localhost:55442/farmcore`.
To keep several checkouts apart, create a database per checkout on the same server and point
`DATABASE_URL` at it.

## Checks

```sh
scripts/check.sh                                   # data guard, ruff lint + format, pytest
PYTEST_ADDOPTS="-m 'not data'" scripts/check.sh    # skip the slower dataset tests
node --test tests/core/*.mjs tests/apps/fields/*.mjs
```

- Tests create and drop their own `farmcore_test_<hex>` database on the configured server
  and never read `.env`. Never point `TEST_DATABASE_URL` at a production server.
- Tests marked `data` replay the demo datasets in `sample-data/` (or a local `data/raw`, which
  wins when present) and are slower. Tests that need datasets which are not bundled skip
  without them.
- CI runs ruff, the non-`data` tests against PostGIS, and the Node tests.

## Conventions

- [Conventional Commits](https://www.conventionalcommits.org/): `feat(advisor): …`,
  `fix(operations): …`, `docs: …`.
- Behaviour changes come with tests; bug fixes start with a failing test.
- Ruff formats and lints (line length 100). Keep comments for the why.
- Every agronomic threshold lives in `apps/advisor/engine/config.py` with its source; a value
  without one is marked `assumed`. Never add a magic number to a rule.
- Raw datasets, `.env` files and credentials are never committed (`data/`, `secrets/` and
  `backups/` are ignored; `scripts/check.sh` guards against archives and spreadsheets).
- Template class changes need the CSS rebuilt: `scripts/build_css.sh`, then commit the
  generated file and update its digest in `src/farmcore/core/static/vendor/README.md`.

## Writing an app

An app is a package under `src/farmcore/apps/<name>/` with a `manifest.py` exporting `MANIFEST`
(`farmcore.core.manifest.Manifest`): its `depends`, consent `scopes`, `routes`, `jobs`, `seed`,
and what it adds to the UI (`nav`, Today `widgets`, `map_layers`, map panel sections, debug
panels, read-only assistant `tools`). The core never imports an app; it renders what enabled
apps declare. Read farm data through `ScopedRepository` so grants and the access log apply.
Migrations go in `src/farmcore/migrations/versions/` as `<app>_<NNNN>_<slug>.py` on the single
linear chain (`uv run alembic heads` must print one head).

## Licence

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
