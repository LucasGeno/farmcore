"""Short-lived, farm/actor-bound map snapshots; a claim never calls the provider.

The signing key is process-local: restart invalidates open maps, so reload them.
FarmCore serves one web process; multi-worker hosting would need a shared key.
"""

import hashlib
import hmac
import json
import secrets
import time

from . import MAX_BYTES, Unavailable

_KEY = secrets.token_bytes(32)
LIFETIME = 900  # Assumed product limit: review a map within fifteen minutes.
MESSAGE = "Reload the public map to select parcels, or import a file instead."


def sign(rows, farm_id, actor):
    payload = json.dumps(
        {"rows": rows, "farm_id": farm_id, "actor": actor, "expires": time.time() + LIFETIME},
        separators=(",", ":"),
    )
    if len(payload.encode()) > MAX_BYTES:
        raise Unavailable("Too many public parcels. Zoom in, or import a file instead.")
    signature = hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return signature + "." + payload


def verify(token, farm_id, actor):
    try:
        if not isinstance(token, str) or len(token.encode()) > MAX_BYTES + 65:
            raise ValueError
        signature, payload = token.split(".", 1)
        expected = hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(payload)
        if data["farm_id"] != farm_id or data["actor"] != actor or data["expires"] < time.time():
            raise ValueError
        return data["rows"]
    except (ValueError, KeyError, TypeError):
        raise Unavailable(MESSAGE) from None
