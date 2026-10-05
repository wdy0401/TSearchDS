# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Source plugin base class and registry."""
from __future__ import annotations

import abc
import logging
import threading
import time
from typing import Dict, Iterable, List, Optional, Type

from ..models import SearchResult

log = logging.getLogger("tsearch.sources")

#: guards the per-source health counters below (they are process-wide, unlike
#: ``last_error`` which is per-thread)
_HEALTH_LOCK = threading.Lock()


class SourceError(Exception):
    pass


class Source(abc.ABC):
    """A single search backend.

    Subclasses implement :meth:`search` and declare metadata via class
    attributes.  ``kinds`` advertises which link families the source can
    produce; the UI uses it to filter.
    """

    #: stable machine id, must be unique
    id: str = "source"
    #: human label shown in the UI
    label: str = "Source"
    #: which link kinds this source returns
    kinds: tuple = ("magnet",)
    #: default enabled state
    default_enabled: bool = True
    #: rough per-request cost, used for ordering / timeouts
    weight: int = 10
    #: set False when the source needs the proxy to be reachable at all
    needs_proxy: bool = False
    #: response language hint
    regions: tuple = ()

    def __init__(self) -> None:
        self.enabled = self.default_enabled
        #: nominal per-request budget; :attr:`request_timeout` shrinks it for
        #: sources that keep coming back empty
        self._base_timeout: float = getattr(self, "_base_timeout", 12.0)
        #: Per-search state is thread-local: with one search per tab, two tabs
        #: run the *same* source object at the same time, and a shared
        #: ``last_error`` would report one tab's failure in the other tab's
        #: summary.  The aggregator reads these from the same worker thread
        #: that ran the search, so each tab sees its own.
        self._local = threading.local()
        #: Process-wide health, used to order the fan-out.  ``last_*`` above is
        #: thread-local because two tabs run the same source object at once;
        #: this is deliberately *not*, because "this index has returned
        #: nothing all session" is a fact about the source, not about a tab.
        self._empty_streak = 0
        self._avg_latency = 0.0
        self._probes = 0

    # -- request budget ------------------------------------------------
    @property
    def request_timeout(self) -> float:
        """Per-request budget, tightened for sources that keep coming back empty.

        A source that has answered nothing for several searches in a row is
        unreachable far more often than it is merely slow.  It still costs the
        full transport-fallback chain -- ``HTTP.get`` caps that at
        ``timeout * 1.6``, so a 12s budget burns ~19s -- while holding a slot
        that a working index could have used.  That is what makes the *second*
        search time out: its healthy sources are queued behind six dead ones.

        The first success restores the full budget, so a source that is only
        slow (or was temporarily rate-limited) is never permanently punished.
        """
        base = self._base_timeout
        streak = self.empty_streak
        if streak >= 3:
            return max(5.0, base * 0.40)
        if streak == 2:
            return max(6.0, base * 0.60)
        if streak == 1:
            return max(7.0, base * 0.80)
        return base

    @request_timeout.setter
    def request_timeout(self, value: float) -> None:
        self._base_timeout = float(value)

    # -- process-wide health -------------------------------------------
    @property
    def empty_streak(self) -> int:
        """Consecutive searches that produced nothing (or raised)."""
        with _HEALTH_LOCK:
            return self._empty_streak

    def _record(self, produced: bool, latency: float) -> None:
        with _HEALTH_LOCK:
            self._probes += 1
            if produced:
                self._empty_streak = 0
            else:
                self._empty_streak += 1
            # Only real answers shape the latency average.  A 20s "answer" is
            # a timeout, and that is already expressed by the empty streak.
            if produced:
                if self._avg_latency:
                    self._avg_latency = self._avg_latency * 0.7 + latency * 0.3
                else:
                    self._avg_latency = latency

    def priority_key(self) -> tuple:
        """Sort key for the fan-out: healthy and fast first.

        A search gets cut short by its wall-clock budget whenever the network
        is slower than that budget.  When that happens the sources still queued
        should be the ones that never had anything to give -- a source with a
        long empty streak and a slow average goes last, so a healthy index is
        not dropped just because it sat behind six unreachable ones.
        Measured (windows then ubuntu): the second search lost mikan's 80 rows
        entirely, because it was queued behind sources that would have
        returned nothing anyway.

        Never-visited sources sort in the middle: ahead of the known-dead,
        behind the proven, so enabling a source still gets it a look.
        """
        with _HEALTH_LOCK:
            streak = self._empty_streak
            lat = self._avg_latency
            known = self._probes > 0
        if not known:
            tier = 1
        elif streak == 0:
            tier = 0
        else:
            tier = 2
        return (tier, streak, lat)

    # -- per-search state ----------------------------------------------
    @property
    def last_error(self) -> str:
        return getattr(self._local, "last_error", "")

    @last_error.setter
    def last_error(self, value: str) -> None:
        self._local.last_error = value

    @property
    def last_latency(self) -> float:
        return getattr(self._local, "last_latency", 0.0)

    @last_latency.setter
    def last_latency(self, value: float) -> None:
        self._local.last_latency = value

    @property
    def last_count(self) -> int:
        return getattr(self._local, "last_count", 0)

    @last_count.setter
    def last_count(self, value: int) -> None:
        self._local.last_count = value

    # -- API -----------------------------------------------------------
    @abc.abstractmethod
    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        """Return hits for ``query``.  Must never raise for network errors."""

    # -- helpers -------------------------------------------------------
    def _finish(self, results: List[SearchResult], started: float
                ) -> List[SearchResult]:
        self.last_latency = time.monotonic() - started
        self.last_count = len(results)
        for r in results:
            if not r.source:
                r.source = self.id
        return results

    def run(self, query: str, limit: int = 60) -> List[SearchResult]:
        started = time.monotonic()
        try:
            out = list(self.search(query, limit))
            self.last_error = ""
            # "answered with nothing" counts as unproductive for fan-out
            # ordering: a source that keeps returning 0 rows is holding a slot
            # that a working index could have used.  It is never disabled, just
            # made to queue behind the sources that actually deliver.
            self._record(bool(out), time.monotonic() - started)
            return self._finish(out, started)
        except Exception as exc:  # noqa: BLE001 - a plugin must not kill the search
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            log.debug("source %s failed: %s", self.id, self.last_error)
            self.last_latency = time.monotonic() - started
            self.last_count = 0
            self._record(False, time.monotonic() - started)
            return []

    def __repr__(self) -> str:  # pragma: no cover
        return "<Source %s enabled=%s>" % (self.id, self.enabled)


