"""Forecast-only DNS admission: at most two native lookups may outlive a request.

Native getaddrinfo cannot be killed safely. Slots stay occupied until native work
finishes, while canceled HTTP awaits return immediately and never resume. These
fixed daemon workers are lazy and are not the event loop's shutdown executor.
"""

import asyncio
import socket
from concurrent.futures import Future
from queue import Queue
from threading import BoundedSemaphore, Lock, Thread

_slots = BoundedSemaphore(2)
_pending = Queue(maxsize=2)
_start_lock = Lock()
_started = False


def _work():
    while True:
        future, args, kwargs = _pending.get()
        try:
            if future.set_running_or_notify_cancel():
                try:
                    value = socket.getaddrinfo(*args, **kwargs)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(value)
        finally:
            _slots.release()
            _pending.task_done()


def _start():
    global _started
    with _start_lock:
        if not _started:
            for index in range(2):
                Thread(target=_work, name=f"forecast-dns-{index}", daemon=True).start()
            _started = True


class FetchLoop(asyncio.SelectorEventLoop):
    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        if not _slots.acquire(blocking=False):
            raise socket.gaierror(socket.EAI_AGAIN, "forecast DNS capacity busy")
        source = Future()
        _start()
        _pending.put_nowait(
            (
                source,
                (host, port),
                {
                    "family": family,
                    "type": type,
                    "proto": proto,
                    "flags": flags,
                },
            )
        )
        # Await cancellation cancels queued work; running native work retains its slot.
        # The standard bridge discards results for canceled or already-closed loops.
        return await asyncio.wrap_future(source, loop=self)
