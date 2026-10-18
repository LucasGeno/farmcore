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


def take(app, value: str | None) -> tuple[str, bool]:
    try:
        token = str(UUID(value or ""))
    except (ValueError, AttributeError):
        token = str(uuid4())
    now = monotonic()
    with _lock:
        if not hasattr(app.state, "assistant_limits"):
            app.state.assistant_limits = OrderedDict()
        buckets = app.state.assistant_limits
        times = buckets.pop(token, deque())
        while times and times[0] <= now - WINDOW:
            times.popleft()
        allowed = len(times) < LIMIT
        if allowed:
            times.append(now)
        buckets[token] = times
        while len(buckets) > MAX_BROWSERS:
            buckets.popitem(last=False)
    return token, allowed
