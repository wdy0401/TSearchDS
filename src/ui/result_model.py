# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Table model + sorting proxy for the results tables.

Kept in its own module so both the window and the per-search tab can import it
without a cycle (the tab needs the model, the window needs the tab).
"""
from __future__ import annotations

from typing import Dict, List

from PyQt5 import QtCore, QtGui

from ..core.links import link_to_ed2k, link_to_magnet, link_to_thunder
from ..core.models import SearchResult

APP_TITLE = "TSearch-DS · 磁力 / 电驴 / 迅雷 搜索   from ds"
LINK_FORMATS = [
    ("原始链接", "auto"),
    ("磁力 (magnet)", "magnet"),
    ("电驴 (ed2k)", "ed2k"),
    ("迅雷 (thunder://)", "thunder"),
]

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


def _esc(text: str) -> str:
    import html
    return html.escape(str(text or ""))


# ---------------------------------------------------------------------------
# table model
# ---------------------------------------------------------------------------

class ResultModel(QtCore.QAbstractTableModel):
    #: 默认列顺序；表头可拖动换序、可调宽度，布局会记住
    HEADERS = ["名称", "资源数", "文件大小", "文件类型", "链接", "来源"]

    #: logical column indexes (independent of the on-screen order)
    COL_NAME, COL_SEEDS, COL_SIZE, COL_TYPE, COL_LINK, COL_SOURCE = range(6)

    #: custom role carrying the SearchResult
    RESULT_ROLE = QtCore.Qt.UserRole + 1
    #: custom role giving the numeric sort key of a cell
    SORT_ROLE = QtCore.Qt.UserRole + 2

    def __init__(self, fmt: str = "auto") -> None:
        super().__init__()
        self.rows: List[SearchResult] = []
        self._index: Dict[str, int] = {}
        #: id(result) -> row, so an in-place 资源数 update never duplicates a
        #: row even when enrichment fills in an infohash (which changes the
        #: result's dedup key from name-based to hash-based)
        self._ids: Dict[int, int] = {}
        self.fmt = fmt

    # -- Qt API --------------------------------------------------------
    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if orientation == QtCore.Qt.Horizontal and role == QtCore.Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()

        if role == QtCore.Qt.DisplayRole:
            if col == self.COL_NAME:
                return row.name
            if col == self.COL_LINK:
                return self.display_link(row)
            if col == self.COL_SEEDS:
                return row.seeds_display
            if col == self.COL_SIZE:
                return row.size_display
            if col == self.COL_TYPE:
                return row.type_display
            if col == self.COL_SOURCE:
                return self.source_display(row)

        elif role == self.SORT_ROLE:
            if col == self.COL_SEEDS:
                return row.seeds
            if col == self.COL_SIZE:
                return row.size_bytes
            if col == self.COL_TYPE:
                return row.type_sort_key
            return None

        elif role == QtCore.Qt.ToolTipRole:
            return self.tooltip(row)

        elif role == QtCore.Qt.TextAlignmentRole:
            if col in (self.COL_SEEDS, self.COL_SIZE):
                return int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            if col == self.COL_TYPE:
                return int(QtCore.Qt.AlignCenter)

        elif role == self.RESULT_ROLE:
            return row

        elif role == QtCore.Qt.ForegroundRole:
            if col == self.COL_SEEDS:
                if row.seeds >= 50:
                    return QtGui.QBrush(QtGui.QColor("#1a7f37"))
                if row.seeds > 0:
                    return QtGui.QBrush(QtGui.QColor("#9a6700"))
                return QtGui.QBrush(QtGui.QColor("#8b949e"))
            if col == self.COL_SIZE:
                if row.size_bytes:
                    return QtGui.QBrush(QtGui.QColor("#24292f"))
                return QtGui.QBrush(QtGui.QColor("#8b949e"))
            if col == self.COL_TYPE:
                colour = _TYPE_COLOURS.get(row.type_display)
                if colour:
                    return QtGui.QBrush(QtGui.QColor(colour))
        return None

    # -- helpers -------------------------------------------------------
    @staticmethod
    def source_display(r: SearchResult) -> str:
        from ..core.sources import REGISTRY
        names = []
        for sid in (r.extra.get("srcs") or [r.source]):
            if not sid:
                continue
            source = REGISTRY.get(sid)
            label = source.label if source is not None else sid
            if label not in names:
                names.append(label)
        return "、".join(names) or "未知"

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

    def tooltip(self, r: SearchResult) -> str:
        lines = [
            "<b>%s</b>" % _esc(r.name),
            "来源: %s" % _esc(self.source_display(r)),
            "种子/来源数: %s" % r.seeds_display,
            "文件大小: %s" % r.size_display,
            "文件类型: %s" % r.type_display,
        ]
        if r.extra.get("size_from_name"):
            lines.append("<span style='color:#8b949e'>"
                         "（大小取自文件名，仅供参考）</span>")
        if r.peers:
            lines.append("下载中(leechers): %d" % r.peers)
        if r.infohash:
            lines.append("infohash: %s" % r.infohash)
        if r.ed2k_hash:
            lines.append("ed2k hash: %s" % r.ed2k_hash.upper())
        page = r.extra.get("page")
        if page:
            lines.append("页面: %s" % _esc(str(page))[:160])
        lines.append("<br><span style='color:#8b949e'>%s</span>"
                     % _esc(self.display_link(r))[:300])
        return "<br>".join(lines)

    # -- mutation ------------------------------------------------------
    def set_format(self, fmt: str) -> None:
        self.fmt = fmt
        if self.rows:
            self.dataChanged.emit(
                self.index(0, ResultModel.COL_LINK),
                self.index(len(self.rows) - 1, ResultModel.COL_LINK))

    def clear(self) -> None:
        self.beginResetModel()
        self.rows = []
        self._index = {}
        self._ids = {}
        self.endResetModel()

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
            start = len(self.rows)
            self.beginInsertRows(QtCore.QModelIndex(), start, start + len(fresh) - 1)
            for r in fresh:
                self._ids[id(r)] = len(self.rows)
                self._index[r.dedup_key] = len(self.rows)
                self.rows.append(r)
            self.endInsertRows()
        for r in updates:
            self.refresh_row(r)
        return len(fresh)

    def refresh_row(self, r: SearchResult) -> None:
        i = self._ids.get(id(r))
        if i is None:
            # fall back to the (possibly stale) dedup key
            i = self._index.get(r.dedup_key)
            if i is not None and (i >= len(self.rows) or self.rows[i] is not r):
                i = None
        if i is not None and i >= len(self.rows):
            i = None
        if i is None:
            i = next((j for j, row in enumerate(self.rows) if row is r), None)
        if i is None:
            return
        # keep both indices coherent after an in-place key change
        self._ids[id(r)] = i
        self._index[r.dedup_key] = i
        last = len(self.HEADERS) - 1
        self.dataChanged.emit(self.index(i, 0), self.index(i, last))

    def refresh_all(self) -> None:
        if self.rows:
            last = len(self.HEADERS) - 1
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.rows) - 1, last))

    def results_at(self, indexes) -> List[SearchResult]:
        seen = set()
        out = []
        for idx in indexes:
            row = idx.row()
            if 0 <= row < len(self.rows) and row not in seen:
                seen.add(row)
                out.append(self.rows[row])
        return out


# ---------------------------------------------------------------------------
# sorting proxy
# ---------------------------------------------------------------------------

class ResultSorter(QtCore.QSortFilterProxyModel):
    """Sorts every column sensibly.

    资源数 / 文件大小 sort numerically and 文件类型 sorts in category order, all
    via the model's ``SORT_ROLE``; everything else falls back to a
    case-insensitive text compare.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setSortCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.setDynamicSortFilter(True)
        self._filter = ""
        self.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self.setFilterKeyColumn(ResultModel.COL_NAME)

    def set_keyword_filter(self, text: str) -> None:
        self._filter = (text or "").strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:
        if not self._filter:
            return True
        model = self.sourceModel()
        if model is None or source_row >= len(model.rows):
            return True
        return self._filter in model.rows[source_row].name.lower()

    def lessThan(self, left, right) -> bool:
        model = self.sourceModel()
        key_a = model.data(left, ResultModel.SORT_ROLE)
        key_b = model.data(right, ResultModel.SORT_ROLE)
        if key_a is not None and key_b is not None:
            return key_a < key_b
        if left.column() == ResultModel.COL_NAME:
            a = model.rows[left.row()].name.lower()
            b = model.rows[right.row()].name.lower()
            return a < b
        a = str(model.data(left, QtCore.Qt.DisplayRole) or "").lower()
        b = str(model.data(right, QtCore.Qt.DisplayRole) or "").lower()
        return a < b
