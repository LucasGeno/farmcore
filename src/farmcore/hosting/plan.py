"""A declared fleet as a deterministic plan: resource names follow from the slug, and nothing is
read from the host, Docker or the secrets the declarations refer to."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from farmcore.hosting.inventory import Declaration, DomainPolicy, read_inventory

# The named volumes of docker-compose.yml, which Compose prefixes with the project name.
_COMPOSE_VOLUMES = ("backups", "pgdata", "whisper-cache")


def _installation(declaration: Declaration) -> dict[str, Any]:
    slug = declaration.slug
    project = f"farmcore-fleet-{slug}"
    return {
        "slug": slug,
        "domain": declaration.domain,
        "images": declaration.images.model_dump(),
        "binding": declaration.binding.model_dump(),
        "limits": declaration.limits.model_dump(),
        "compose_project": project,
        "network": f"{project}_default",
        "volumes": [f"{project}_{volume}" for volume in _COMPOSE_VOLUMES],
        "state_dir": f"installations/{slug}",
        # Names only: a reference holding a pasted value must not reach the output.
        "secrets": sorted(declaration.secrets.model_dump()),
    }


def build_plan(folder: Path, policy: DomainPolicy) -> dict[str, Any]:
    declarations = read_inventory(folder, policy)
    return {
        "describes": "declared inventory only; no live state was read",
        "domain_policy": "test-domains" if policy.test_domains else "allowlist",
        "installations": [
            _installation(declaration)
            for declaration in sorted(declarations, key=lambda item: item.slug)
        ],
    }
