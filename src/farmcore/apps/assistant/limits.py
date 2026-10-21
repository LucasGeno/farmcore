"""Small process-local browser quota; intentionally no database or replay-clock dependency."""

from collections import OrderedDict, deque
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

COOKIE = "farmcore_assistant_browser"
LIMIT = 10
WINDOW = 60
MAX_BROWSERS = 1024
_lock = Lock()


def take(app, value: str | None, identity_id: int | None = None) -> tuple[str, bool]:
    """Count one request. A signed-in member's bucket is their identity, so clearing the cookie
    does not reset it; anonymous browsers are counted by the cookie token (returned to set)."""
    try:
        token = str(UUID(value or ""))
    except (ValueError, AttributeError):
        token = str(uuid4())
    now = monotonic()
    with _lock:
        if not hasattr(app.state, "assistant_limits"):
            app.state.assistant_limits = OrderedDict()
        buckets = app.state.assistant_limits
        key = f"identity:{identity_id}" if identity_id is not None else token
        times = buckets.pop(key, deque())
        while times and times[0] <= now - WINDOW:
            times.popleft()
        allowed = len(times) < LIMIT
        if allowed:
            times.append(now)
        buckets[key] = times
        while len(buckets) > MAX_BROWSERS:
            buckets.popitem(last=False)
    return token, allowed
