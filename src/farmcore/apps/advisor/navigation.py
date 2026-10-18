"""App-owned parcel entry links; canonical advice URLs remain unchanged."""

from urllib.parse import urlencode

from fastapi import Request

from farmcore.core.navigation import resolved_url


def parcel_link(request: Request, parcel_id: int, origin: str) -> str:
    return (
        resolved_url(request, f"/fields/parcels/{parcel_id}?view=protection")
        + "&"
        + urlencode({"return_to": resolved_url(request, origin)})
    )
