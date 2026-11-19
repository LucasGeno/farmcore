"""Kernel models. Importing this package registers every core table on `Base.metadata`."""

from farmcore.core.models import (  # noqa: F401
    assessment,
    canonical,
    catalog,
    corrections,
    drafts,
    identity,
    kernel,
    weather,
)
from farmcore.core.models.base import Base

__all__ = ["Base"]
