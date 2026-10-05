# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""eD2k / Kad network source -- a headless replacement for TSearch.exe.

Uses :mod:`src.core.kad` to talk to the live Kademlia network.  Keyword
searching on Kad is inherently slow (a DHT walk plus a result-collection
window), so this source runs with a much larger timeout than the HTTP ones
and reports partial hits as they arrive.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Iterable, List, Optional

from ..http import HTTP
from ..kad import KadHit, KadSearcher, keyword_words
from ..links import build_ed2k
from ..models import KIND_ED2K, SearchResult
from .base import Source, register

log = logging.getLogger("tsearch.kad.source")

#: Publicly maintained Kad contact lists.  The first one that answers is used.
NODES_DAT_URLS = (
    "http://upd.emule-security.org/nodes.dat",
    "https://upd.emule-security.org/nodes.dat",
    "http://peerates.net/nodes.dat",
)

_SHARED_LOCK = threading.Lock()
_SHARED: dict = {"searcher": None, "nodes_path": "", "ready": False}


def nodes_dat_path() -> str:
    from ..proxy.core import data_dir
    return os.path.join(data_dir(), "nodes.dat")


def refresh_nodes_dat(timeout: float = 15.0) -> str:
    """Download a live Kad bootstrap list.  Returns the local path ('' on fail)."""
    dest = nodes_dat_path()
    for url in NODES_DAT_URLS:
        data = HTTP.get_bytes(url, timeout=timeout, max_bytes=2 << 20)
        if data and len(data) > 1024:
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, dest)
                return dest
            except OSError:
                continue
    return dest if os.path.isfile(dest) else ""


def _bundled_nodes() -> List[str]:
    """Candidate nodes.dat files already on disk (including TSearch's own)."""
    out = []
    p = nodes_dat_path()
    if os.path.isfile(p):
        out.append(p)
    try:
        from ..proxy.core import app_dir
        out.append(os.path.join(app_dir(), "nodes.dat"))
        out.append(os.path.join(app_dir(), "resources", "nodes.dat"))
    except Exception:
        pass
    for probe in (r"C:\Users\wdy04\Desktop\desktop\TSearch\nodes.dat",):
        if os.path.isfile(probe):
            out.append(probe)
    return [p for p in out if p and os.path.isfile(p)]


def shared_searcher(autostart: bool = True) -> KadSearcher:
    """Process-wide Kad node so we do not re-bootstrap on every search."""
    with _SHARED_LOCK:
        s: Optional[KadSearcher] = _SHARED.get("searcher")
        if s is None:
            path = nodes_dat_path()
            if not os.path.isfile(path):
                path = refresh_nodes_dat() or ""
            s = KadSearcher(nodes_dat=path or None)
            _SHARED["searcher"] = s
        return s


def _matches(name: str, words: List[str]) -> bool:
    """Fuzzy keyword gate: every search word must appear somewhere."""
    low = name.lower()
    return all(w in low for w in words)


@register
class KadSource(Source):
    """Live eD2k / Kad keyword search."""

    id = "kad"
    label = "eD2k Kad (电驴网络)"
    kinds = (KIND_ED2K,)
    weight = 60
    default_enabled = True
    #: Kad search windows are measured in seconds, not milliseconds
    _base_timeout = 22.0

    def __init__(self) -> None:
        super().__init__()
        self.progress = ""

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        words = keyword_words(query)
        if not words:
            return []
        searcher = shared_searcher()
        if not searcher.ready:
            self.progress = "正在连接 Kad 网络…"
            searcher.start(timeout=18.0)
        if not searcher.ready:
            # try a fresher contact list once
            self.progress = "节点列表过期，正在更新…"
            path = refresh_nodes_dat()
            if path and _SHARED.get("searcher") is not None:
                _SHARED["searcher"].stop()
                _SHARED["searcher"] = KadSearcher(nodes_dat=path)
                searcher = _SHARED["searcher"]
                searcher.start(timeout=18.0)
        if not searcher.ready:
            self.progress = "Kad bootstrap 失败（网络可能屏蔽了 UDP）"
            return []

        self.progress = "Kad 搜索中…"
        hits = searcher.search(query, duration=self.request_timeout)

        out: List[SearchResult] = []
        seen = set()
        for h in hits:
            if h.file_hash in seen:
                continue
            if words and not _matches(h.name, words):
                continue
            seen.add(h.file_hash)
            link = build_ed2k(h.name, h.size, h.file_hash.hex())
            out.append(SearchResult(
                name=h.name,
                link=link,
                seeds=h.sources,
                source=self.id,
                kind=KIND_ED2K,
                size=h.size,
                ed2k_hash=h.file_hash.hex(),
                seeds_verified=h.sources > 0,
                extra={"filetype": h.filetype, "format": h.fileformat,
                       "publishers": h.publishers},
            ))
            if len(out) >= limit:
                break
        out.sort(key=lambda r: (-r.seeds, r.name.lower()))
        return out

    def status_line(self) -> str:
        try:
            s = _SHARED.get("searcher")
            n = s.contact_count if s else 0
        except Exception:
            n = 0
        return self.progress or ("Kad 节点 %d" % n if n else "Kad 未连接")
