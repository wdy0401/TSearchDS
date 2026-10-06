# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""The main window: keyword search over magnets / eD2k / thunder links (Tk).

One search per tab -- a new tab is opened for every search, so a slow query
does not block the next one.  See :mod:`search_tab` for the per-search half.

Nothing about what the user searched is written to disk: no query is stored in
``settings.json``, and the only place a keyword could have reached a log file
(a failed-request URL in ``http.py``) is redacted.

The public surface deliberately mirrors :class:`src.ui.main_window.MainWindow`
so ``main.py`` -- and the tests -- can drive either backend.
"""
from __future__ import annotations

from ..ui.i18n import tr
from ..ui import i18n
from ..ui.constants import APP_TITLE, LINK_FORMATS

import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from ..core.models import SearchResult  # noqa: F401  (re-exported)
from ..core.proxy.core import MihomoCore, data_dir, data_dir_label
from ..core.sources import REGISTRY, load_dynamic_sources
from .result_model import ResultModel  # noqa: F401  (re-exported)
from .search_tab import SearchTab
from .widgets import PlaceholderEntry, Tooltip, apply_default_font

log = logging.getLogger("tsearch.ui")

#: keys that must never be written to settings.json
_FORBIDDEN_SETTINGS = ("last_query", "query", "history", "recent", "search")


class TabBook:
    """A thin Qt-``QTabWidget``-shaped wrapper around ``ttk.Notebook``.

    Qt extras that Tk does not have: draggable tabs (``setMovable``) and real
    close buttons.  Close is emulated by appending "✕" to the label and
    locating the click with the notebook's own ``bbox``.
    """

    def __init__(self, master, on_close=None) -> None:
        self.nb = ttk.Notebook(master)
        self._widgets: List[tk.Misc] = []
        self._on_close = on_close
        self.nb.bind("<Button-1>", self._on_click)
        self.nb.bind("<Button-2>", self._on_middle_click)
        self.nb.bind("<<NotebookTabChanged>>", self._on_changed)
        self.on_changed = None

    # -- geometry helper -----------------------------------------------
    def _tab_bbox(self, index: int):
        try:
            return self.nb.tk.call(self.nb._w, "bbox", index)
        except Exception:  # noqa: BLE001 - no tabs / exotic theme
            return None

    def _index_at(self, x: int, y: int) -> int:
        try:
            return int(self.nb.tk.call(self.nb._w, "index", "@%d,%d" % (x, y)))
        except Exception:  # noqa: BLE001
            return -1

    def _on_click(self, event) -> None:
        index = self._index_at(event.x, event.y)
        if index < 0:
            return
        box = self._tab_bbox(index)
        if not box:
            return
        _x, _y, width, _h = box
        # the trailing "✕" occupies roughly the last 18 pixels
        if event.x >= width - 18 and self._on_close is not None:
            self._on_close(index)

    def _on_middle_click(self, event) -> None:
        index = self._index_at(event.x, event.y)
        if index >= 0 and self._on_close is not None:
            self._on_close(index)

    def _on_changed(self, _event=None) -> None:
        if self.on_changed is not None:
            self.on_changed(self.currentIndex())

    # -- QTabWidget-ish API --------------------------------------------
    def count(self) -> int:
        return len(self._widgets)

    def widget(self, index: int):
        if 0 <= index < len(self._widgets):
            return self._widgets[index]
        return None

    def indexOf(self, widget) -> int:  # noqa: N802 - Qt spelling
        try:
            return self._widgets.index(widget)
        except ValueError:
            return -1

    def addTab(self, widget, text: str) -> int:  # noqa: N802 - Qt spelling
        self._widgets.append(widget)
        self.nb.add(widget, text="%s ✕" % text)
        return len(self._widgets) - 1

    def removeTab(self, index: int) -> None:  # noqa: N802 - Qt spelling
        if not (0 <= index < len(self._widgets)):
            return
        widget = self._widgets.pop(index)
        try:
            self.nb.forget(widget)
        except Exception:  # noqa: BLE001
            pass

    def currentIndex(self) -> int:  # noqa: N802 - Qt spelling
        try:
            current = self.nb.select()
        except tk.TclError:
            return -1
        if isinstance(current, str):
            if not current:
                return -1
            try:
                # Notebook.select() answers with a widget *path*, not a widget
                current = self.nb.nametowidget(current)
            except Exception:  # noqa: BLE001
                return -1
        try:
            return self._widgets.index(current)
        except ValueError:
            return -1

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt spelling
        if 0 <= index < len(self._widgets):
            self.nb.select(self._widgets[index])

    def currentWidget(self):  # noqa: N802 - Qt spelling
        return self.widget(self.currentIndex())

    def tabText(self, index: int) -> str:  # noqa: N802 - Qt spelling
        widget = self.widget(index)
        if widget is None:
            return ""
        try:
            return str(self.nb.tab(widget, "text"))
        except Exception:  # noqa: BLE001
            return ""

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802
        widget = self.widget(index)
        if widget is None:
            return
        try:
            self.nb.tab(widget, text="%s ✕" % text)
        except Exception:  # noqa: BLE001
            pass

    def setTabToolTip(self, index: int, text: str) -> None:  # noqa: N802
        # ttk.Notebook has no per-tab tooltip; the text is kept on the tab so
        # :meth:`SearchTab._apply_title` call sites stay identical to Qt.
        widget = self.widget(index)
        if widget is not None:
            widget._tab_tooltip = text


class MainWindow:
    """Root window.  Not a widget subclass -- it *owns* a ``Tk`` root."""

    def __init__(self, start_proxy: bool = True, root=None) -> None:
        self.root = root if root is not None else tk.Tk()
        self._owns_root = root is None
        self._apply_scaling()
        apply_default_font(self.root, size=10)

        self.settings_path = os.path.join(data_dir(), "settings.json")
        self.settings = self._load_settings()
        i18n.set_language(self.settings.get('language', i18n.DEFAULT_LANGUAGE))
        self.root.title(tr(APP_TITLE))

        self.core: Optional[MihomoCore] = None
        self._pending_query = ""
        self._pending_deadline = 0.0
        #: worker threads -> GUI thread (see :meth:`post`)
        self._queue: "queue.Queue" = queue.Queue()
        self._jobs: List[str] = []
        #: everything that has to be re-labelled on a language switch
        self._tr_text: List[tuple] = []
        self._tr_placeholder: List[tuple] = []
        self._tooltips: Dict[tk.Misc, Tooltip] = {}
        self._menubar = None
        self._source_vars: List[tuple] = []

        self._build_ui()
        self._build_menu()
        self._wire_signals()
        self._restore_sources()
        self._restore_geometry()

        load_dynamic_sources()
        self._populate_sources_menu()

        self.new_tab(focus=True)

        if start_proxy:
            self.root.after(200, self._start_proxy)

    def _apply_scaling(self) -> None:
        """Qt 6 is high-DPI aware out of the box; Tk needs telling.

        A DPI-aware CPython reports real pixels, so Tk's 1/72in logical unit
        has to be rescaled or every widget comes out tiny on a 125%/150%
        display.
        """
        try:
            dpi = self.root.winfo_fpixels("1i")
            if dpi > 0:
                self.root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
        except Exception:  # noqa: BLE001 - headless / exotic Tk
            pass

    # -- translation bookkeeping ---------------------------------------
    def _label(self, widget, text: str) -> tk.Misc:
        widget.configure(text=tr(text))
        self._tr_text.append((widget, text))
        return widget

    def _button(self, parent, text: str, command=None, **kwargs) -> ttk.Button:
        button = ttk.Button(parent, text=tr(text), command=command, **kwargs)
        self._tr_text.append((button, text))
        return button

    def _tip(self, widget, text: str) -> Tooltip:
        tip = Tooltip(widget, tr(text))
        self._tooltips[widget] = tip
        return tip

    def _placeholder(self, entry: PlaceholderEntry, text: str) -> PlaceholderEntry:
        entry.set_placeholder(tr(text), remember=True)
        self._tr_placeholder.append((entry, text))
        return entry

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
        try:
            self.settings["geometry"] = [self.root.winfo_width(),
                                         self.root.winfo_height()]
        except Exception:  # noqa: BLE001
            pass
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
                self.root.geometry("%dx%d" % (int(geo[0]), int(geo[1])))
            except (TypeError, ValueError):
                pass

    def save_table_layout(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self.settings.update(tab.layout_state())

    # -- ui ------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.geometry("1180x720")
        self.root.minsize(900, 520)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        shell = ttk.Frame(self.root, padding=(10, 10, 10, 8))
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)

        # ---- search row
        top = ttk.Frame(shell)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(0, weight=1)
        self.edit = PlaceholderEntry(
            top, placeholder=tr("输入关键词（支持模糊匹配，例如：火影 1080p / ubuntu / naruto）"))
        self._placeholder(self.edit,
                          "输入关键词（支持模糊匹配，例如：火影 1080p / ubuntu / naruto）")
        self.edit.enable_clear_button()
        self.edit.grid(row=0, column=0, sticky="ew")
        self.edit.bind("<Return>", lambda _e: self.start_search())

        self.btn_search = self._button(top, "搜索（新标签）", self.start_search,
                                       width=14)
        self.btn_search.grid(row=0, column=1, padx=(6, 0))
        self._tip(self.btn_search,
                  "每次搜索都会新开一个标签，可以在上一个还没出结果时接着搜下一个 (Ctrl+Enter)")
        self.btn_stop = self._button(top, "停止当前", self.stop_search,
                                     width=10, state="disabled")
        self.btn_stop.grid(row=0, column=2, padx=(6, 0))
        self._tip(self.btn_stop, "只停止当前标签的搜索；关闭标签也会停止它")

        # ---- toolbar row
        bar = ttk.Frame(shell)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        self._label(ttk.Label(bar), "链接格式:").pack(side="left")
        self.cmb_format = ttk.Combobox(
            bar, state="readonly", width=18,
            values=[tr(label) for label, _key in LINK_FORMATS])
        self.cmb_format.pack(side="left", padx=(4, 10))
        self.cmb_format.bind("<<ComboboxSelected>>", self._on_format_changed)
        self._tip(self.cmb_format, "表格中链接的显示格式；复制时也使用该格式（所有标签）")
        index = self._format_index(self.settings.get("link_format", "auto"))
        self.cmb_format.current(index)

        self.btn_copy = self._button(bar, "复制选中链接", self.copy_selected,
                                     state="disabled")
        self._tip(self.btn_copy, "每行一个链接 (Ctrl+C)")
        self.btn_copy_all = self._button(bar, "复制全部结果", self.copy_all,
                                         state="disabled")
        self.btn_select_all = self._button(bar, "全选", self._select_all)
        self.btn_invert = self._button(bar, "反选", self.invert_selection)
        for button in (self.btn_copy, self.btn_copy_all,
                       self.btn_select_all, self.btn_invert):
            button.pack(side="left", padx=(0, 6))

        self.btn_import = self._button(bar, "导入代理内容",
                                       self._import_proxy_text)
        self._tip(self.btn_import, "粘贴 Clash/mihomo 配置或节点链接，作为代理来源")
        self.btn_speedtest = self._button(bar, "节点测速", self._speedtest)
        self.btn_sources = self._button(bar, "数据源", self._sources_dialog)
        self.btn_new_tab = self._button(bar, "新建标签",
                                        lambda: self.new_tab(focus=True))
        for button in (self.btn_import, self.btn_speedtest,
                       self.btn_sources, self.btn_new_tab):
            button.pack(side="right", padx=(6, 0))

        # ---- tabs: one search each
        self.tabs = TabBook(shell, on_close=self.close_tab)
        self.tabs.on_changed = self._on_tab_changed
        self.tabs.nb.grid(row=2, column=0, sticky="nsew")

        # ---- status bar (shared by all tabs)
        status = ttk.Frame(shell)
        status.grid(row=3, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.lbl_status = self._label(ttk.Label(status), "就绪")
        self.lbl_status.grid(row=0, column=0, sticky="w")
        self.lbl_proxy = self._label(ttk.Label(status, width=44, anchor="e"),
                                     "代理: 未启动")
        self.lbl_proxy.grid(row=0, column=1, sticky="e")

    def _format_index(self, key: str) -> int:
        for i, (_label, k) in enumerate(LINK_FORMATS):
            if k == key:
                return i
        return 0

    def _on_format_changed(self, _event=None) -> None:
        index = self.cmb_format.current()
        if 0 <= index < len(LINK_FORMATS):
            self._set_format(LINK_FORMATS[index][1])

    def _build_menu(self) -> None:
        if getattr(self, "_menubar", None) is not None:
            try:
                self._menubar.destroy()
            except Exception:  # noqa: BLE001
                pass
        mb = tk.Menu(self.root)
        self._menubar = mb
        self.root.configure(menu=mb)

        m_file = self._add_menu(mb, "文件(&F)")
        self._add_item(m_file, "新建搜索标签", lambda: self.new_tab(focus=True),
                       "Ctrl+T")
        self._add_item(m_file, "关闭当前标签",
                       lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        m_file.add_separator()
        self._add_item(m_file, "打开数据目录", self._open_data_dir)
        self._add_item(m_file, "查看 mihomo 日志", self._open_mihomo_log)
        m_file.add_separator()
        self._add_item(m_file, "退出", self.close)

        m_search = self._add_menu(mb, "搜索(&S)")
        self._add_item(m_search, "聚焦搜索框", self._focus_search, "Ctrl+L")
        self._add_item(m_search, "复制选中链接", self.copy_selected, "Ctrl+C")
        self._add_item(m_search, "复制全部", self.copy_all, "Ctrl+Shift+C")

        m_link = self._add_menu(mb, "链接(&L)")
        for label, key in LINK_FORMATS:
            m_link.add_command(label=tr(label),
                               command=lambda k=key: self._set_format(k))

        m_view = self._add_menu(mb, "视图(&V)")
        self._add_item(m_view, "重置当前标签的列布局", self._reset_columns)
        self._add_item(m_view, "按窗口宽度分配列宽", self._fit_columns)

        self.m_sources = self._add_menu(mb, "数据源(&D)")
        self.m_proxy = self._add_menu(mb, "代理(&P)")

        m_help = self._add_menu(mb, "帮助(&H)")
        self._add_item(m_help, "关于", self._about)

        self.language_menu = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label='语言 / Language', menu=self.language_menu)
        self.language_actions: Dict[str, tk.BooleanVar] = {}
        for code, label in (('zh_CN', '中文'), ('en', 'English')):
            var = tk.BooleanVar(value=i18n.language() == code)
            self.language_actions[code] = var
            self.language_menu.add_radiobutton(
                label=label, value=True, variable=var,
                command=lambda value=code: self._set_language(value))

    def _add_menu(self, parent: tk.Menu, title: str) -> tk.Menu:
        menu = tk.Menu(parent, tearoff=0)
        parent.add_cascade(label=tr_text(title), menu=menu)
        return menu

    def _add_item(self, menu: tk.Menu, title: str, command,
                  shortcut: str = "") -> None:
        label = tr(title)
        if shortcut:
            label = "%s\t%s" % (label, shortcut)
        menu.add_command(label=label, command=command)

    def _wire_signals(self) -> None:
        root = self.root
        root.bind("<Control-Return>", lambda _e: self.start_search())
        root.bind("<Control-t>", lambda _e: self.new_tab(focus=True))
        root.bind("<Control-T>", lambda _e: self.new_tab(focus=True))
        root.bind("<Control-w>", lambda _e: self.close_tab(self.tabs.currentIndex()))
        root.bind("<Control-W>", lambda _e: self.close_tab(self.tabs.currentIndex()))
        root.bind("<Control-l>", lambda _e: self._focus_search())
        root.bind("<Control-L>", lambda _e: self._focus_search())
        root.bind("<Control-c>", self._on_ctrl_c)
        root.bind("<Control-C>", self._on_ctrl_c)
        root.bind("<Control-Shift-C>", lambda _e: self.copy_all())
        root.bind("<Control-Shift-c>", lambda _e: self.copy_all())
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _on_ctrl_c(self, event) -> Optional[str]:
        # let the native Entry/Text copy win when the caret is in a text box
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text, tk.Spinbox, tk.Listbox)):
            return None
        self.copy_selected()
        return "break"

    def _focus_search(self) -> None:
        self.edit.focus_set()
        self.edit.select_range(0, "end")

    def set_status(self, text: str) -> None:
        self.lbl_status.configure(text=text)

    def _after_idle(self, widget, method: str) -> None:
        """Schedule ``widget.method()``, skipping it if the widget is gone.

        Tk raises "invalid command name" for an ``after`` callback whose widget
        was destroyed in the meantime -- Qt just drops the queued signal.
        """
        def run() -> None:
            try:
                if widget.winfo_exists():
                    getattr(widget, method)()
            except Exception:  # noqa: BLE001
                pass
        self.root.after_idle(run)

    # -- thread marshalling --------------------------------------------
    def post(self, fn) -> None:
        """Run ``fn`` on the GUI thread (Qt would emit a signal)."""
        self._queue.put(fn)

    def drain(self) -> None:
        """Run everything queued so far.  Tests call this instead of looping."""
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:  # noqa: BLE001 - one bad callback must not
                log.exception("queued UI callback failed")  # kill the loop

    def _drain_loop(self) -> None:
        self.drain()
        self.root.after(50, self._drain_loop)

    # -- tabs ----------------------------------------------------------
    def new_tab(self, query: str = "", focus: bool = False) -> SearchTab:
        """Open a tab; it inherits the current tab's column layout."""
        ref = self.current_tab()
        tab = SearchTab(self, query=query)
        if ref is not None:
            tab.apply_layout(ref.layout_state())
        else:
            tab.apply_layout(self.settings)
        i = self.tabs.addTab(tab, tab.query or tr("(空)"))
        if focus:
            self.tabs.setCurrentIndex(i)
            self.edit.focus_set()
            self.edit.select_range(0, "end")
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
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass
        if self.tabs.count() == 0:
            self.new_tab(focus=True)
        self._on_tab_changed(self.tabs.currentIndex())

    def _on_tab_changed(self, _index: int = -1) -> None:
        tab = self.current_tab()
        if tab is not None:
            self.settings["link_format"] = tab.model.fmt
            i = self._format_index(tab.model.fmt)
            if self.cmb_format.current() != i:
                self.cmb_format.current(i)
            if tab.lbl_tab_status.cget("text"):
                self.lbl_status.configure(text=tab.lbl_tab_status.cget("text"))
            self._after_idle(tab, "fit_columns")
        self._update_copy_enabled()
        self._update_search_buttons()

    # -- compatibility accessors (tests / scripts) ---------------------
    @property
    def model(self):
        tab = self.current_tab()
        return tab.model if tab is not None else None

    @property
    def proxy_model(self):
        """Qt had a separate sort/filter proxy; Tk folds it into the model."""
        tab = self.current_tab()
        return tab.model if tab is not None else None

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
        """Run a search once the proxy is up (or after ``wait_for_proxy``)."""
        self.edit.set_value(query)
        self._pending_query = query
        self._pending_deadline = time.monotonic() + max(0.0, wait_for_proxy)
        if self.core is None or self.core.running:
            self.root.after(150, self._run_pending_search)
            return
        self.set_status(tr("等待代理就绪后自动搜索…"))
        self.root.after(1000, self._pending_tick)

    def _pending_tick(self) -> None:
        if not getattr(self, "_pending_query", ""):
            return
        if self.core is not None and self.core.running:
            self._run_pending_search()
            return
        if time.monotonic() >= getattr(self, "_pending_deadline", 0):
            self.set_status(tr("代理未就绪，仍以直连方式搜索"))
            self._run_pending_search()
            return
        self.root.after(1000, self._pending_tick)

    def _run_pending_search(self) -> None:
        q = getattr(self, "_pending_query", "")
        self._pending_query = ""
        if q:
            self.edit.set_value(q)
            self.start_search()

    def start_search(self) -> None:
        """Every search gets its own tab, so searches run in parallel."""
        query = self.edit.value().strip()
        if not query:
            self.set_status(tr("请输入关键词"))
            return

        sources = REGISTRY.enabled()
        if not sources:
            messagebox.showinfo(tr("没有数据源"),
                                tr("请在「数据源」菜单中至少启用一个数据源。"),
                                parent=self.root)
            return

        tab = self.current_tab()
        # reuse the tab only while it is still pristine (nothing typed, nothing
        # found); otherwise every search opens a fresh tab
        reusable = (tab is not None and tab.session is None
                    and len(tab.model.rows) == 0 and not tab.query)
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
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_search.configure(state="normal")

    # -- copy ----------------------------------------------------------
    def _update_copy_enabled(self) -> None:
        tab = self.current_tab()
        if tab is None:
            self.btn_copy.configure(state="disabled")
            self.btn_copy_all.configure(state="disabled")
            return
        has_selection = bool(tab.table.tree.selection())
        self.btn_copy.configure(state="normal" if has_selection else "disabled")
        self.btn_copy_all.configure(
            state="normal" if len(tab.model.rows) else "disabled")

    def _to_clipboard(self, text: str, what: str) -> None:
        if not text:
            self.set_status(tr("没有可复制的内容"))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        n = len([x for x in text.split("\r\n") if x])
        self.set_status(tr("已复制 %d 条%s链接到剪贴板") % (n, what))

    def copy_selected(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self._to_clipboard(tab.links_for(tab.selected_results()), tr("选中"))

    def copy_all(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        self._to_clipboard(tab.links_for(tab.results()), tr("全部"))

    def _select_all(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.select_all()

    def invert_selection(self) -> None:
        tab = self.current_tab()
        if tab is not None:
            tab.invert_selection()

    def reenrich_selected(self) -> None:
        tab = self.current_tab()
        if tab is None:
            return
        rows = [r for r in tab.selected_results() if r.kind == "magnet"]
        if not rows:
            self.set_status(tr("选中结果中没有磁力链接"))
            return
        tab.reenrich(rows)

    # -- format --------------------------------------------------------
    def _set_format(self, key: str) -> None:
        """Applies to every tab, so the copy format never depends on focus."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SearchTab):
                w.set_format(key)
        i = self._format_index(key)
        if self.cmb_format.current() != i:
            self.cmb_format.current(i)
        self.set_status(tr("链接格式: %s") % dict(
            (k, tr(l)) for l, k in LINK_FORMATS).get(key, key))

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
    def _populate_sources_menu(self) -> None:
        self.m_sources.delete(0, "end")
        self._source_vars = []
        for s in REGISTRY.all():
            var = tk.BooleanVar(value=s.enabled)
            self._source_vars.append((var, s))
            self.m_sources.add_checkbutton(
                label="%s  (%s)" % (tr(s.label), ", ".join(s.kinds)),
                variable=var,
                command=lambda src=s, v=var: self._toggle_source(src, v.get()))
        self.m_sources.add_separator()
        self.m_sources.add_command(label=tr("全部启用"),
                                   command=lambda: self._set_all_sources(True))
        self.m_sources.add_command(label=tr("全部禁用"),
                                   command=lambda: self._set_all_sources(False))

        self.m_proxy.delete(0, "end")
        for title, command in (
                ("启动代理", self._start_proxy),
                ("停止代理", self._stop_proxy),
                ("节点测速并切换最快", self._speedtest),
                ("重新获取订阅", self._refresh_subscription),
                ("设置订阅地址…", self._configure_subscription)):
            self.m_proxy.add_command(label=tr(title), command=command)
        self.m_proxy.add_separator()
        for title, command in (
                ("导入代理内容（粘贴文本）…", self._import_proxy_text),
                ("导入配置文件…", self._import_proxy_file),
                ("清空已导入的节点", self._clear_imported)):
            self.m_proxy.add_command(label=tr(title), command=command)
        self.m_proxy.add_separator()
        self.m_proxy.add_command(label=tr("打开数据目录"),
                                 command=self._open_data_dir)

    def _sources_dialog(self) -> None:
        """The 数据源 button: a checklist (Qt popped the menu instead)."""
        top = tk.Toplevel(self.root)
        top.title(tr("数据源"))
        top.transient(self.root)
        vars_ = []
        for s in REGISTRY.all():
            var = tk.BooleanVar(value=s.enabled)
            vars_.append((var, s))
            ttk.Checkbutton(top, text="%s  (%s)" % (tr(s.label),
                                                    ", ".join(s.kinds)),
                            variable=var).pack(anchor="w", padx=12, pady=2)

        def apply_and_close() -> None:
            for var, src in vars_:
                src.enabled = bool(var.get())
            self._save_settings()
            self._populate_sources_menu()
            top.destroy()

        ttk.Button(top, text=tr("确定"), command=apply_and_close).pack(pady=8)
        top.grab_set()

    def _toggle_source(self, src, on: bool) -> None:
        src.enabled = bool(on)
        self._save_settings()

    def _set_all_sources(self, on: bool) -> None:
        for s in REGISTRY.all():
            s.enabled = bool(on)
        self._populate_sources_menu()
        self._save_settings()

    # -- language ------------------------------------------------------
    def _set_language(self, value: str) -> None:
        old = i18n.language()
        i18n.set_language(value)
        # Record the choice even when it is unchanged: picking the default
        # explicitly must still persist it for the next start.
        self.settings['language'] = i18n.language()
        for code, var in self.language_actions.items():
            var.set(code == i18n.language())
        if old == i18n.language():
            self._save_settings()
            return
        self.root.title(tr(APP_TITLE))
        # Keep the existing tabs, workers, result rows and selections intact.
        for widget, source in self._tr_text:
            try:
                widget.configure(text=tr(source))
            except Exception:  # noqa: BLE001 - widget destroyed
                pass
        for entry, source in self._tr_placeholder:
            entry.set_placeholder(tr(source), remember=True)
        for widget, tip in self._tooltips.items():
            tip.set(tr(tip.source), remember=True)
        self.cmb_format.configure(
            values=[tr(label) for label, _k in LINK_FORMATS])
        self.cmb_format.current(
            self._format_index(self.settings.get("link_format", "auto")))
        self._build_menu()
        self._populate_sources_menu()
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if isinstance(tab, SearchTab):
                tab._sync_headings()
                tab._apply_title(tab._label())
                tab.show_details()
        self._save_settings()

    # -- proxy ---------------------------------------------------------
    def _configure_subscription(self) -> bool:
        urls = self.settings.get("subscriptions") or []
        result = {"value": None}

        top = tk.Toplevel(self.root)
        top.title(tr("设置代理订阅"))
        top.transient(self.root)
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text=tr("填写 HTTP/HTTPS 订阅地址（仅保存在本机）："),
                  wraplength=520, justify="left").grid(
                      row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        entry = tk.Entry(top, show="*", width=64)
        entry.grid(row=1, column=0, sticky="ew", padx=12)
        if urls:
            entry.insert(0, urls[0])
        entry.focus_set()

        buttons = ttk.Frame(top)
        buttons.grid(row=2, column=0, sticky="e", padx=12, pady=12)

        def accept() -> None:
            result["value"] = entry.get()
            top.destroy()

        def cancel() -> None:
            result["value"] = None
            top.destroy()

        ttk.Button(buttons, text=tr("取消"), command=cancel).pack(side="right")
        ttk.Button(buttons, text=tr("确定"), command=accept).pack(
            side="right", padx=(0, 6))
        top.bind("<Return>", lambda _e: accept())
        top.bind("<Escape>", lambda _e: cancel())
        top.grab_set()
        top.wait_window()

        if result["value"] is None:
            return False
        value = result["value"].strip()
        try:
            parts = urlsplit(value)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            valid = False
        if not valid:
            messagebox.showwarning(tr("订阅地址无效"),
                                   tr("请填写完整的 HTTP/HTTPS 订阅地址。"),
                                   parent=self.root)
            return False
        self.settings["subscriptions"] = [value]
        self._save_settings()
        if self.core is not None:
            self.core.subscriptions = [value]
        self.set_status(tr("订阅已保存，正在获取节点…"))
        if self.core is not None and self.core.running:
            self._refresh_subscription()
        else:
            self._start_proxy()
        return True

    def _start_proxy(self) -> None:
        if self.core is not None and self.core.running:
            self.lbl_proxy.configure(
                text=tr("代理: 已运行 · %s") % self.core.current_node)
            return
        urls = self.settings.get("subscriptions") or None
        self.core = MihomoCore(
            subscription_urls=urls,
            on_event=lambda kind, msg: self.post(
                lambda: self._on_proxy_event(kind, msg)))
        if not self.core.subscriptions and not self.core.load_user_nodes():
            self.lbl_proxy.configure(text=tr("代理: 未配置订阅"))
            if self._configure_subscription():
                return
            self.set_status(tr("可在「代理 → 设置订阅地址」配置；当前使用直连"))
            return
        self.lbl_proxy.configure(text=tr("代理: 启动中…"))

        def work():
            self.core.start()
        threading.Thread(target=work, daemon=True).start()

    def _stop_proxy(self) -> None:
        if self.core is not None:
            self.core.stop()
        self.lbl_proxy.configure(text=tr("代理: 已停止"))

    def _speedtest(self) -> None:
        if self.core is None or not self.core.running:
            self.set_status(tr("代理未运行，先启动代理"))
            return
        self.set_status(tr("节点测速中…"))

        def work():
            best = self.core.manual_sweep()
            self.post(lambda: self._on_proxy_event(
                "node", "%s|0|0" % best if best else "nomatch"))
        threading.Thread(target=work, daemon=True).start()

    def _refresh_subscription(self) -> None:
        if self.core is None or not self.core.running:
            self._start_proxy()
            return
        self.set_status(tr("正在重新获取订阅…"))

        def work():
            ok = self.core.refresh_subscription()
            self.post(lambda: self._on_proxy_event(
                "info" if ok else "warn",
                tr("订阅已更新 (%d 节点)") % len(self.core.proxies)
                if ok else tr("订阅获取失败")))
        threading.Thread(target=work, daemon=True).start()

    # -- proxy text import ---------------------------------------------
    def _import_proxy_text(self) -> None:
        from .import_dialog import ImportProxyDialog, parse_for_preview
        dlg = ImportProxyDialog(self, on_parse=parse_for_preview)
        if not dlg.show():
            return
        self._apply_imported(dlg.parsed)

    def _import_proxy_file(self) -> None:
        from .import_dialog import ImportProxyDialog, parse_for_preview
        dlg = ImportProxyDialog(self, on_parse=parse_for_preview)
        dlg._load_file()
        if not dlg.text().strip():
            dlg.destroy()
            return
        if not dlg.show():
            return
        self._apply_imported(dlg.parsed)

    def _apply_imported(self, proxies: list) -> None:
        if not proxies:
            return
        if self.core is None:
            self.core = MihomoCore(
                subscription_urls=self.settings.get("subscriptions") or None,
                on_event=lambda kind, msg: self.post(
                    lambda: self._on_proxy_event(kind, msg)))
        self.set_status(tr("正在校验并应用 %d 个导入节点…") % len(proxies))

        def work():
            try:
                self.core.apply_user_nodes(proxies)
                self.post(lambda: self._on_proxy_event(
                    "node", "%s|0|%d" % (
                        self.core.current_node or tr("导入节点"),
                        len(self.core.proxies))))
            except Exception as exc:  # noqa: BLE001
                self.post(lambda: self._on_proxy_event(
                    "error", tr("导入失败: %s") % exc))
        threading.Thread(target=work, daemon=True).start()

    def _clear_imported(self) -> None:
        if self.core is None:
            self.core = MihomoCore(
                subscription_urls=self.settings.get("subscriptions") or None,
                on_event=lambda kind, msg: self.post(
                    lambda: self._on_proxy_event(kind, msg)))
        self.core.clear_user_nodes()
        self.core._pushed_nodes = []
        self.set_status(tr("已清空手动导入的节点，下次刷新订阅后生效"))

    def _on_proxy_event(self, kind: str, msg: str) -> None:
        if kind != 'node':
            msg = i18n.translate_status(msg)
        if kind == "node":
            parts = msg.split("|")
            name = parts[0]
            try:
                delay = int(parts[1])
                total = int(parts[2])
            except (IndexError, ValueError):
                delay = total = 0
            if delay:
                self.lbl_proxy.configure(
                    text=tr("代理: 运行中 · %s · %dms · %d个可用")
                    % (name, delay, total))
            else:
                self.lbl_proxy.configure(
                    text=tr("代理: 运行中 · %s") % name)
            return
        if kind == "error":
            self.lbl_proxy.configure(text=tr("代理: 错误 · %s") % msg[:60])
        elif kind == "warn":
            self.lbl_proxy.configure(text=tr("代理: %s") % msg[:70])
        elif self.core is not None and self.core.running:
            self.lbl_proxy.configure(
                text=tr("代理: 运行中 · %s")
                % (self.core.current_node or tr("自动")))
        self.set_status(tr("代理: %s") % msg[:120])

    # -- misc ----------------------------------------------------------
    @staticmethod
    def _reveal(path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]  # Windows
        except Exception:  # noqa: BLE001 - non-Windows or no handler
            import subprocess
            try:
                subprocess.Popen(["xdg-open", path])
            except Exception:  # noqa: BLE001
                pass

    def _open_data_dir(self) -> None:
        self._reveal(data_dir())

    def _open_mihomo_log(self) -> None:
        path = os.path.join(data_dir(), "mihomo.log")
        if os.path.isfile(path):
            self._reveal(path)
        else:
            self.set_status(tr("暂无 mihomo 日志"))

    def _about(self) -> None:
        mode = tr("%s\n数据目录: %s") % (data_dir_label(), data_dir())
        # Qt rendered this as HTML; Tk has no rich text, so it is plain.
        messagebox.showinfo(
            tr("关于"),
            "\n\n".join([
                "TSearch-DS",
                tr("磁力 / eD2k(电驴) / 迅雷 链接聚合搜索器  from ds"),
                tr("结果列 名称 / 资源数 / 文件大小 / 文件类型 / 链接 / 来源；"
                   "表头可拖动换序、可调宽度；多选后按 Ctrl+C 复制（每行一个链接）。"),
                tr("每次搜索新开一个标签，可以并行搜索。"),
                tr("不建立搜索历史；查询会发送至所选站点，网络服务可能记录请求。"),
                tr("可选 mihomo 代理核心由用户自行从官方项目获取，使用用户提供的订阅、"
                   "测速选优，节点失效时自动切换到可用节点。"),
                tr("GPL-3.0-or-later，许可证见 COPYING。软件按现状提供，无担保，"
                   "以适用法律允许的范围为限。搜索结果不代表获得内容使用许可。"),
                mode,
            ]), parent=self.root)

    # -- lifecycle -----------------------------------------------------
    def mainloop(self) -> None:
        self._drain_loop()
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    def close(self) -> None:
        self._cancel_afters()
        self._shutdown()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    #: Qt overrides ``closeEvent``; Tk scripts call ``close()`` directly.
    closeEvent = close  # noqa: N815 - Qt spelling kept for call-site parity

    def _cancel_afters(self) -> None:
        """Drop pending ``after`` callbacks before the interpreter goes away.

        Tk prints a background "invalid command name" error for any ``after``
        script still queued when its root is destroyed; Qt silently discards
        queued signals, so this only exists to keep Tk quiet.
        """
        try:
            pending = self.root.tk.call("after", "info")
        except Exception:  # noqa: BLE001
            return
        for job in pending:
            try:
                self.root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass

    def _shutdown(self) -> None:
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


def tr_text(title: str) -> str:
    """Menu titles carry a Qt ``&`` accelerator; Tk has no use for it.

    ``"文件(&F)"`` -> ``"文件(F)"``: drop the marker but keep the letter, so
    the translated catalog key still matches the Qt one.
    """
    return tr(title).replace("&", "")
