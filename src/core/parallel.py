# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Daemon-thread fan-out used by every network-bound part of the app.

``concurrent.futures.ThreadPoolExecutor`` spawns **non-daemon** workers and
registers an interpreter ``atexit`` hook that joins them.  Two consequences,
both of them visible to the user:

* a headless run (``--self-test``) prints its last line, then the process sits
  there until the slowest in-flight socket probe times out -- it looks hung;
* closing the window mid-search leaves ``TSearchDS.exe`` in Task Manager,
  because an unreachable tracker's read timeout can be tens of seconds.

Everything here runs on daemon threads and never delays process exit.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any, Callable, Iterable, List, Optional, Tuple

from . import gateway

log = logging.getLogger("tsearch.parallel")

_ids = itertools.count(1)


def pmap(fn: Callable[[Any], Any], items: Iterable[Any], workers: int = 8,
         timeout: Optional[float] = None,
         cancel: Optional[threading.Event] = None,
         stop_when: Optional[Callable[[Any], bool]] = None,
         on_result: Optional[Callable[[Any, Any, Optional[BaseException]], None]]
         = None) -> List[Tuple[Any, Any, Optional[BaseException]]]:
    """Call ``fn(item)`` for every item, concurrently, on daemon threads.

    Returns ``(item, value, error)`` triples **in completion order**, stopping
    early when ``timeout`` expires, when ``stop_when(value)`` says the answer
    is good enough, or when ``cancel`` is set.  Work that has not started by
    then is simply dropped -- the point is that the caller never waits for a
    job it has stopped caring about.

    ``on_result`` and ``stop_when`` are invoked with a lock held, so a caller
    that mutates shared state (or drives the UI) from them sees exactly the
    serialised, one-at-a-time behaviour the old ``as_completed`` loop had.

    Workers inherit the caller's :mod:`src.core.gateway` context -- priority,
    deadline, cancel event -- so a phase's budget reaches the actual requests
    even though they are issued from a nested pool several levels down.
    """
    pending = list(items)
    if not pending:
        return []
    workers = max(1, min(workers or 8, len(pending)))

    parent_ctx = gateway.current()

    lock = threading.Lock()
    finished: List[Tuple[Any, Any, Optional[BaseException]]] = []
    done = threading.Event()
    cursor = itertools.count()

    def worker(idx: int) -> None:
        gateway.bind(parent_ctx)
        while True:
            if cancel is not None and cancel.is_set():
                return
            with lock:
                pos = next(cursor, None)
                if pos is None or pos >= len(pending) or done.is_set():
                    return
                item = pending[pos]
            try:
                value: Any = fn(item)
                error: Optional[BaseException] = None
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                value, error = None, exc
            with lock:
                finished.append((item, value, error))
                if on_result is not None:
                    try:
                        on_result(item, value, error)
                    except Exception:  # noqa: BLE001
                        log.debug("on_result callback failed", exc_info=True)
                if error is None and stop_when is not None and stop_when(value):
                    done.set()
                elif len(finished) >= len(pending):
                    done.set()

    for i in range(workers):
        threading.Thread(target=worker, args=(i,), daemon=True,
                         name="pmap-%d-%d" % (next(_ids), i)).start()

    deadline = None if timeout is None else time.monotonic() + timeout
    while not done.is_set():
        if cancel is not None and cancel.is_set():
            break
        step = 0.2
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            step = min(step, left)
        done.wait(step)

    with lock:
        return list(finished)
