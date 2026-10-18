"""Bounded, disposable routing context. Farm records are always fetched anew.

Only questions and the selected tool/parcel survive a request; never tool facts or answers.
A process restart, clock/reset change, browser/tab change or 30 idle minutes starts fresh.
"""

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from time import monotonic

TTL = 30 * 60
MAX_CONVERSATIONS = 1024
MAX_TURNS = 3
_lock = Lock()


@dataclass(frozen=True)
class Turn:
    question: str
    tool: str | None
    parcel: str | None


def load(app, key, epoch):
    if key is None:
        return ()
    with _lock:
        store = getattr(app.state, "assistant_conversations", {})
        saved = store.get(key)
        if saved and saved[0] > monotonic() - TTL and saved[1] == epoch:
            return saved[2]
        store.pop(key, None)
    return ()


def save(app, key, epoch, turns):
    if key is None:
        return
    with _lock:
        if not hasattr(app.state, "assistant_conversations"):
            app.state.assistant_conversations = OrderedDict()
        store = app.state.assistant_conversations
        now = monotonic()
        for old in [k for k, v in store.items() if v[0] <= now - TTL]:
            del store[old]
        store.pop(key, None)
        store[key] = (now, epoch, tuple(turns[-MAX_TURNS:]))
        while len(store) > MAX_CONVERSATIONS:
            store.popitem(last=False)
