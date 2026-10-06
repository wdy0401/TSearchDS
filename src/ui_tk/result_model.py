# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Result store, sorting and filtering for the Tk backend.

Tk has no model/view split -- ``ttk.Treeview`` owns its rows -- so the Qt
``QAbstractTableModel`` / ``QSortFilterProxyModel`` pair collapses into one
plain-Python store that the tab renders into the Treeview:

* :attr:`rows`   every result that arrived, in arrival order (the truth)
* :attr:`order`  what the user currently sees (after filter + sort)

The dedup/merge rules are identical to the Qt model so both backends show the
same rows for the same input.
"""
from __future__ import annotations

from ..ui.i18n import tr
from ..ui.constants import APP_TITLE, LINK_FORMATS  # noqa: F401

from typing import Dict, List, Optional

from ..core.links import link_to_ed2k, link_to_magnet, link_to_thunder
from ..core.models import SearchResult

#: subtle colour coding for the 文件类型 column
_TYPE_COLOURS = {
    "视频": "#1a5fb4",
    "音频": "#813d9c",
    "图片": "#a35200",
    "文档": "#1a7f37",
    "程序": "#b3261e",
    "压缩包": "#7a5901",
    "光盘镜像": "#0f6674",
}

_SEED_COLOURS = ((50, "#1a7f37"), (1, "#9a6700"))


class ResultModel:
    """Holds the results of one search and the view order derived from them."""

    #: default column order
    HEADERS = ["名称", "资源数", "文件大小", "文件类型", "链接", "来源"]

    COL_NAME, COL_SEEDS, COL_SIZE, COL_TYPE, COL_LINK, COL_SOURCE = range(6)

    #: numeric columns -- everything else sorts case-insensitively as text
    NUMERIC_COLUMNS = (COL_SEEDS, COL_SIZE, COL_TYPE)

    def __init__(self, fmt: str = "auto", on_change=None) -> None:
        self.rows: List[SearchResult] = []
        self.order: List[SearchResult] = []
        self._index: Dict[str, int] = {}
        #: id(result) -> row, so an in-place 资源数 update never duplicates a
        #: row even when enrichment fills in an infohash (which changes the
        #: result's dedup key from name-based to hash-based)
        self._ids: Dict[int, int] = {}
        self.fmt = fmt
        self._filter = ""
        self._sort_col = self.COL_SEEDS
        self._sort_desc = True
        #: called (on the GUI thread) whenever the visible order changed
        self.on_change = on_change

    # -- view state ----------------------------------------------------
    @property
    def sort_col(self) -> int:
        return self._sort_col

    @property
    def sort_desc(self) -> bool:
        return self._sort_desc

    def set_filter(self, text: str) -> None:
        self._filter = (text or "").strip().lower()
        self.rebuild()

    def set_sort(self, col: int, desc: Optional[bool] = None) -> None:
        if desc is None:
            # first click on a column sorts descending for numbers, ascending
            # for text; clicking the active column flips it
            if col == self._sort_col:
                desc = not self._sort_desc
            else:
                desc = col in self.NUMERIC_COLUMNS
        self._sort_col = col
        self._sort_desc = bool(desc)
        self.rebuild()

    def sort_key(self, r: SearchResult, col: int):
        if col == self.COL_SEEDS:
            return r.seeds
        if col == self.COL_SIZE:
            return r.size_bytes
        if col == self.COL_TYPE:
            return r.type_sort_key
        if col == self.COL_NAME:
            return r.name.lower()
        if col == self.COL_LINK:
            return self.display_link(r).lower()
        if col == self.COL_SOURCE:
            return self.source_display(r).lower()
        return ""

    def rebuild(self) -> None:
        """Recompute :attr:`order` from rows + filter + sort."""
        if self._filter:
            rows = [r for r in self.rows if self._filter in r.name.lower()]
        else:
            rows = list(self.rows)
        col = self._sort_col
        try:
            rows.sort(key=lambda r: self.sort_key(r, col),
                      reverse=self._sort_desc)
        except TypeError:  # mixed key types from a hostile source
            pass
        self.order = rows
        if self.on_change is not None:
            self.on_change()

    # -- display helpers -----------------------------------------------
    @staticmethod
    def source_display(r: SearchResult) -> str:
        from ..core.sources import REGISTRY
        names = []
        for sid in (r.extra.get("srcs") or [r.source]):
            if not sid:
                continue
            source = REGISTRY.get(sid)
            label = tr(source.label) if source is not None else sid
            if label not in names:
                names.append(label)
        from ..ui.i18n import language
        return (', ' if language() == 'en' else '、').join(names) or tr("未知")

    def display_link(self, r: SearchResult) -> str:
        if self.fmt == "auto":
            return r.link
        try:
            if self.fmt == "magnet":
                return link_to_magnet(r.link, r.name, r.size) or r.link
            if self.fmt == "ed2k":
                return link_to_ed2k(r.link, r.name, r.size) or r.link
            if self.fmt == "thunder":
                return link_to_thunder(r.link) or r.link
        except Exception:
            return r.link
        return r.link

    def cell(self, r: SearchResult, col: int) -> str:
        if col == self.COL_NAME:
            return r.name
        if col == self.COL_LINK:
            return self.display_link(r)
        if col == self.COL_SEEDS:
            return r.seeds_display
        if col == self.COL_SIZE:
            return r.size_display
        if col == self.COL_TYPE:
            return tr(r.type_display)
        if col == self.COL_SOURCE:
            return self.source_display(r)
        return ""

    #: Treeview tag for a row's 文件类型 (Tk tag names stay ASCII-safe)
    @staticmethod
    def type_tag(r: SearchResult) -> str:
        return "ty_%s" % abs(hash(r.type_display))

    def type_colour(self, r: SearchResult) -> str:
        return _TYPE_COLOURS.get(r.type_display, "")

    def seed_colour(self, r: SearchResult) -> str:
        for threshold, colour in _SEED_COLOURS:
            if r.seeds >= threshold:
                return colour
        return "#8b949e"

    def tooltip(self, r: SearchResult) -> str:
        """Plain text -- Tk has no rich-text tooltips (Qt used HTML here)."""
        lines = [
            r.name,
            tr("来源: %s") % self.source_display(r),
            tr("种子/来源数: %s") % r.seeds_display,
            tr("文件大小: %s") % r.size_display,
            tr("文件类型: %s") % tr(r.type_display),
        ]
        if r.extra.get("size_from_name"):
            lines.append(tr("（大小取自文件名，仅供参考）"))
        if r.peers:
            lines.append(tr("下载中(leechers): %d") % r.peers)
        if r.infohash:
            lines.append("infohash: %s" % r.infohash)
        if r.ed2k_hash:
            lines.append("ed2k hash: %s" % r.ed2k_hash.upper())
        page = r.extra.get("page")
        if page:
            lines.append(tr("页面: %s") % str(page)[:160])
        lines.append(self.display_link(r)[:300])
        return "\n".join(lines)

    # -- mutation ------------------------------------------------------
    def set_format(self, fmt: str) -> None:
        self.fmt = fmt
        self._notify()

    def clear(self) -> None:
        self.rows = []
        self._index = {}
        self._ids = {}
        self.rebuild()

    def add(self, results: List[SearchResult]) -> int:
        """Insert new results; existing objects are treated as updates."""
        fresh: List[SearchResult] = []
        updates: List[SearchResult] = []
        for r in results:
            i = self._ids.get(id(r))
            if i is not None:
                updates.append(r)
                continue
            prev = self._index.get(r.dedup_key)
            if prev is not None and prev < len(self.rows) and self.rows[prev] is not r:
                # a *different* result object claiming the same swarm: merge
                keep = self.rows[prev]
                if r.seeds > keep.seeds:
                    keep.seeds = r.seeds
                    keep.seeds_verified = keep.seeds_verified or r.seeds_verified
                keep.extra.setdefault("srcs", [keep.source] if keep.source else [])
                for s in (r.extra.get("srcs") or [r.source]):
                    if s and s not in keep.extra.setdefault("srcs", []):
                        keep.extra["srcs"].append(s)
                updates.append(keep)
                continue
            fresh.append(r)

        if fresh:
            for r in fresh:
                self._ids[id(r)] = len(self.rows)
                self._index[r.dedup_key] = len(self.rows)
                self.rows.append(r)
        for r in updates:
            self.refresh_row(r)
        self.rebuild()
        return len(fresh)

    def refresh_row(self, r: SearchResult) -> None:
        i = self._ids.get(id(r))
        if i is None:
            i = self._index.get(r.dedup_key)
            if i is not None and (i >= len(self.rows) or self.rows[i] is not r):
                i = None
        if i is not None and i >= len(self.rows):
            i = None
        if i is None:
            i = next((j for j, row in enumerate(self.rows) if row is r), None)
        if i is None:
            return
        self._ids[id(r)] = i
        self._index[r.dedup_key] = i

    def refresh_all(self) -> None:
        self.rebuild()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # -- lookups -------------------------------------------------------
    def result_at(self, position: int) -> Optional[SearchResult]:
        if 0 <= position < len(self.order):
            return self.order[position]
        return None

    def results(self) -> List[SearchResult]:
        """Rows in the order the user currently sees them."""
        return list(self.order)

    def columnCount(self) -> int:  # noqa: N802 - Qt spelling kept for parity
        return len(self.HEADERS)

    def __len__(self) -> int:
        return len(self.rows)
