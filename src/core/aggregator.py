# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Search orchestration: fan out to every enabled source and stream results."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

from .enrich import enrich
from .gateway import scope
from .models import KIND_ED2K, KIND_MAGNET, SearchResult
from .parallel import pmap
from .sources.base import REGISTRY, Source

log = logging.getLogger("tsearch.aggregator")


def query_tokens(query: str) -> List[str]:
    """Split a query into lower-cased tokens used for relevance scoring."""
    out = []
    for tok in (query or "").replace("\u3000", " ").split():
        tok = tok.strip().lower()
        if len(tok) >= 2 or (tok and not tok.isascii()):
            out.append(tok)
    if not out and (query or "").strip():
        out = [query.strip().lower()]
    return out


def relevance(name: str, tokens: Sequence[str]) -> float:
    """Fraction of the query tokens that literally occur in ``name``.

    Indexes are literal-match engines: apibay in particular answers a Chinese
    keyword with whatever is popular rather than with nothing, so a source's
    results have to be scored before they reach the table.
    """
    if not tokens:
        return 1.0
    low = (name or "").lower()
    if not low:
        return 0.0
    hits = 0
    for t in tokens:
        if t in low:
            hits += 1
        elif not t.isascii():
            # CJK is written without spaces, so accept a character-level
            # overlap for at least half of the token
            chars = [c for c in t if not c.isspace()]
            if chars and sum(1 for c in chars if c in low) >= max(1, len(chars) // 2):
                hits += 0.5
    return hits / len(tokens)


class SearchSession:
    """One keyword search across every enabled source.

    Results are deduplicated by info-hash / eD2k hash / normalised name so the
    table never shows the same swarm twice, even though eight indexes return it.
    """

    def __init__(self, query: str,
                 sources: Optional[Sequence[Source]] = None,
                 on_result: Optional[Callable[[List[SearchResult]], None]] = None,
                 on_status: Optional[Callable[[str, str], None]] = None,
                 on_done: Optional[Callable[[List[SearchResult]], None]] = None,
                 enrich_seeders: bool = True,
                 per_source_limit: int = 80,
                 max_results: int = 2000,
                 max_enrich: int = 100,
                 enrich_workers: int = 14,
                 index_budget: float = 40.0,
                 enrich_budget: float = 20.0) -> None:
        self.query = (query or "").strip()
        self.sources = list(sources if sources is not None else REGISTRY.enabled())
        self.on_result = on_result or (lambda rows: None)
        self.on_status = on_status or (lambda sid, msg: None)
        self.on_done = on_done or (lambda rows: None)
        self.per_source_limit = per_source_limit
        self.max_results = max_results
        self.max_enrich = max_enrich
        self.enrich_workers = enrich_workers
        self.enrich_seeders = enrich_seeders
        #: hard wall-clock caps: the index phase and the 资源数 pass may not run
        #: longer than this, no matter how many sources hang
        self.index_budget = float(index_budget)
        self.enrich_budget = float(enrich_budget)
        self.timed_out = False

        self.results: List[SearchResult] = []
        self._seen: Dict[str, SearchResult] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending = 0
        self._tokens = query_tokens(self.query)
        self._dropped = 0
        self.started_at = 0.0
        self.finished_at = 0.0
        self.errors: Dict[str, str] = {}
        self.counts: Dict[str, int] = {}
        self.running = False

    # -- control -------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.started_at = time.monotonic()
        self._pending = len(self.sources)
        self._thread = threading.Thread(target=self._run, name="search",
                                        daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` was called (the thread may still wind down)."""
        return self._stop.is_set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- internals -----------------------------------------------------
    def _add(self, items: Sequence[SearchResult]) -> List[SearchResult]:
        fresh: List[SearchResult] = []
        with self._lock:
            for r in items:
                if self._stop.is_set():
                    break
                key = r.dedup_key
                prev = self._seen.get(key)
                if prev is not None:
                    changed = False
                    # keep the better of the two: more seeds, richer link
                    if r.seeds > prev.seeds:
                        changed = True
                        prev.seeds = r.seeds
                        prev.seeds_verified = prev.seeds_verified or r.seeds_verified
                    if prev.link.lower().startswith("http") and \
                            not r.link.lower().startswith("http"):
                        changed = True
                        prev.link = r.link
                    if r.source and r.source not in prev.extra.get("srcs", []):
                        prev.extra.setdefault("srcs", []).append(r.source)
                        changed = True
                    if changed and not any(item is prev for item in fresh):
                        fresh.append(prev)
                    continue
                r.extra.setdefault("srcs", [r.source] if r.source else [])
                self._seen[key] = r
                self.results.append(r)
                fresh.append(r)
                if len(self.results) >= self.max_results:
                    self._stop.set()
                    break
        return fresh

    def _run_source(self, src: Source) -> List[SearchResult]:
        if self._stop.is_set():
            return []
        self.on_status(src.id, "查询中…")
        t0 = time.monotonic()
        rows: List[SearchResult] = []
        # No slot is taken here: the concurrency limit lives down in http.py,
        # where the actual requests are.  Capping "sources in flight" never
        # bounded anything -- one 资源数 row fans out into a dozen tracker
        # scrapes, so the old cap of ten was really ~120 requests.
        rows = src.run(self.query, self.per_source_limit)
        dt = time.monotonic() - t0

        # drop rows that share nothing with the query: several indexes answer
        # an unmatched keyword with their current front page instead of "no
        # results", which otherwise floods the table with unrelated torrents
        kept: List[SearchResult] = []
        for r in rows:
            score = relevance(r.name, self._tokens)
            r.extra["relevance"] = round(score, 3)
            if score > 0:
                kept.append(r)
        dropped = len(rows) - len(kept)
        if dropped:
            self._dropped += dropped
            log.debug("source %s: dropped %d/%d irrelevant rows",
                      src.id, dropped, len(rows))
        self.on_status(src.id, "%d 条 / %.1fs%s"
                       % (len(kept), dt, (" (−%d 无关)" % dropped) if dropped else ""))
        return kept

    def _run(self) -> None:
        try:
            # Fan out healthiest-first.  The index phase has a wall-clock
            # budget, so when it runs out the sources left waiting are the
            # ones that have been returning nothing -- measured: the second
            # search lost a healthy source (mikan, 80 rows) purely because it
            # was queued behind six unreachable ones.  Sources with no history
            # yet sort as "unknown", ahead of known-dead ones but behind the
            # proven ones, so a newly enabled source still gets its chance.
            ordered = sorted(self.sources, key=lambda s: s.priority_key())
            self.sources = ordered
            workers = max(1, min(10, len(ordered)))

            def _collect(src: Source, rows, error: Optional[BaseException]) -> None:
                if error is not None:
                    self.errors[src.id] = str(error)
                rows = rows or []
                self.counts[src.id] = len(rows)
                if src.last_error:
                    self.errors[src.id] = src.last_error
                if rows:
                    fresh = self._add(rows)
                    if fresh:
                        self.on_result(fresh)

            # pmap invokes ``_collect`` under its lock, so the results model is
            # still mutated by one thread at a time.
            #
            # ``timeout`` is a hard cap on the whole index phase: results
            # stream in as sources answer, so waiting another 30s for one dead
            # index buys the user nothing.  Stragglers are reported as
            # "no answer" instead of holding the search open.
            # The index phase is what the user is waiting for: mark it
            # high-priority and hand the phase deadline down to the network
            # layer, so a request nobody is waiting for any more is never sent
            # instead of being sent and then discarded.
            with scope(high=True,
                       deadline=time.monotonic() + self.index_budget,
                       stop=self._stop, owner=self):
                pmap(self._run_source, ordered, workers=workers,
                     timeout=self.index_budget, cancel=self._stop,
                     on_result=_collect)
            self.timed_out = (time.monotonic() - self.started_at
                              > self.index_budget * 0.98)

            # The index phase is what the user is waiting for: announce
            # completion now so the UI becomes interactive, then keep working
            # on 资源数 in the background and stream the updates in.
            self.finished_at = time.monotonic()
            with self._lock:
                snapshot = list(self.results)
            self.on_done(snapshot)

            if not self.enrich_seeders or self._stop.is_set():
                return
            todo = [r for r in snapshot if not r.seeds and r.kind == KIND_MAGNET]
            if not todo:
                return
            todo = todo[:self.max_enrich]
            self.on_status("enrich", "补充资源数 (%d)…" % len(todo))

            def _done(r: SearchResult) -> None:
                self.on_result([r])

            # 资源数 is a nicety, not the answer: it gets a wall-clock budget
            # too, so a swarm whose trackers never answer cannot keep the tab
            # looking busy for minutes.
            enrich(todo, workers=self.enrich_workers, use_dht=False,
                   on_done=_done, stop_event=self._stop, owner=self,
                   deadline=time.monotonic() + self.enrich_budget)
            self.on_status("enrich", "资源数补充完成")
        finally:
            self.running = False
            if not self.finished_at:
                self.finished_at = time.monotonic()
            with self._lock:
                snapshot = list(self.results)
            # a terminal pass so the UI can settle any pending sort
            self.on_done(snapshot)

    # -- info ----------------------------------------------------------
    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.monotonic()
        return max(0.0, end - (self.started_at or end))

    def summary(self) -> str:
        ok = sum(1 for s in self.sources if self.counts.get(s.id))
        text = ("%d 条结果 · %d/%d 个源有响应 · %.1fs"
                % (len(self.results), ok, len(self.sources), self.elapsed))
        if self.timed_out:
            text += "（%.0fs 上限已到，慢的源按无结果计）" % self.index_budget
        return text