class Registry:
    """Holds one instance per source class, in a stable order."""

    def __init__(self) -> None:
        self._classes: List[Type[Source]] = []
        self._instances: Dict[str, Source] = {}
        self._lock = threading.RLock()

    def register(self, cls: Type[Source]) -> Type[Source]:
        with self._lock:
            if cls not in self._classes:
                self._classes.append(cls)
        return cls

    def add_instance(self, inst: Source) -> None:
        """Register a pre-built source (used for data-driven scrapers)."""
        with self._lock:
            self._instances[inst.id] = inst
            if inst.id not in [c.id for c in self._classes]:
                self._extra_ids = getattr(self, "_extra_ids", [])
                if inst.id not in self._extra_ids:
                    self._extra_ids.append(inst.id)

    def all(self) -> List[Source]:
        with self._lock:
            out = []
            for cls in self._classes:
                inst = self._instances.get(cls.id)
                if inst is None:
                    try:
                        inst = cls()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("cannot instantiate source %s: %s", cls.id, exc)
                        continue
                    self._instances[cls.id] = inst
                out.append(inst)
            for sid in getattr(self, "_extra_ids", []):
                inst = self._instances.get(sid)
                if inst is not None and inst not in out:
                    out.append(inst)
            return out

    def get(self, sid: str) -> Optional[Source]:
        for s in self.all():
            if s.id == sid:
                return s
        return None

    def enabled(self) -> List[Source]:
        return [s for s in self.all() if s.enabled]

    def apply_config(self, cfg: dict) -> None:
        """``cfg`` maps source id -> bool (enabled)."""
        for s in self.all():
            if s.id in cfg:
                s.enabled = bool(cfg[s.id])

    def snapshot(self):
        return {s.id: s.enabled for s in self.all()}


REGISTRY = Registry()


def register(cls: Type[Source]) -> Type[Source]:
    return REGISTRY.register(cls)
