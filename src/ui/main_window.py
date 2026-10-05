"""The main window: keyword search over magnets / eD2k / thunder links.

One search per tab -- a new tab is opened for every search, so a slow query
does not block the next one.  See :mod:`search_tab` for the per-search half.

Nothing about what the user searched is written to disk: no query is stored in
``settings.json``, and the only place a keyword could have reached a log file
(a failed-request URL in ``http.py``) is redacted.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit
from typing import Dict, List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from ..core.models import SearchResult
from ..core.proxy.core import MihomoCore, data_dir, data_dir_label
from ..core.sources import REGISTRY, load_dynamic_sources
from .result_model import (APP_TITLE, LINK_FORMATS, ResultModel,  # noqa: F401
                           ResultSorter)
from .search_tab import SearchTab

log = logging.getLogger("tsearch.ui")

#: keys that must never be written to settings.json
_FORBIDDEN_SETTINGS = ("last_query", "query", "history", "recent", "search")


class MainWindow(QtWidgets.QMainWindow):

    #: marshals proxy events from worker threads onto the GUI thread
    sig_proxy = QtCore.pyqtSignal(str, str)

    def __init__(self, start_proxy: bool = True) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 720)
        self.setMinimumSize(900, 520)

        self.settings_path = os.path.join(data_dir(), "settings.json")
        self.settings = self._load_settings()

        self.core: Optional[MihomoCore] = None
        self._pending_query = ""
        self._pending_deadline = 0.0

        self._build_ui()
        self._build_menu()
        self._wire_signals()
        self._restore_sources()
        self._restore_geometry()

        load_dynamic_sources()
        self._populate_sources_menu()

        self.new_tab(focus=True)

        if start_proxy:
            QtCore.QTimer.singleShot(200, self._start_proxy)

    # -- settings ------------------------------------------------------
    def _load_settings(self) -> dict:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            self.settings["link_format"] = tab.model.fmt
        self.settings["sources"] = REGISTRY.snapshot()
        self.settings["geometry"] = [self.width(), self.height()]
        # 不保存搜索记录：查询词、标签标题、结果一概不落盘
        for key in _FORBIDDEN_SETTINGS:
            self.settings.pop(key, None)
        self.save_table_layout()
        try:
            with open(self.settings_path, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _restore_sources(self) -> None:
        cfg = self.settings.get("sources")
        if isinstance(cfg, dict):
            for sid, on in cfg.items():
                s = REGISTRY.get(sid)
                if s is not None:
                    s.enabled = bool(on)

    def _restore_geometry(self) -> None:
        geo = self.settings.get("geometry")
        if isinstance(geo, list) and len(geo) == 2:
            try:
                self.resize(int(geo[0]), int(geo[1]))
            except (TypeError, ValueError):
                pass

    # -- column layout -------------------------------------------------
    def save_table_layout(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self.settings.update(tab.layout_state())

    # -- ui ------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        # ---- search row
        top = QtWidgets.QHBoxLayout()
        self.edit = QtWidgets.QLineEdit()
        self.edit.setPlaceholderText(
            "输入关键词（支持模糊匹配，例如：火影 1080p / ubuntu / naruto）")
        self.edit.setClearButtonEnabled(True)
        self.edit.returnPressed.connect(self.start_search)
        f = self.edit.font()
        f.setPointSize(f.pointSize() + 2)
        self.edit.setFont(f)

        self.btn_search = QtWidgets.QPushButton("搜索（新标签）")
        self.btn_search.setDefault(True)
        self.btn_search.setMinimumWidth(110)
        self.btn_search.setToolTip(
            "每次搜索都会新开一个标签，可以在上一个还没出结果时接着搜下一个 (Ctrl+Enter)")
        self.btn_stop = QtWidgets.QPushButton("停止当前")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setMinimumWidth(80)
        self.btn_stop.setToolTip("只停止当前标签的搜索；关闭标签也会停止它")

        top.addWidget(self.edit, 1)
        top.addWidget(self.btn_search)
        top.addWidget(self.btn_stop)
        root.addLayout(top)

        # ---- toolbar row
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)

        self.cmb_format = QtWidgets.QComboBox()
        for label, key in LINK_FORMATS:
            self.cmb_format.addItem(label, key)
        idx = self.cmb_format.findData(self.settings.get("link_format", "auto"))
        self.cmb_format.setCurrentIndex(max(0, idx))
        self.cmb_format.setToolTip("表格中链接的显示格式；复制时也使用该格式（所有标签）")
        bar.addWidget(QtWidgets.QLabel("链接格式:"))
        bar.addWidget(self.cmb_format)

        bar.addSpacing(10)
        self.btn_copy = QtWidgets.QPushButton("复制选中链接")
        self.btn_copy.setToolTip("每行一个链接 (Ctrl+C)")
        self.btn_copy_all = QtWidgets.QPushButton("复制全部结果")
        self.btn_select_all = QtWidgets.QPushButton("全选")
        self.btn_invert = QtWidgets.QPushButton("反选")
        for b in (self.btn_copy, self.btn_copy_all, self.btn_select_all,
                  self.btn_invert):
            bar.addWidget(b)

        bar.addStretch(1)
        self.btn_new_tab = QtWidgets.QPushButton("新建标签")
        self.btn_sources = QtWidgets.QPushButton("数据源")
        self.btn_import = QtWidgets.QPushButton("导入代理内容")
        self.btn_import.setToolTip("粘贴 Clash/mihomo 配置或节点链接，作为代理来源")
        self.btn_speedtest = QtWidgets.QPushButton("节点测速")
        for b in (self.btn_new_tab, self.btn_sources, self.btn_import,
                  self.btn_speedtest):
            bar.addWidget(b)
        root.addLayout(bar)

        # ---- tabs: one search each
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(QtCore.Qt.ElideRight)
        root.addWidget(self.tabs, 1)

        # ---- status bar (shared by all tabs)
        sb = self.statusBar()
        self.lbl_status = QtWidgets.QLabel("就绪")
        self.lbl_proxy = QtWidgets.QLabel("代理: 未启动")
        self.lbl_proxy.setMinimumWidth(320)
        sb.addWidget(self.lbl_status, 1)
        sb.addPermanentWidget(self.lbl_proxy)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("文件(&F)")
        act_new = m_file.addAction("新建搜索标签")
        act_new.setShortcut("Ctrl+T")
        act_new.triggered.connect(lambda: self.new_tab(focus=True))
        act_close = m_file.addAction("关闭当前标签")
        act_close.setShortcut("Ctrl+W")
        act_close.triggered.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        m_file.addSeparator()
        act_dir = m_file.addAction("打开数据目录")
        act_dir.triggered.connect(self._open_data_dir)
        act_log = m_file.addAction("查看 mihomo 日志")
        act_log.triggered.connect(self._open_mihomo_log)
        m_file.addSeparator()
        act_quit = m_file.addAction("退出")
        act_quit.triggered.connect(self.close)

        m_search = mb.addMenu("搜索(&S)")
        act_focus = m_search.addAction("聚焦搜索框")
        act_focus.setShortcut("Ctrl+L")
        act_focus.triggered.connect(lambda: (self.edit.setFocus(),
                                            self.edit.selectAll()))
        act_copy = m_search.addAction("复制选中链接")
        act_copy.setShortcut("Ctrl+C")
        act_copy.triggered.connect(self.copy_selected)
        act_copy_all = m_search.addAction("复制全部")
        act_copy_all.setShortcut("Ctrl+Shift+C")
        act_copy_all.triggered.connect(self.copy_all)

        m_link = mb.addMenu("链接(&L)")
        for label, key in LINK_FORMATS:
            a = m_link.addAction(label)
            a.triggered.connect(lambda _c, k=key: self._set_format(k))

        m_view = mb.addMenu("视图(&V)")
        a_reset = m_view.addAction("重置当前标签的列布局")
        a_reset.triggered.connect(self._reset_columns)
        a_fit = m_view.addAction("按窗口宽度分配列宽")
        a_fit.triggered.connect(self._fit_columns)

        self.m_sources = mb.addMenu("数据源(&D)")
        self.m_proxy = mb.addMenu("代理(&P)")

        m_help = mb.addMenu("帮助(&H)")
        a_about = m_help.addAction("关于")
        a_about.triggered.connect(self._about)

    def _populate_sources_menu(self) -> None:
        self.m_sources.clear()
        for s in REGISTRY.all():
            act = QtWidgets.QAction("%s  (%s)" % (s.label, ", ".join(s.kinds)),
                                    self.m_sources, checkable=True)
            act.setChecked(s.enabled)
            act.toggled.connect(lambda on, src=s: self._toggle_source(src, on))
            self.m_sources.addAction(act)
        self.m_sources.addSeparator()
        act_all = self.m_sources.addAction("全部启用")
        act_all.triggered.connect(lambda: self._set_all_sources(True))
        act_none = self.m_sources.addAction("全部禁用")
        act_none.triggered.connect(lambda: self._set_all_sources(False))

        self.m_proxy.clear()
        a_start = self.m_proxy.addAction("启动代理")
        a_start.triggered.connect(self._start_proxy)
        a_stop = self.m_proxy.addAction("停止代理")
        a_stop.triggered.connect(self._stop_proxy)
        a_test = self.m_proxy.addAction("节点测速并切换最快")
        a_test.triggered.connect(self._speedtest)
        a_refresh = self.m_proxy.addAction("重新获取订阅")
        a_refresh.triggered.connect(self._refresh_subscription)
        a_subscription = self.m_proxy.addAction("设置订阅地址…")
        a_subscription.triggered.connect(self._configure_subscription)
        self.m_proxy.addSeparator()
        a_import = self.m_proxy.addAction("导入代理内容（粘贴文本）…")
        a_import.triggered.connect(self._import_proxy_text)
        a_import_file = self.m_proxy.addAction("导入配置文件…")
        a_import_file.triggered.connect(self._import_proxy_file)
        a_clear = self.m_proxy.addAction("清空已导入的节点")
        a_clear.triggered.connect(self._clear_imported)
        self.m_proxy.addSeparator()
        a_dir = self.m_proxy.addAction("打开数据目录")
        a_dir.triggered.connect(self._open_data_dir)

    def _wire_signals(self) -> None:
        self.sig_proxy.connect(self._on_proxy_event)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.btn_search.clicked.connect(self.start_search)
        self.btn_stop.clicked.connect(self.stop_search)
        self.btn_new_tab.clicked.connect(lambda: self.new_tab(focus=True))
        self.btn_copy.clicked.connect(self.copy_selected)
        self.btn_copy_all.clicked.connect(self.copy_all)
        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_invert.clicked.connect(self.invert_selection)
        self.btn_speedtest.clicked.connect(self._speedtest)
        self.btn_import.clicked.connect(self._import_proxy_text)
        self.cmb_format.currentIndexChanged.connect(
            lambda i: self._set_format(self.cmb_format.itemData(i)))
        # Ctrl+Enter searches without leaving the search box
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self,
                            activated=self.start_search)

    # -- tabs ----------------------------------------------------------
    def new_tab(self, query: str = "", focus: bool = False) -> SearchTab:
        """Open a tab; it inherits the current tab's column layout."""
        ref = self.current_tab()
        tab = SearchTab(self, query=query)
        if ref is not None:
            tab.apply_layout(ref.layout_state())
        else:
            tab.apply_layout(self.settings)
        i = self.tabs.addTab(tab, tab.query or "(空)")
        if focus:
            self.tabs.setCurrentIndex(i)
            self.edit.setFocus()
            self.edit.selectAll()
        self._on_tab_changed(self.tabs.currentIndex())
        return tab

    def current_tab(self) -> Optional[SearchTab]:
        w = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        return w if isinstance(w, SearchTab) else None

    def close_tab(self, index: int) -> None:
        if index < 0:
            return
        w = self.tabs.widget(index)
        if isinstance(w, SearchTab):
            w.cancel()
        self.tabs.removeTab(index)
        w.deleteLater() if w is not None else None
        if self.tabs.count() == 0:
            self.new_tab(focus=True)
        self._on_tab_changed(self.tabs.currentIndex())

    def _on_tab_changed(self, _index: int) -> None:
        tab = self.current_tab()
        if tab is not None:
            self.settings["link_format"] = tab.model.fmt
            i = self.cmb_format.findData(tab.model.fmt)
            if i >= 0 and self.cmb_format.currentIndex() != i:
                self.cmb_format.blockSignals(True)
                self.cmb_format.setCurrentIndex(i)
                self.cmb_format.blockSignals(False)
            QtCore.QTimer.singleShot(0, tab.fit_columns)
            if tab.lbl_tab_status.text():
                self.lbl_status.setText(tab.lbl_tab_status.text())
        self._update_copy_enabled()
        self._update_search_buttons()

    # -- compatibility accessors (tests / scripts) ---------------------
    @property
    def model(self) -> Optional[ResultModel]:
        tab = self.current_tab()
        return tab.model if tab is not None else None

    @property
    def proxy_model(self) -> Optional[ResultSorter]:
        tab = self.current_tab()
        return tab.proxy_model if tab is not None else None

    @property
    def table(self):
        tab = self.current_tab()
        return tab.table if tab is not None else None

    @property
    def filter_edit(self):
        tab = self.current_tab()
        return tab.filter_edit if tab is not None else None

    @property
    def details(self):
        tab = self.current_tab()
        return tab.details if tab is not None else None

    @property
    def session(self):
        tab = self.current_tab()
        return tab.session if tab is not None else None

    # -- search --------------------------------------------------------
    def queue_search(self, query: str, wait_for_proxy: float = 45.0) -> None:
        """Run a search once the proxy is up (or after ``wait_for_proxy``).

        Launching straight into a search would race the mihomo start-up and
        the first query would run entirely on the (often blocked) direct
        route.
        """
        self.edit.setText(query)
        self._pending_query = query
        self._pending_deadline = time.monotonic() + max(0.0, wait_for_proxy)
        if self.core is None or self.core.running:
            QtCore.QTimer.singleShot(150, self._run_pending_search)
            return
        self.lbl_status.setText("等待代理就绪后自动搜索…")
        QtCore.QTimer.singleShot(1000, self._pending_tick)

    def _pending_tick(self) -> None:
        if not getattr(self, "_pending_query", ""):
            return
        if self.core is not None and self.core.running:
            self._run_pending_search()
            return
        if time.monotonic() >= getattr(self, "_pending_deadline", 0):
            self.lbl_status.setText("代理未就绪，仍以直连方式搜索")
            self._run_pending_search()
            return
        QtCore.QTimer.singleShot(1000, self._pending_tick)

    def _run_pending_search(self) -> None:
        q = getattr(self, "_pending_query", "")
        self._pending_query = ""
        if q:
            self.edit.setText(q)
            self.start_search()

    def start_search(self) -> None:
        """Every search gets its own tab, so searches run in parallel."""
        query = self.edit.text().strip()
        if not query:
            self.lbl_status.setText("请输入关键词")
            return

        sources = REGISTRY.enabled()
        if not sources:
            QtWidgets.QMessageBox.information(
                self, "没有数据源", "请在「数据源」菜单中至少启用一个数据源。")
            return

        tab = self.current_tab()
        # reuse the tab only while it is still pristine (nothing typed, nothing
        # found); otherwise every search opens a fresh tab
        reusable = (tab is not None and tab.session is None
                    and tab.model.rowCount() == 0 and not tab.query)
        if not reusable:
            tab = self.new_tab(focus=True)
        tab.start(query, sources)
        self._update_search_buttons()

    def stop_search(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.stop()
        self._update_search_buttons()

    def _update_search_buttons(self) -> None:
        tab = self.current_tab()
        running = bool(tab is not None and tab.running)
        self.btn_stop.setEnabled(running)
        self.btn_search.setEnabled(True)

    # -- copy ----------------------------------------------------------
    def _update_copy_enabled(self) -> None:
        tab = self.current_tab()
        if tab is None:
            self.btn_copy.setEnabled(False)
            self.btn_copy_all.setEnabled(False)
            return
        sm = tab.table.selectionModel()
        self.btn_copy.setEnabled(bool(sm is not None and sm.hasSelection()))
        self.btn_copy_all.setEnabled(tab.model.rowCount() > 0)

    def _to_clipboard(self, text: str, what: str) -> None:
        if not text:
            self.lbl_status.setText("没有可复制的内容")
            return
        QtWidgets.QApplication.clipboard().setText(text)
        n = len([x for x in text.split("\r\n") if x])
        self.lbl_status.setText("已复制 %d 条%s链接到剪贴板" % (n, what))

    def copy_selected(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self._to_clipboard(tab.links_for(tab.selected_results()), "选中")

    def copy_all(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self._to_clipboard(tab.links_for(tab.results()), "全部")

    def _select_all(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.table.selectAll()

    def invert_selection(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.invert_selection()

    def _show_details(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.show_details()

    def reenrich_selected(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        rows = [r for r in tab.selected_results() if r.kind == "magnet"]
        if not rows:
            self.lbl_status.setText("选中结果中没有磁力链接")
            return
        tab.reenrich(rows)

    # -- format --------------------------------------------------------
    def _set_format(self, key: str) -> None:
        """Applies to every tab, so the copy format never depends on focus."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SearchTab):
                w.set_format(key)
        i = self.cmb_format.findData(key)
        if i >= 0 and self.cmb_format.currentIndex() != i:
            self.cmb_format.setCurrentIndex(i)
        self.lbl_status.setText("链接格式: %s" % dict(
            (k, l) for l, k in LINK_FORMATS).get(key, key))

    # -- column layout -------------------------------------------------
    def _fit_columns(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab._auto_fit = True
            tab.fit_columns()
            self.save_table_layout()

    def _reset_columns(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.reset_columns()

    # -- sources -------------------------------------------------------
    def _toggle_source(self, src, on: bool) -> None:
        src.enabled = bool(on)
        self._save_settings()

    def _set_all_sources(self, on: bool) -> None:
        for s in REGISTRY.all():
            s.enabled = bool(on)
        self._populate_sources_menu()
        self._save_settings()

    # -- proxy ---------------------------------------------------------
    def _configure_subscription(self) -> bool:
        urls = self.settings.get("subscriptions") or []
        value, accepted = QtWidgets.QInputDialog.getText(
            self, "设置代理订阅", "填写 HTTP/HTTPS 订阅地址（仅保存在本机）：",
            QtWidgets.QLineEdit.Password, urls[0] if urls else "")
        if not accepted:
            return False
        value = value.strip()
        try:
            parts = urlsplit(value)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            valid = False
        if not valid:
            QtWidgets.QMessageBox.warning(self, "订阅地址无效", "请填写完整的 HTTP/HTTPS 订阅地址。")
            return False
        self.settings["subscriptions"] = [value]
        self._save_settings()
        if self.core is not None:
            self.core.subscriptions = [value]
        self.lbl_status.setText("订阅已保存，正在获取节点…")
        if self.core is not None and self.core.running:
            self._refresh_subscription()
        else:
            self._start_proxy()
        return True

    def _start_proxy(self) -> None:
        if self.core is not None and self.core.running:
            self.lbl_proxy.setText("代理: 已运行 · %s" % self.core.current_node)
            return
        urls = self.settings.get("subscriptions") or None
        self.core = MihomoCore(
            subscription_urls=urls,
            on_event=lambda kind, msg: self.sig_proxy.emit(kind, msg))
        if not self.core.subscriptions and not self.core.load_user_nodes():
            self.lbl_proxy.setText("代理: 未配置订阅")
            if self._configure_subscription():
                return
            self.lbl_status.setText("可在「代理 → 设置订阅地址」配置；当前使用直连")
            return
        self.lbl_proxy.setText("代理: 启动中…")

        def work():
            self.core.start()
        threading.Thread(target=work, daemon=True).start()

    def _stop_proxy(self) -> None:
        if self.core is not None:
            self.core.stop()
        self.lbl_proxy.setText("代理: 已停止")

    def _speedtest(self) -> None:
        if self.core is None or not self.core.running:
            self.lbl_status.setText("代理未运行，先启动代理")
            return
        self.lbl_status.setText("节点测速中…")

        def work():
            best = self.core.manual_sweep()
            self.sig_proxy.emit("node", "%s|0|0" % best if best else "nomatch")
        threading.Thread(target=work, daemon=True).start()

    def _refresh_subscription(self) -> None:
        if self.core is None or not self.core.running:
            self._start_proxy()
            return
        self.lbl_status.setText("正在重新获取订阅…")

        def work():
            ok = self.core.refresh_subscription()
            self.sig_proxy.emit("info" if ok else "warn",
                                "订阅已更新 (%d 节点)" % len(self.core.proxies)
                                if ok else "订阅获取失败")
        threading.Thread(target=work, daemon=True).start()

    # -- proxy text import ---------------------------------------------
    def _import_proxy_text(self) -> None:
        from .import_dialog import ImportProxyDialog, parse_for_preview
        dlg = ImportProxyDialog(self, on_parse=parse_for_preview)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._apply_imported(dlg.parsed)

    def _import_proxy_file(self) -> None:
        from .import_dialog import ImportProxyDialog, parse_for_preview
        dlg = ImportProxyDialog(self, on_parse=parse_for_preview)
        dlg._load_file()
        if not dlg.edit.toPlainText().strip():
            return
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._apply_imported(dlg.parsed)

    def _apply_imported(self, proxies: list) -> None:
        if not proxies:
            return
        if self.core is None:
            self.core = MihomoCore(
                subscription_urls=self.settings.get("subscriptions") or None,
                on_event=lambda kind, msg: self.sig_proxy.emit(kind, msg))
        self.lbl_status.setText("正在校验并应用 %d 个导入节点…" % len(proxies))

        def work():
            try:
                self.core.apply_user_nodes(proxies)
                self.sig_proxy.emit("node", "%s|0|%d" % (
                    self.core.current_node or "导入节点", len(self.core.proxies)))
            except Exception as exc:  # noqa: BLE001
                self.sig_proxy.emit("error", "导入失败: %s" % exc)
        threading.Thread(target=work, daemon=True).start()

    def _clear_imported(self) -> None:
        if self.core is None:
            self.core = MihomoCore(
                subscription_urls=self.settings.get("subscriptions") or None,
                on_event=lambda kind, msg: self.sig_proxy.emit(kind, msg))
        self.core.clear_user_nodes()
        self.core._pushed_nodes = []
        self.lbl_status.setText("已清空手动导入的节点，下次刷新订阅后生效")

    @QtCore.pyqtSlot(str, str)
    def _on_proxy_event(self, kind: str, msg: str) -> None:
        if kind == "node":
            parts = msg.split("|")
            name = parts[0]
            try:
                delay = int(parts[1])
                total = int(parts[2])
            except (IndexError, ValueError):
                delay = total = 0
            if delay:
                self.lbl_proxy.setText("代理: 运行中 · %s · %dms · %d个可用"
                                       % (name, delay, total))
            else:
                self.lbl_proxy.setText("代理: 运行中 · %s" % name)
            return
        if kind == "error":
            self.lbl_proxy.setText("代理: 错误 · %s" % msg[:60])
        elif kind == "warn":
            self.lbl_proxy.setText("代理: %s" % msg[:70])
        elif self.core is not None and self.core.running:
            self.lbl_proxy.setText("代理: 运行中 · %s" % (self.core.current_node or "自动"))
        self.lbl_status.setText("代理: %s" % msg[:120])

    # -- misc ----------------------------------------------------------
    def _open_data_dir(self) -> None:
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(data_dir()))

    def _open_mihomo_log(self) -> None:
        path = os.path.join(data_dir(), "mihomo.log")
        if os.path.isfile(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        else:
            self.lbl_status.setText("暂无 mihomo 日志")

    def _about(self) -> None:
        mode = "%s\n数据目录: %s" % (data_dir_label(), data_dir())
        QtWidgets.QMessageBox.about(
            self, "关于",
            "<h3>TSearch-DS</h3>"
            "<p>磁力 / eD2k(电驴) / 迅雷 链接聚合搜索器 &nbsp;<b>from ds</b></p>"
            "<p>结果列 <b>名称 / 资源数 / 文件大小 / 文件类型 / 链接</b>；"
            "表头可拖动换序、可调宽度；多选后按 Ctrl+C 复制（每行一个链接）。</p>"
            "<p>每次搜索新开一个标签，可以并行搜索。</p>"
            "<p>不保存任何搜索记录：查询词不写入配置、不写入日志。</p>"
            "<p>内置 mihomo 代理核心，启动时自动获取订阅、测速选优，"
            "节点失效时自动切换到可用节点。</p>"
            "<p style='color:#8b949e'>%s</p>" % mode)

    def closeEvent(self, event) -> None:  # noqa: N802
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SearchTab):
                try:
                    w.cancel()
                except Exception:  # noqa: BLE001
                    pass
        self._save_settings()
        if self.core is not None:
            try:
                self.core.stop()
            except Exception:  # noqa: BLE001
                pass
        event.accept()
