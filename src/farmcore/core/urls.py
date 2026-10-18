"""Carry explicit farm context through local read URLs; endpoints still authorize each read."""

from urllib.parse import parse_qsl, urlencode, urlsplit


def with_farm(path: str, farm_id: int) -> str:
    """Qualify a local URL without changing its other query fields or fragment.

    External URLs, fragment-only links and unsafe redirect shapes stay untouched.
    Callers qualify owner-farm URLs only, not static or grant-bound links.
    This function carries context, never access rights.
    """
    if not path.startswith("/") or path.startswith(("//", "/\\")):
        return path
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        return path
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "farm"
    ]
    query.append(("farm", str(farm_id)))
    return parts._replace(query=urlencode(query)).geturl()
