"""Core data models for TSearch-DS."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .filetypes import (TYPE_UNKNOWN, classify, human_size, size_from_name,
                        type_rank)


# Link kinds
KIND_MAGNET = "magnet"
KIND_ED2K = "ed2k"
KIND_THUNDER = "thunder"
KIND_HTTP = "http"
KIND_TORRENT = "torrent"
KIND_UNKNOWN = "unknown"


def _norm_name(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[\s\.\-_\[\]\(\)【】（）]+", "", s)
    return s


@dataclass
class SearchResult:
    """One search hit.  Shown as:

    名称 / 链接 / 资源数 / 文件大小 / 文件类型
    """

    name: str
    link: str
    seeds: int = 0
    source: str = ""
    kind: str = KIND_UNKNOWN
    size: int = 0
    peers: int = 0
    infohash: str = ""
    ed2k_hash: str = ""
    #: display category (视频/音频/...); filled lazily from
    #: ``extra["filetype"]`` (eD2k FT_FILETYPE) or the filename extension
    filetype: str = ""
    #: extra bookkeeping (page url, torrent url, raw tags, ...)
    extra: Dict[str, Any] = field(default_factory=dict)
    #: monotonic timestamp of when the hit was produced
    found_at: float = field(default_factory=time.monotonic)
    #: true once a background pass has confirmed seeds to be accurate
    seeds_verified: bool = False

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        # Recover a size from the filename *now* rather than lazily: the size
        # feeds dedup_key, and a key that changes after the row was inserted
        # would duplicate rows.  Sources that report a real size overwrite it.
        if self.size <= 0:
            guess = size_from_name(self.name)
            if guess:
                self.size = guess
                self.extra["size_from_name"] = True
        # resolve the display category up front too, using the eD2k
        # FT_FILETYPE hint when a source supplied one
        if not self.filetype:
            self.filetype = classify(self.name, self.extra.get("filetype"))

    @property
    def dedup_key(self) -> str:
        """Stable identity for de-duplication across sources.

        Deliberately does NOT include the size: enrichment can fill a size in
        after the fact, and a key that mutates would let the same swarm in
        twice.
        """
        if self.infohash:
            return "bt:" + self.infohash.lower()
        if self.ed2k_hash:
            return "ed2k:" + self.ed2k_hash.lower()
        return "nm:" + _norm_name(self.name)

    @property
    def seeds_display(self) -> str:
        if self.seeds > 0:
            return str(self.seeds)
        if self.seeds_verified:
            return "0"
        return "-"

    # -- size ----------------------------------------------------------
    @property
    def size_bytes(self) -> int:
        return self.size if self.size > 0 else 0

    def set_size(self, size: int) -> None:
        """Record an authoritative size (from a tracker / .torrent / server)."""
        try:
            size = int(size)
        except (TypeError, ValueError):
            return
        if size > 0:
            self.size = size
            # a real size supersedes a guess taken from the filename
            self.extra.pop("size_from_name", None)

    @property
    def size_display(self) -> str:
        n = self.size_bytes
        if not n:
            return "-"
        return human_size(n) + ("?" if self.extra.get("size_from_name") else "")

    # -- type ----------------------------------------------------------
    @property
    def type_display(self) -> str:
        if not self.filetype:
            self.filetype = classify(self.name, self.extra.get("filetype"))
        return self.filetype or TYPE_UNKNOWN

    @property
    def type_sort_key(self) -> int:
        return type_rank(self.type_display)

    def to_row(self):
        return [self.name, self.link, self.seeds_display,
                self.size_display, self.type_display, self.source]
