"""Kernel models. Importing this package registers every core table on `Base.metadata`."""

from farmcore.core.models import canonical, identity, kernel  # noqa: F401
from farmcore.core.models.base import Base

__all__ = ["Base"]
