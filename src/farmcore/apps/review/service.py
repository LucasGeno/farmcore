"""Scoped immutable versions, opinions and activations; reads never seed state."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.review.models import ParameterActivation, ParameterReview, ParameterVersion
from farmcore.apps.review.parameters import apply_overrides, catalog
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Farm

SCOPE = "parameters:read"
SCOPE_KEYS = ("farm_id", "crop", "region", "evaluation_year")


def scope_for(farm: Farm, now: datetime, tz: str) -> dict:
    return {
        "farm_id": farm.id,
        "crop": "vine",
        "region": str((farm.props or {}).get("region") or "FRI12"),
        "evaluation_year": now.astimezone(ZoneInfo(tz)).year,
    }


def match(model, scope):
    return tuple(getattr(model, key) == value for key, value in scope.items())


def digest(config: AdvisorConfig) -> str:
    return sha256(
        json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Effective:
    config: AdvisorConfig
    scope: dict
    version: ParameterVersion | None
    review: ParameterReview | None

    @property
    def status(self):
        return self.review.verdict if self.review else "unreviewed"

    def identity(self):
        return {
            "id": self.version.id if self.version else None,
            # The digest of the configuration that actually ran; a read verifies the named
            # version row against it.
            "hash": digest(self.config),
            "status": self.status,
            "baseline": self.version.baseline if self.version else True,
            "scope": self.scope,
            "review": (
                {
                    "id": self.review.id,
                    "verdict": self.review.verdict,
                    "status": self.review.verdict,
                }
                if self.review
                else None
            ),
            "accuracy_claim": False,
        }


def latest_review(repo, version_id):
    rows = repo.list(
        ParameterReview,
        ParameterReview.version_id == version_id,
        scope=SCOPE,
        order_by=ParameterReview.id.desc(),
        limit=1,
    )
    return rows[0] if rows else None


def resolve(repo: ScopedRepository, now: datetime, tz: str, *, seed=False) -> Effective:
    from farmcore.apps.advisor.service import config_for

    repo.check(SCOPE, "parameter_version")

    farm = repo.get(Farm, repo.farm_id, scope=SCOPE)
    scope = scope_for(farm, now, tz)
    activations = repo.list(
        ParameterActivation,
        *match(ParameterActivation, scope),
        scope=SCOPE,
        order_by=ParameterActivation.id.desc(),
        limit=1,
    )
    activation = activations[0] if activations else None
    if activation:
        version = repo.get(ParameterVersion, activation.version_id, scope=SCOPE)
        return Effective(
            AdvisorConfig.model_validate(version.effective_config),
            scope,
            version,
            latest_review(repo, version.id),
        )
    config = config_for(farm, now, tz)
    versions = repo.list(
        ParameterVersion,
        *match(ParameterVersion, scope),
        ParameterVersion.baseline.is_(True),
        scope=SCOPE,
        limit=1,
    )
    version = versions[0] if versions else None
    if version is None and seed:
        repo.session.execute(
            insert(ParameterVersion)
            .values(
                **scope,
                created_by=repo.actor,
                created_at=datetime.now(UTC),
                evidence="Unchanged typed defaults; explicitly unreviewed baseline",
                overrides={},
                parameter_catalog=catalog(config),
                effective_config=config.model_dump(mode="json"),
                config_hash=digest(config),
                baseline=True,
            )
            .on_conflict_do_nothing()
        )
        version = repo.list(
            ParameterVersion,
            *match(ParameterVersion, scope),
            ParameterVersion.baseline.is_(True),
            scope=SCOPE,
            limit=1,
        )[0]
    return Effective(config, scope, version, latest_review(repo, version.id) if version else None)


def version_for(repo, version_id):
    return repo.get(ParameterVersion, version_id, scope=SCOPE)


def propose(repo, now, tz, overrides, evidence, parent_version_id=None):
    current = resolve(repo, now, tz)
    parent = current.version if parent_version_id is None else version_for(repo, parent_version_id)
    if parent_version_id is not None and parent is None:
        raise ValueError("Parent version is unavailable for this scope")
    if parent is not None and any(
        getattr(parent, key) != value for key, value in current.scope.items()
    ):
        raise ValueError("Parent version is unavailable for this scope")
    base = AdvisorConfig.model_validate(parent.effective_config) if parent else current.config
    config = apply_overrides(base, overrides)
    if parent is None:
        parent = resolve(repo, now, tz, seed=True).version
    version = ParameterVersion(
        **current.scope,
        parent_version_id=parent.id,
        created_by=repo.actor,
        created_at=datetime.now(UTC),
        evidence=evidence,
        overrides=overrides,
        parameter_catalog=catalog(config),
        effective_config=config.model_dump(mode="json"),
        config_hash=digest(config),
        baseline=False,
    )
    repo.session.add(version)
    repo.session.flush()
    return version


def version_json(repo, version):
    review = latest_review(repo, version.id)
    return {
        "id": version.id,
        "scope": {
            key: getattr(version, key) for key in ("farm_id", "crop", "region", "evaluation_year")
        },
        "parent_version_id": version.parent_version_id,
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat(),
        "evidence": version.evidence,
        "overrides": version.overrides,
        "parameter_catalog": version.parameter_catalog,
        "effective_config": version.effective_config,
        "config_hash": version.config_hash,
        "baseline": version.baseline,
        "status": review.verdict if review else "unreviewed",
        "accuracy_claim": False,
    }
