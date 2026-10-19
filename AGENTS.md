# AGENTS.md — FarmCore

FarmCore is an open-source (Apache-2.0) farm records and decision-support platform: one FastAPI process, PostgreSQL + PostGIS, a small core and installable apps in the Odoo style. This repository is **public**.

## Read first
- [CONTRIBUTING.md](CONTRIBUTING.md): setup, checks, conventions, writing an app.
- [docs/architecture.md](docs/architecture.md): core, apps, data model, the AI boundary.
- [CONTEXT.md](CONTEXT.md): domain glossary (Installation, Farm, Record, Planned job, Background task, Assessment…).
- [SECURITY.md](SECURITY.md): access model.

## Commands
```sh
docker compose -f docker-compose.dev.yml up -d db   # PostGIS on localhost:55442
uv sync && uv run farmcore migrate
scripts/check.sh                                    # data guard, ruff lint + format, pytest
PYTEST_ADDOPTS="-m 'not data'" scripts/check.sh     # skip the slower dataset tests
node --test tests/core/*.mjs tests/apps/fields/*.mjs
uv run alembic heads                                # must print one head
```

## Rules
- **Commits.** Conventional Commits. Behaviour changes come with tests; bug fixes start with a failing test.
- **Migrations.** One linear chain. During coordinated work, only the integration coordinator authors revision files.
- **Agronomy.** Every threshold has a published source or is marked `assumed`. No magic numbers in rules.
- **Licences.** Never copy GPL, AGPL or EUPL code into this repository. External data keeps its licence and attribution.
- **Never commit** credentials, raw datasets, private farm data or planning documents.

## Planning
Planning artifacts (specs, tickets, research, coordination) live outside this public repository, in the maintainer's private planning repo. The exception is `docs/work/farmcore-revamp/`, which moves there once its integration QA closes. Contributors: open an issue to propose work.
