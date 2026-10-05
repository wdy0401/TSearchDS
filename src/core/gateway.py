# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""One gate for every outbound request, counted at the layer that matters.

What this replaced
------------------

The first attempt at this file counted *sources in flight* and *rows being
enriched*.  That is the wrong object.  One row fans out into a dozen tracker
scrapes -- ``enrich.http_scrape`` builds ``http_urls[:8] + udp_urls[:12]`` and
appends :data:`src.core.enrich.FALLBACK_TRACKERS` -- so "ten rows at a time"
was never a bound on traffic.  Measured with every outbound request replaced
by a counter (``probe_fanout.py``):

    one search    : 96 requests in flight at once
    two searches  : 144

Both go through the same mihomo node.  144 concurrent requests is what turns
into urllib3 "connection pool is full, discarding connection", thousands of DNS
lookups, and, from the user's side, a second tab that is slower and returns
fewer rows than the first one.

So the gate moved down to the one place every request actually passes through:
:meth:`src.core.http.Http.get` (plus the UDP trackers, which open their own
socket and used to bypass everything -- see :func:`slot`).

Rules
-----

* **count requests, not rows** -- ``CAP`` in flight, process-wide;
* **background yields** -- 资源数 lookups may hold at most ``LOW_CAP`` of them,
  so an index fetch somebody is staring at can always get on the wire;
* **one host at a time, mostly** -- at most ``PER_HOST`` concurrent requests to
  the same host no matter how many tabs asked.  Two tabs hitting the same index
  at once used to trip its rate limit, and a 429 came back as "this source
  returned nothing" -- the random missing-source symptom;
* **first come, first served** -- :class:`threading.Semaphore` lets a thread
  that just arrived take a slot ahead of one that has been waiting, so a search
  that started first could keep stealing the pipe from the one behind it;
* **the budget reaches the wire** -- a phase's deadline and a tab's cancel event
  ride along with the thread pool (:func:`src.core.parallel.pmap`), so work
  nobody is waiting for stops *sending* instead of lingering for seconds past
  its budget.

Nothing here knows about searches.  The caller says "somebody is waiting for
this" (``high=True``) or "this is polish" (``high=False``) with :func:`scope`,
and nested thread pools inherit it.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Deque, Dict, Hashable, Optional, Tuple
from urllib.parse import urlsplit

#: requests in flight, process-wide
CAP = 20
#: slots that belong to foreground work only -- background may never take them
HIGH_RESERVE = 6
#: what background (资源数) traffic may hold: CAP - HIGH_RESERVE
LOW_CAP = CAP - HIGH_RESERVE
#: concurrent requests to one host, all tabs together
PER_HOST = 3
#: ...of which this many are kept for foreground work
PER_HOST_RESERVE = 1
#: never squeeze one search below this many slots, however many compete.
#: Set to a quarter of the pipe: with three searches open each still gets a
#: usable share instead of the last one starving.
OWNER_FLOOR = 5
#: how long a request may queue before it gives up and does not send
QUEUE_MAX = 12.0


