"""eD2k **server** ("hub") source -- the search path that still works today.

The Kad network (`kad_source.py`) needs a reachable UDP node and is filtered
from datacentre addresses; the classic eDonkey2000 *server* protocol is plain
TCP, tolerates NAT (you just get a LowID) and currently has ~100k users spread
over a handful of live servers.

This is also what the local ``TSearch.exe`` did: its bundled ``kad.dll`` is a
stripped eMule kernel containing ``emule_hub_queryer`` and
``emule_server_listener``, and ``TSearch.exe`` ships the hostnames of its own
hubs.  Those hubs are long dead, but the public servers are not.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, List

from ..ed2k_server import POOL, Ed2kHit
from ..models import KIND_ED2K, SearchResult
from .base import Source, register

log = logging.getLogger("tsearch.ed2k.source")


@register
class Ed2kServerSource(Source):
    """Keyword search against the live eD2k server network."""

    id = "ed2k_server"
    label = "eD2k 服务器 (ed2k hub)"
    kinds = (KIND_ED2K,)
    weight = 30
    default_enabled = True
    #: The server probe happens once per run (cold start) and is capped
    #: separately; a *search* against the two or three servers that accepted us
    #: has no business holding the index phase for 30s.
    _base_timeout = 20.0

    def __init__(self) -> None:
        super().__init__()
        self.progress = ""
        # warm the server shortlist in the background so the first search does
        # not have to pay for the probe (which takes ~20s)
        self._warming = threading.Thread(target=self._warm, name="ed2k-warm",
                                         daemon=True)
        self._warming.start()

    @staticmethod
    def _warm() -> None:
        try:
            if not POOL.good_servers:
                POOL.refresh()
        except Exception:  # noqa: BLE001
            pass

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        q = (query or "").strip()
        if not q:
            return []

        if not POOL.good_servers:
            self.progress = "正在探测可用服务器…"
            # this blocks (and joins the start-up warm-up probe if it is still
            # running) so the first search of a session is not empty
            n = POOL.refresh(budget=min(45.0, self.request_timeout + 15.0))
            if n == 0:
                self.progress = POOL.last_error or "没有服务器接受登录"
                return []
        self.progress = "搜索中 (%d 台服务器)" % len(POOL.good_servers)

        hits: List[Ed2kHit] = POOL.search(
            q, max_results=max(120, limit * 4), servers=3,
            budget=self.request_timeout)
        self.progress = "完成"

        out: List[SearchResult] = []
        for h in hits:
            out.append(SearchResult(
                name=h.name,
                link=h.ed2k,
                seeds=h.sources,
                source=self.id,
                kind=KIND_ED2K,
                size=h.size,
                ed2k_hash=h.file_hash,
                seeds_verified=True,
                extra={"complete_sources": h.complete_sources,
                       "filetype": h.filetype},
            ))
            if len(out) >= limit:
                break
        self.progress = "%d 条" % len(out)
        return out

    def status_line(self) -> str:
        n = len(POOL.good_servers)
        return self.progress or ("eD2k 服务器 %d 台可用" % n if n else "eD2k 未探测")
