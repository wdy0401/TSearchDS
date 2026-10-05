"""One search per tab.

Everything a single query needs lives here: its own result model, its own
table (with its own column layout), its own filter box, its own detail pane and
its own :class:`SearchSession`.  That is what allows a second search to start
while the first one is still waiting for the slower indexes -- the window keeps
one of these per tab and they never share mutable state.

Worker threads never touch a widget directly: they emit the signals declared
here, which Qt queues onto the GUI thread.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from ..core.aggregator import SearchSession
from ..core.links import link_to_ed2k, link_to_magnet, link_to_thunder, safe_page_url
from ..core.models import SearchResult
from .result_model import LINK_FORMATS, ResultModel, ResultSorter

log = logging.getLogger("tsearch.ui.tab")


class SearchTab(QtWidgets.QWidget):
    """Results of exactly one keyword search."""

    sig_results = QtCore.pyqtSignal(list)
    sig_status = QtCore.pyqtSignal(str, str)
    sig_done = QtCore.pyqtSignal(str)
    #: tab label (title text)
    sig_title = QtCore.pyqtSignal(str)
    #: message for the shared status bar
    sig_message = QtCore.pyqtSignal(str)

    def __init__(self, owner, query: str = "") -> None:
        super().__init__(owner)
        self.owner = owner
        self.query = (query or "").strip()
        self.session: Optional[SearchSession] = None
        self.started_at = 0.0
        #: column widths are allocated automatically until the user drags one
        self._auto_fit = True
        self._fitting = False
        #: rows waiting to be applied (see :meth:`_on_results`)
        self._pending: List[SearchResult] = []
        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(120)
        self._flush_timer.timeout.connect(self._flush_results)
        #: "index" while the sources are still answering, then "enrich"
        self._phase = ""

        self.model = ResultModel(owner.settings.get("link_format", "auto"))
        self.proxy_model = ResultSorter()
        self.proxy_model.setSourceModel(self.model)

        self._build()
        self.sig_results.connect(self._on_results)
        self.sig_status.connect(self._on_status)
        self.sig_done.connect(self._on_done)
        self.sig_title.connect(self._apply_title)

    # -- construction --------------------------------------------------
    def _build(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(6)

        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("在本标签的结果里过滤…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(260)
        bar.addWidget(QtWidgets.QLabel("过滤:"))
        bar.addWidget(self.filter_edit)
        bar.addStretch(1)
        self.lbl_tab_status = QtWidgets.QLabel("")
        self.lbl_tab_status.setStyleSheet("color:#57606a")
        bar.addWidget(self.lbl_tab_status)
        root.addLayout(bar)

        self.table = QtWidgets.QTableView()
        self.table.setModel(self.proxy_model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        hh = self.table.horizontalHeader()
        # 每一列都可拖动换序、都可调宽度（包括最后一列）
        hh.setSectionsMovable(True)
        hh.setSectionsClickable(True)
        hh.setStretchLastSection(False)
        for _col in range(len(ResultModel.HEADERS)):
            hh.setSectionResizeMode(_col, QtWidgets.QHeaderView.Interactive)
        # default order: most sources first
        self.table.sortByColumn(ResultModel.COL_SEEDS, QtCore.Qt.DescendingOrder)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        root.addWidget(self.table, 1)

        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(96)
        self.details.setPlaceholderText("选中一行查看详情")
        root.addWidget(self.details)

        self.filter_edit.textChanged.connect(self.proxy_model.set_keyword_filter)
        hh.sectionResized.connect(self._on_column_resized)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: (self.owner._update_copy_enabled(), self.show_details()))
        self.table.doubleClicked.connect(self.show_details)

    # -- search lifecycle ----------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.session is not None and self.session.running)

    def start(self, query: str, sources) -> None:
        self.query = (query or "").strip()
        self.cancel()
        self._flush_timer.stop()
        self._pending = []
        self.model.clear()
        self.details.clear()
        self.filter_edit.clear()
        self.proxy_model.set_keyword_filter("")
        self.started_at = time.monotonic()
        self._phase = "index"
        self.sig_title.emit(self._label())
        self.lbl_tab_status.setText("搜索中… (%d 个数据源)" % len(sources))
        self.session = SearchSession(
            self.query, sources=sources,
            on_result=lambda rows: self.sig_results.emit(rows),
            on_status=lambda sid, msg: self.sig_status.emit(sid, msg),
            on_done=lambda rows: self.sig_done.emit(""),
            enrich_seeders=True)
        self.session.start()
        self.sig_message.emit("「%s」搜索中… (%d 个数据源)"
                              % (self.query, len(sources)))

    def cancel(self) -> None:
        if self.session is not None:
            try:
                self.session.cancel()
                # Cancelling is cooperative: the worker may be blocked on a
                # socket for another few seconds.  Drop its callbacks so it
                # cannot emit into a tab that has been closed (whose C++ side
                # is gone by then).
                self.session.on_result = lambda rows: None
                self.session.on_status = lambda sid, msg: None
                self.session.on_done = lambda rows: None
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self.cancel()
        self.lbl_tab_status.setText("已停止")
        self.sig_title.emit(self._label())
        self.sig_message.emit("已停止")

    # -- slots (GUI thread) -------------------------------------------
    @QtCore.pyqtSlot(list)
    def _on_results(self, rows: list) -> None:
        """Queue incoming rows; the table is updated in bursts.

        Enrichment delivers 资源数 one row at a time, and each insertion made
        the proxy model re-sort and the view repaint the whole table.  With two
        searches running that was enough work to make the *second* tab look
        stuck.  Coalescing a few dozen milliseconds of batches into one update
        keeps the table responsive without a visible delay.
        """
        self._pending.extend(rows)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_results(self) -> None:
        rows, self._pending = self._pending, []
        if not rows:
            return
        for r in rows:
            # never let a keyword-bearing URL reach the UI (tooltip, details
            # pane or the "open page" action): the browser history is a record
            raw = r.extra.get("page")
            if raw:
                r.extra["page"] = safe_page_url(str(raw), self.query)
        added = self.model.add(rows)
        if added:
            # the enricher can fill in 资源数 / 文件大小 / 文件类型 after the row
            # was inserted, so keep the active sort order honest
            if self.table.horizontalHeader().sortIndicatorSection() in (
                    ResultModel.COL_SEEDS, ResultModel.COL_SIZE,
                    ResultModel.COL_TYPE):
                self.model.refresh_all()
        else:
            # nothing new -> these are in-place 资源数 updates from the enricher
            for r in rows:
                self.model.refresh_row(r)
        self.lbl_tab_status.setText("%d 条 · %.0fs"
                                    % (self.model.rowCount(),
                                       time.monotonic() - self.started_at))
        self.sig_title.emit(self._label())
        if self.owner.current_tab() is self:
            self.owner._update_copy_enabled()

    @QtCore.pyqtSlot(str, str)
    def _on_status(self, sid: str, msg: str) -> None:
        from ..core.sources import REGISTRY
        if sid == "enrich":
            # make it visible on the tab itself: a background tab that is
            # filling in 资源数 otherwise looks finished-but-empty for a minute
            self._phase = "" if "完成" in msg else "enrich"
            text = "补充资源数… %s" % msg
            self.sig_title.emit(self._label())
        else:
            s = REGISTRY.get(sid)
            text = "%s: %s" % (s.label if s else sid, msg)
        self.lbl_tab_status.setText(text)
        if self.owner.current_tab() is self:
            self.sig_message.emit(text)

    @QtCore.pyqtSlot(str)
    def _on_done(self, _msg: str) -> None:
        """Fired once when the index phase ends and again when enrichment ends."""
        self._flush_timer.stop()
        self._flush_results()
        self.model.refresh_all()
        try:
            summary = self.session.summary() if self.session is not None else ""
        except Exception:  # noqa: BLE001
            summary = ""
        if summary:
            self.lbl_tab_status.setText(summary)
        else:
            self.lbl_tab_status.setText("%d 条" % self.model.rowCount())
        self._phase = ""
        self.sig_title.emit(self._label())
        if self.owner.current_tab() is self:
            self.owner._update_copy_enabled()
            if summary:
                self.sig_message.emit(summary)
        # the proxy may have filled rows in after the user started another tab
        self.setToolTip(self.lbl_tab_status.text())

    # -- title ---------------------------------------------------------
    def _label(self) -> str:
        text = self.query or "(空)"
        n = self.model.rowCount()
        if self._phase == "enrich":
            return "%s (%d) 补资源数…" % (text, n)
        if self.running:
            return "%s …" % text
        if n:
            return "%s (%d)" % (text, n)
        return text

    @QtCore.pyqtSlot(str)
    def _apply_title(self, text: str) -> None:
        i = self.owner.tabs.indexOf(self)
        if i >= 0:
            self.owner.tabs.setTabText(i, text)
            self.owner.tabs.setTabToolTip(i, self.lbl_tab_status.text() or text)

    # -- results access ------------------------------------------------
    def results(self) -> List[SearchResult]:
        """Rows in the order the user currently sees them."""
        out = []
        for i in range(self.proxy_model.rowCount()):
            r = self.proxy_model.data(self.proxy_model.index(i, 0),
                                      ResultModel.RESULT_ROLE)
            if r is not None:
                out.append(r)
        return out

    def selected_results(self) -> List[SearchResult]:
        sm = self.table.selectionModel()
        if sm is None:
            return []
        out = []
        for idx in sm.selectedRows():
            r = self.proxy_model.data(idx, ResultModel.RESULT_ROLE)
            if r is not None:
                out.append(r)
        return out

    def links_for(self, results: List[SearchResult]) -> str:
        parts = []
        for r in results:
            text = self.model.display_link(r)
            if text:
                parts.append(text)
        return "\r\n".join(parts)

    def invert_selection(self) -> None:
        sm = self.table.selectionModel()
        if sm is None:
            return
        sel = {i.row() for i in sm.selectedRows()}
        sm.clearSelection()
        selection = QtCore.QItemSelection()
        n = self.proxy_model.rowCount()
        for row in range(n):
            if row not in sel:
                selection.select(self.proxy_model.index(row, 0),
                                 self.proxy_model.index(
                                     row, self.model.columnCount() - 1))
        sm.select(selection, QtCore.QItemSelectionModel.Select)

    def set_format(self, key: str) -> None:
        self.model.set_format(key)

    # -- context menu / details ---------------------------------------
    def _context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        a1 = menu.addAction("复制选中链接")
        a1.triggered.connect(self.owner.copy_selected)
        a2 = menu.addAction("复制全部结果链接")
        a2.triggered.connect(self.owner.copy_all)
        menu.addSeparator()
        a3 = menu.addAction("复制名称")
        a3.triggered.connect(lambda: self.owner._to_clipboard(
            "\r\n".join(r.name for r in self.selected_results()), "名称"))
        a4 = menu.addAction("重新获取资源数")
        a4.triggered.connect(self.owner.reenrich_selected)
        menu.addSeparator()
        a5 = menu.addAction("在浏览器中打开页面")
        a5.triggered.connect(self.open_page)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def show_details(self) -> None:
        rows = self.selected_results()
        if not rows:
            self.details.clear()
            return
        r = rows[0]
        srcs = ", ".join(s for s in (r.extra.get("srcs") or [r.source]) if s)
        info = [
            "名称: %s" % r.name,
            "资源数: %s%s" % (r.seeds_display,
                            "" if r.seeds_verified else "  (未确认)"),
            "文件大小: %s%s" % (r.size_display,
                              "  (取自文件名，仅供参考)"
                              if r.extra.get("size_from_name") else ""),
            "文件类型: %s" % r.type_display,
            "来源: %s" % srcs,
        ]
        if r.peers:
            info.append("下载中(leechers): %d" % r.peers)
        if r.infohash:
            info.append("infohash: %s" % r.infohash)
        if r.ed2k_hash:
            info.append("ed2k hash: %s" % r.ed2k_hash.upper())
        info.append("")
        info.append("原始: %s" % r.link)
        for label, key in LINK_FORMATS[1:]:
            conv = {"magnet": link_to_magnet, "ed2k": link_to_ed2k}.get(key)
            val = link_to_thunder(r.link) if key == "thunder" else (
                conv(r.link, r.name, r.size) if conv else "")
            info.append("%s: %s" % (label, val or "（无法转换）"))
        self.details.setPlainText("\n".join(info))

    def open_page(self) -> None:
        rows = self.selected_results()
        if not rows:
            return
        raw = str(rows[0].extra.get("page") or "")
        if not raw:
            self.sig_message.emit("该结果没有可打开的页面")
            return
        # never hand a keyword-bearing URL to the browser: the query string is
        # stripped, and a URL whose *path* contains the search term is refused
        page = safe_page_url(raw, self.query)
        if not page:
            self.sig_message.emit(
                "该结果只有带关键词的搜索页，不打开（避免关键词进入浏览器历史）")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(page))

    def reenrich(self, rows: List[SearchResult]) -> None:
        """Kick off a background 资源数 refresh for ``rows`` of this tab."""
        import threading

        def done(r: SearchResult) -> None:
            self.sig_results.emit([r])

        def work() -> None:
            from ..core.enrich import enrich
            try:
                enrich(rows, workers=6, use_dht=True, on_done=done)
            finally:
                self.sig_done.emit("")

        threading.Thread(target=work, daemon=True).start()
        self.lbl_tab_status.setText("正在重新获取资源数 (%d)…" % len(rows))

    # -- column layout -------------------------------------------------
    #: 默认列宽按可用宽度配比（名称 资源数 大小 类型 链接）
    _COL_WEIGHTS = (0.30, 0.075, 0.095, 0.085, 0.32, 0.125)

    def fit_columns(self) -> None:
        """Distribute the available width over the columns (default layout)."""
        if not self._auto_fit:
            return
        total = self.table.viewport().width()
        if total < 200:
            return
        self._fitting = True
        try:
            for logical, weight in enumerate(self._COL_WEIGHTS):
                self.table.setColumnWidth(logical, max(48, int(total * weight)))
        finally:
            self._fitting = False

    def _on_column_resized(self, *_a) -> None:
        # fires for our own fit_columns() too; the flag tells them apart
        if self._fitting:
            return
        if self._auto_fit:
            self._auto_fit = False
            self.owner.save_table_layout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit_columns()

    def layout_state(self) -> dict:
        try:
            state = self.table.horizontalHeader().saveState()
            return {"table_state": bytes(state.toBase64()).decode("ascii"),
                    "table_columns": len(ResultModel.HEADERS),
                    "table_autofit": bool(self._auto_fit)}
        except Exception:  # noqa: BLE001
            return {}

    def apply_layout(self, state: Optional[dict]) -> None:
        """Restore a saved layout, or inherit the current tab's."""
        if not state:
            return
        if state.get("table_columns") != len(ResultModel.HEADERS):
            # Old five-column layouts must not hide the newly added source.
            self._auto_fit = True
            return
        blob = state.get("table_state")
        if not blob:
            return
        try:
            ba = QtCore.QByteArray.fromBase64(blob.encode("ascii"))
            if self.table.horizontalHeader().restoreState(ba):
                self._auto_fit = bool(state.get("table_autofit", False))
        except Exception:  # noqa: BLE001
            pass

    def reset_columns(self) -> None:
        self._auto_fit = True
        hh = self.table.horizontalHeader()
        # restoreState() cannot undo a drag -- an empty QByteArray is a no-op --
        # so the sections have to be walked back into logical order by hand
        for logical in range(len(ResultModel.HEADERS)):
            visual = hh.visualIndex(logical)
            if visual != logical:
                hh.moveSection(visual, logical)
        self.table.sortByColumn(ResultModel.COL_SEEDS, QtCore.Qt.DescendingOrder)
        self.fit_columns()
        self.owner.save_table_layout()
        self.sig_message.emit("列布局已重置为默认（名称/资源数/大小/类型/链接/来源）")
