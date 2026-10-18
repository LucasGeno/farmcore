"""Farm shortlist selection; an explicit empty list never inherits another country's products."""

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Farm, Product


def products_for_farm(repo: ScopedRepository, *, scope: str, order_by=None) -> list[Product]:
    farm = repo.get(Farm, repo.farm_id, scope=scope)
    ids = (farm.props or {}).get("product_ids") if farm else None
    if ids == []:
        return []
    filters = (Product.id.in_(ids),) if ids is not None else ()
    return repo.list(
        Product, *filters, scope=scope, order_by=Product.name if order_by is None else order_by
    )