class Gate:
    """A counting gate with two priority classes, FIFO inside each class.

    ``high_reserve`` slots can only ever be held by ``high=True`` waiters, so
    however much background traffic is queued, a request somebody is waiting
    for can always start.

    FIFO is the whole point: :class:`threading.Semaphore` has no queue, so a
    thread arriving now can take a slot ahead of one that has been waiting for
    seconds.  That is how a search that happened to start first starves the one
    behind it.
    """

    def __init__(self, cap: int, high_reserve: int = 0,
                 owner_floor: int = 0) -> None:
        self._cap = max(1, int(cap))
        self._reserve = max(0, min(int(high_reserve), self._cap - 1))
        #: never squeeze one owner below this, however many are competing
        self._floor = max(1, min(int(owner_floor) or self._cap, self._cap))
        self._cv = threading.Condition()
        self._used = 0
        #: owner -> slots currently held
        self._by_owner: Dict[Hashable, int] = {}
        self._hi: Deque[object] = deque()
        self._lo: Deque[object] = deque()
        #: observability
        self.peak = 0

    # -- internals -----------------------------------------------------
    @property
    def cap(self) -> int:
        return self._cap

    def _low_limit(self) -> int:
        return self._cap - self._reserve

    def _owner_limit(self, owner: Optional[Hashable]) -> int:
        """How many slots one owner may hold right now.

        FIFO alone is not fair: the search that started earlier always has a
        request at the head of the queue, so the one behind it starves.  Measured
        with two searches (every tracker answering): the first filled 24/24 rows,
        the second 19/24.  Capacity is therefore divided by how many owners are
        actually using it -- one search still gets the whole pipe.
        """
        if owner is None:
            return self._cap
        active = sum(1 for n in self._by_owner.values() if n > 0)
        if self._by_owner.get(owner, 0) == 0:
            active += 1          # this owner is about to become active
        if active <= 1:
            return self._cap
        return max(self._floor, self._cap // active)

    def _free(self, high: bool, owner: Optional[Hashable] = None) -> bool:
        if self._used >= self._cap:
            return False
        if not high:
            # The reserve exists so that a request somebody is waiting for can
            # always start.  When nobody is waiting, holding it idle is pure
            # waste -- and the 资源数 phase usually runs with no index fetch
            # in flight at all, so it should get the whole pipe.
            limit = self._cap if not self._hi else self._low_limit()
            if self._used >= limit:
                return False
        if self._by_owner.get(owner, 0) >= self._owner_limit(owner):
            return False
        return True

    # -- API -----------------------------------------------------------
    def acquire_until(self, end: float, high: bool = False,
                      owner: Optional[Hashable] = None) -> bool:
        """Wait for a slot until the wall clock reads ``end``.

        ``False`` means it did not open in time and the caller must not send.
        """
        token = object()
        q = self._hi if high else self._lo
        with self._cv:
            q.append(token)
            try:
                while True:
                    # head of *my* queue, and the right limit for my class
                    if q[0] is token and self._free(high, owner):
                        q.popleft()
                        self._used += 1
                        if owner is not None:
                            self._by_owner[owner] = \
                                self._by_owner.get(owner, 0) + 1
                        if self._used > self.peak:
                            self.peak = self._used
                        return True
                    left = end - time.monotonic()
                    if left <= 0:
                        return False
                    self._cv.wait(min(left, 0.25))
            finally:
                if token in q:
                    q.remove(token)
                    self._cv.notify_all()

    def release(self, owner: Optional[Hashable] = None) -> None:
        with self._cv:
            self._used -= 1
            if owner is not None:
                left = self._by_owner.get(owner, 0) - 1
                if left > 0:
                    self._by_owner[owner] = left
                else:
                    self._by_owner.pop(owner, None)
            self._cv.notify_all()

    # -- introspection -------------------------------------------------
    @property
    def used(self) -> int:
        with self._cv:
            return self._used

    def waiting(self, high: bool = True) -> int:
        q = self._hi if high else self._lo
        with self._cv:
            return len(q)


class Context:
    """What this thread's traffic is: priority, deadline, cancellation, owner.

    ``owner`` is whoever the traffic belongs to -- a :class:`SearchSession`,
    usually.  It is what makes two searches split the pipe instead of the
    first one taking all of it: with no owner limit, a search that started
    earlier keeps a request at the head of the queue the whole time, and the
    one behind it is still waiting when its budget runs out.
    """

    __slots__ = ("high", "deadline", "stop", "owner")

    def __init__(self, high: bool = True, deadline: float = 0.0,
                 stop: Optional[threading.Event] = None,
                 owner: Optional[Hashable] = None) -> None:
        self.high = bool(high)
        self.deadline = float(deadline or 0.0)
        self.stop = stop
        self.owner = owner

    @property
    def aborted(self) -> bool:
        if self.stop is not None and self.stop.is_set():
            return True
        return bool(self.deadline) and time.monotonic() >= self.deadline

    def left(self, timeout: float) -> float:
        """The wall clock one request may still cost; 0 if it is already moot."""
        if self.aborted:
            return 0.0
        t = max(0.0, float(timeout))
        if self.deadline:
            t = min(t, self.deadline - time.monotonic())
        return max(0.0, t)


#: Threads that never asked -- GUI, proxy control, subscription refresh -- are
#: user-facing work: they are what a click is waiting on.
DEFAULT = Context(high=True)

_local = threading.local()


def current() -> Context:
    return getattr(_local, "ctx", None) or DEFAULT


def bind(ctx: Context) -> None:
    """Install ``ctx`` on this thread (used by the thread pools to inherit)."""
    _local.ctx = ctx


@contextmanager
def scope(high: bool = True, deadline: float = 0.0,
          stop: Optional[threading.Event] = None,
          owner: Optional[Hashable] = None):
    """Mark everything this thread -- and any pool it fans out to -- sends."""
    old = getattr(_local, "ctx", None)
    _local.ctx = Context(high, deadline, stop, owner)
    try:
        yield _local.ctx
    finally:
        _local.ctx = old


# -- the gates ---------------------------------------------------------
_NET = Gate(CAP, HIGH_RESERVE, OWNER_FLOOR)
_HOSTS: Dict[str, Gate] = {}
_HOSTS_LOCK = threading.Lock()


def host_gate(host: str) -> Gate:
    with _HOSTS_LOCK:
        g = _HOSTS.get(host)
        if g is None:
            # a reserve here too: otherwise a background 资源数 storm against
            # one tracker host can sit in front of an index fetch
            g = _HOSTS[host] = Gate(PER_HOST, PER_HOST_RESERVE)
        return g


def host_of(url: str) -> str:
    """The rate-limit subject for ``url``.

    UDP trackers have no path to speak of and are not HTTP at all, but they are
    the same host for the purpose of "how many are we sending there".
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    if parts.port:
        return "%s:%d" % (host, parts.port)
    return host


class Handle:
    """A held slot: remembers which gates to give back, and for whom."""

    __slots__ = ("_host", "_owner")

    def __init__(self, host: Optional[Gate],
                 owner: Optional[Hashable]) -> None:
        self._host = host
        self._owner = owner


def acquire(timeout: float, host: str = "") -> Optional[Handle]:
    """Ask for permission to put one request on the wire.

    Waits at most ``min(timeout, what the phase still has, QUEUE_MAX)`` across
    *both* gates -- a single absolute deadline, so queueing for a host slot
    cannot silently double the wait.  ``None`` means do not send.
    """
    ctx = current()
    left = ctx.left(timeout)
    if left <= 0:
        return None
    left = min(left, QUEUE_MAX)
    end = time.monotonic() + left
    high = ctx.high
    owner = ctx.owner
    hg = host_gate(host) if host else None
    if hg is not None and not hg.acquire_until(end, high, owner):
        return None
    if not _NET.acquire_until(end, high, owner):
        if hg is not None:
            hg.release(owner)
        return None
    return Handle(hg, owner)


def release(handle: Optional[Handle]) -> None:
    if handle is None:
        return
    _NET.release(handle._owner)
    if handle._host is not None:
        handle._host.release(handle._owner)


@contextmanager
def slot(timeout: float, url: str = ""):
    """``with gateway.slot(12.0, url) as h:`` -- ``h`` is None if you must
    not send.  Releases whatever it took, including on the way out of an
    exception.

    Takes a *URL*, not a host, so the UDP trackers can use it too: they open
    their own socket and never touch :mod:`src.core.http`, which is why they
    used to be invisible to every limit in this program.
    """
    h = acquire(timeout, host_of(url) if url else "")
    try:
        yield h
    finally:
        release(h)


def stats() -> Tuple[int, int, int]:
    """(in flight, waiting foreground, waiting background)."""
    return _NET.used, _NET.waiting(True), _NET.waiting(False)


def peak() -> int:
    """Highest number of simultaneous requests seen so far."""
    return _NET.peak


def reset_stats() -> None:
    with _NET._cv:
        _NET.peak = 0
