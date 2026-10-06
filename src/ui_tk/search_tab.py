# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""One search per tab (Tk backend).

Everything a single query needs lives here: its own result store, its own
table (with its own column layout), its own filter box, its own detail pane and
its own :class:`SearchSession`.  That is what allows a second search to start
while the first one is still waiting for the slower indexes -- the window keeps
one of these per tab and they never share mutable state.

Worker threads never touch a widget directly: they hand a callable to
``owner.post()``, which Tk runs on the GUI thread (the equivalent of Qt
queueing a signal across threads).
"""
from __future__ import annotations

from ..ui.i18n import tr
from ..ui.constants import LINK_FORMATS

import logging
import time
import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from ..core.aggregator import SearchSession
from ..core.links import link_to_ed2k, link_to_magnet, link_to_thunder, safe_page_url
from ..core.models import SearchResult
from .result_model import ResultModel
from .widgets import PlaceholderEntry, ScrolledTree, monospace_font

log = logging.getLogger("tsearch.ui.tab")

#: internal Treeview column keys -- fixed; the visual order is the
#: ``displaycolumns`` option, which is what the user rearranges
COL_KEYS = ("name", "seeds", "size", "type", "link", "source")

#: foreground by 资源数 tier (Tk colours whole rows, not single cells)
_SEED_TAGS = ((50, "seed_hi", "#1a7f37"),
              (1, "seed_mid", "#9a6700"),
              (0, "seed_low", "#8b949e"))


class SearchTab(ttk.Frame):
    """Results of exactly one keyword search."""

    #: default width allocation (名称 资源数 大小 类型 链接 来源)
    _COL_WEIGHTS = (0.30, 0.075, 0.095, 0.085, 0.32, 0.125)

    def __init__(self, owner, query: str = "") -> None:
        super().__init__(owner.tabs.nb)
        self.owner = owner
        self.query = (query or "").strip()
        self.session: Optional[SearchSession] = None
        self.started_at = 0.0
        #: column widths are allocated automatically until the user drags one
        self._auto_fit = True
        self._fitting = False
        #: rows waiting to be applied (see :meth:`_on_results`)
        self._pending: List[SearchResult] = []
        self._flush_job = None
        #: "index" while the sources are still answering, then "enrich"
        self._phase = ""
        #: Treeview item id -> SearchResult
        self._items: dict = {}
        self._seq = 0
        #: visual column order, as logical column indexes
        self.col_order = list(range(len(ResultModel.HEADERS)))

        self.model = ResultModel(owner.settings.get("link_format", "auto"),
                                 on_change=self.refresh_table)
        self._build()
        self.model.set_sort(ResultModel.COL_SEEDS, True)

    def destroy(self) -> None:
        """Qt deletes the C++ half with the Python half; Tk needs telling."""
        self._cancel_flush()
        super().destroy()

    # -- construction --------------------------------------------------
    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        ttk.Label(bar, text=tr("过滤:")).pack(side="left")
        self.filter_edit = PlaceholderEntry(
            bar, placeholder=tr("在本标签的结果里过滤…"), width=30)
        self.filter_edit.pack(side="left", padx=(4, 0))
        self.filter_edit.enable_clear_button()
        self.filter_edit.bind("<KeyRelease>", self._on_filter_key)
        self.lbl_tab_status = ttk.Label(bar, text="", foreground="#57606a")
        self.lbl_tab_status.pack(side="right")

        self.table = ScrolledTree(self, columns=COL_KEYS,
                                  on_widths_changed=self._on_column_resized)
        self.table.grid(row=1, column=0, sticky="nsew", padx=8)
        self._configure_tags()

        self.details = tk.Text(self, height=6, wrap="none", relief="solid",
                               borderwidth=1, font=monospace_font(self, 9))
        self.details.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 8))
        self._set_details(None)

        tree = self.table.tree
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", lambda _e: self.show_details())
        tree.bind("<Button-3>", self._on_right_click)
        self.bind("<Configure>", self._on_configure)

    def _configure_tags(self) -> None:
        tree = self.table.tree
        tree.tag_configure("evenrow", background="#f6f8fa")
        for _threshold, tag, colour in _SEED_TAGS:
            tree.tag_configure(tag, foreground=colour)

    def _on_filter_key(self, _event=None) -> None:
        self.model.set_filter(self.filter_edit.value())

    def _on_configure(self, _event=None) -> None:
        self.fit_columns()

    def _on_select(self, _event=None) -> None:
        self.owner._update_copy_enabled()
        self.show_details()

    def _on_column_resized(self, _widths) -> None:
        # fires for our own fit_columns() too; the flag tells them apart
        if self._fitting:
            return
        if self._auto_fit:
            self._auto_fit = False
            self.owner.save_table_layout()

    # -- rendering -----------------------------------------------------
    def refresh_table(self) -> None:
        """Redraw every row from :attr:`model.order` (GUI thread only)."""
        tree = self.table.tree
        keep = {id(r) for iid, r in self._items.items()
                if iid in set(tree.selection())}
        tree.delete(*tree.get_children())
        self._items = {}
        for position, r in enumerate(self.model.order):
            iid = "r%d" % self._seq
            self._seq += 1
            values = [self.model.cell(r, c)
                      for c in range(len(ResultModel.HEADERS))]
            tags = ["evenrow"] if position % 2 else []
            for threshold, tag, _colour in _SEED_TAGS:
                if r.seeds >= threshold:
                    tags.append(tag)
                    break
            tree.insert("", "end", iid=iid, values=values, tags=tags)
            self._items[iid] = r
        if keep:
            tree.selection_set([i for i, r in self._items.items()
                                if id(r) in keep])
        self._sync_headings()

    def _sync_headings(self) -> None:
        tree = self.table.tree
        display = [COL_KEYS[c] for c in self.col_order]
        try:
            tree.configure(displaycolumns=display)
        except Exception:  # noqa: BLE001
            pass
        for position, logical in enumerate(self.col_order):
            key = COL_KEYS[logical]
            mark = ""
            if logical == self.model.sort_col:
                mark = " ▾" if self.model.sort_desc else " ▴"
            tree.heading(key, text=tr(ResultModel.HEADERS[logical]) + mark,
                         command=lambda c=logical: self._sort_by(c))
            anchor = ("e" if logical in (ResultModel.COL_SEEDS,
                                         ResultModel.COL_SIZE)
                      else "center" if logical == ResultModel.COL_TYPE
                      else "w")
            tree.column(key, anchor=anchor,
                        stretch=logical == ResultModel.COL_NAME)

    def _sort_by(self, col: int) -> None:
        self.model.set_sort(col)

    # -- search lifecycle ----------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.session is not None and self.session.running)

    def start(self, query: str, sources) -> None:
        self.query = (query or "").strip()
        self.cancel()
        self._cancel_flush()
        self._pending = []
        self.model.clear()
        self._set_details(None)
        self.filter_edit.clear_value()
        self.model.set_filter("")
        self.started_at = time.monotonic()
        self._phase = "index"
        self._apply_title(self._label())
        self.lbl_tab_status.configure(
            text=tr("搜索中… (%d 个数据源)") % len(sources))
        post = self.owner.post
        self.session = SearchSession(
            self.query, sources=sources,
            on_result=lambda rows: post(lambda: self._on_results(rows)),
            on_status=lambda sid, msg: post(lambda: self._on_status(sid, msg)),
            on_done=lambda rows: post(lambda: self._on_done("")),
            enrich_seeders=True)
        self.session.start()
        self.owner.set_status(tr("「%s」搜索中… (%d 个数据源)")
                              % (self.query, len(sources)))

    def cancel(self) -> None:
        if self.session is not None:
            try:
                self.session.cancel()
                # Cancelling is cooperative: the worker may be blocked on a
                # socket for another few seconds.  Drop its callbacks so it
                # cannot emit into a tab that has been closed.
                self.session.on_result = lambda rows: None
                self.session.on_status = lambda sid, msg: None
                self.session.on_done = lambda rows: None
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self.cancel()
        self.lbl_tab_status.configure(text=tr("已停止"))
        self._apply_title(self._label())
        self.owner.set_status(tr("已停止"))

    # -- callbacks (GUI thread) ----------------------------------------
    def _cancel_flush(self) -> None:
        if self._flush_job is not None:
            try:
                self.after_cancel(self._flush_job)
            except Exception:  # noqa: BLE001
                pass
            self._flush_job = None

    def _on_results(self, rows: list) -> None:
        """Queue incoming rows; the table is updated in bursts.

        Enrichment delivers 资源数 one row at a time and each insertion made
        the table re-sort and repaint.  With two searches running that was
        enough work to make the *second* tab look stuck, so batches are
        coalesced into one update.
        """
        self._pending.extend(rows)
        if self._flush_job is None:
            self._flush_job = self.after(120, self._flush_results)

    def _flush_results(self) -> None:
        self._flush_job = None
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
        if not added:
            # nothing new -> these are in-place 资源数 updates from the enricher
            for r in rows:
                self.model.refresh_row(r)
            self.model.refresh_all()
        self.lbl_tab_status.configure(text=tr("%d 条 · %.0fs")
                                      % (len(self.model.rows),
                                         time.monotonic() - self.started_at))
        self._apply_title(self._label())
        if self.owner.current_tab() is self:
            self.owner._update_copy_enabled()

    def _on_status(self, sid: str, msg: str) -> None:
        from ..core.sources import REGISTRY
        from ..ui import i18n
        raw_message = msg
        msg = i18n.translate_status(msg)
        if sid == "enrich":
            # make it visible on the tab itself: a background tab that is
            # filling in 资源数 otherwise looks finished-but-empty for a minute
            self._phase = "" if "完成" in raw_message else "enrich"
            text = tr("补充资源数… %s") % msg
            self._apply_title(self._label())
        else:
            s = REGISTRY.get(sid)
            text = "%s: %s" % (tr(s.label) if s else sid, msg)
        self.lbl_tab_status.configure(text=text)
        if self.owner.current_tab() is self:
            self.owner.set_status(text)

    def _on_done(self, _msg: str) -> None:
        """Fired once when the index phase ends and again when enrichment ends."""
        self._cancel_flush()
        self._flush_results()
        self.model.refresh_all()
        try:
            summary = self.session.summary() if self.session is not None else ""
        except Exception:  # noqa: BLE001
            summary = ""
        if summary:
            from ..ui import i18n
            summary = i18n.translate_status(summary)
            self.lbl_tab_status.configure(text=summary)
        else:
            self.lbl_tab_status.configure(
                text=tr("%d 条") % len(self.model.rows))
        self._phase = ""
        self._apply_title(self._label())
        if self.owner.current_tab() is self:
            self.owner._update_copy_enabled()
            if summary:
                self.owner.set_status(summary)

    # -- title ---------------------------------------------------------
    def _label(self) -> str:
        text = self.query or tr("(空)")
        n = len(self.model.rows)
        if self._phase == "enrich":
            return tr("%s (%d) 补资源数…") % (text, n)
        if self.running:
            return "%s …" % text
        if n:
            return "%s (%d)" % (text, n)
        return text

    def _apply_title(self, text: str) -> None:
        i = self.owner.tabs.indexOf(self)
        if i >= 0:
            self.owner.tabs.setTabText(i, text)
            self.owner.tabs.setTabToolTip(
                i, self.lbl_tab_status.cget("text") or text)

    # -- results access ------------------------------------------------
    def results(self) -> List[SearchResult]:
        """Rows in the order the user currently sees them."""
        return self.model.results()

    def selected_results(self) -> List[SearchResult]:
        out = []
        for iid in self.table.tree.selection():
            r = self._items.get(iid)
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

    def select_all(self) -> None:
        self.table.tree.selection_set(tuple(self._items.keys()))

    def invert_selection(self) -> None:
        sel = set(self.table.tree.selection())
        self.table.tree.selection_set(
            tuple(i for i in self._items if i not in sel))

    def set_format(self, key: str) -> None:
        self.model.set_format(key)

    # -- context menu / details ---------------------------------------
    def _on_right_click(self, event) -> None:
        tree = self.table.tree
        if tree.identify_region(event.x, event.y) == "heading":
            self._heading_menu(event)
            return
        if not self._items:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=tr("复制选中链接"),
                         command=self.owner.copy_selected)
        menu.add_command(label=tr("复制全部结果链接"),
                         command=self.owner.copy_all)
        menu.add_separator()
        menu.add_command(label=tr("复制名称"),
                         command=lambda: self.owner._to_clipboard(
                             "\r\n".join(r.name for r in self.selected_results()),
                             tr("名称")))
        menu.add_command(label=tr("重新获取资源数"),
                         command=self.owner.reenrich_selected)
        menu.add_separator()
        menu.add_command(label=tr("在浏览器中打开页面"),
                         command=self.open_page)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _heading_menu(self, event) -> None:
        """Qt let the user drag a header to reorder columns; Tk cannot."""
        tree = self.table.tree
        column = tree.identify_column(event.x)  # '#1'.. in *display* order
        try:
            position = int(str(column).lstrip("#")) - 1
        except (TypeError, ValueError):
            return
        if not (0 <= position < len(self.col_order)):
            return
        menu = tk.Menu(self, tearoff=0)
        logical = self.col_order[position]
        if position > 0:
            menu.add_command(
                label=tr("左移「%s」") % tr(ResultModel.HEADERS[logical]),
                command=lambda: self._move_column(position, -1))
        if position < len(self.col_order) - 1:
            menu.add_command(
                label=tr("右移「%s」") % tr(ResultModel.HEADERS[logical]),
                command=lambda: self._move_column(position, 1))
        menu.add_separator()
        menu.add_command(label=tr("按窗口宽度分配列宽"),
                         command=self.owner._fit_columns)
        menu.add_command(label=tr("重置当前标签的列布局"),
                         command=self.owner._reset_columns)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _move_column(self, position: int, delta: int) -> None:
        other = position + delta
        (self.col_order[position],
         self.col_order[other]) = (self.col_order[other],
                                   self.col_order[position])
        self._sync_headings()
        self.owner.save_table_layout()

    def _set_details(self, text: Optional[str]) -> None:
        if text is None:
            text = tr("选中一行查看详情")
            colour = "#8b949e"
        else:
            colour = "#24292f"
        self.details.configure(state="normal", foreground=colour)
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def show_details(self) -> None:
        rows = self.selected_results()
        if not rows:
            self._set_details(None)
            return
        r = rows[0]
        srcs = ", ".join(s for s in (r.extra.get("srcs") or [r.source]) if s)
        info = [
            tr("名称: %s") % r.name,
            tr("资源数: %s%s") % (r.seeds_display,
                            "" if r.seeds_verified else tr("  (未确认)")),
            tr("文件大小: %s%s") % (r.size_display,
                              tr("  (取自文件名，仅供参考)")
                              if r.extra.get("size_from_name") else ""),
            tr("文件类型: %s") % tr(r.type_display),
            tr("来源: %s") % srcs,
        ]
        if r.peers:
            info.append(tr("下载中(leechers): %d") % r.peers)
        if r.infohash:
            info.append("infohash: %s" % r.infohash)
        if r.ed2k_hash:
            info.append("ed2k hash: %s" % r.ed2k_hash.upper())
        info.append("")
        info.append(tr("原始: %s") % r.link)
        for label, key in LINK_FORMATS[1:]:
            conv = {"magnet": link_to_magnet, "ed2k": link_to_ed2k}.get(key)
            val = link_to_thunder(r.link) if key == "thunder" else (
                conv(r.link, r.name, r.size) if conv else "")
            info.append("%s: %s" % (tr(label), val or tr("（无法转换）")))
        self._set_details("\n".join(info))

    def open_page(self) -> None:
        import webbrowser
        rows = self.selected_results()
        if not rows:
            return
        raw = str(rows[0].extra.get("page") or "")
        if not raw:
            self.owner.set_status(tr("该结果没有可打开的页面"))
            return
        # never hand a keyword-bearing URL to the browser: the query string is
        # stripped, and a URL whose *path* contains the search term is refused
        page = safe_page_url(raw, self.query)
        if not page:
            self.owner.set_status(
                tr("该结果只有带关键词的搜索页，不打开（避免关键词进入浏览器历史）"))
            return
        webbrowser.open(page)

    def reenrich(self, rows: List[SearchResult]) -> None:
        """Kick off a background 资源数 refresh for ``rows`` of this tab."""
        import threading
        post = self.owner.post

        def done(r: SearchResult) -> None:
            post(lambda: self._on_results([r]))

        def work() -> None:
            from ..core.enrich import enrich
            try:
                enrich(rows, workers=6, use_dht=True, on_done=done)
            finally:
                post(lambda: self._on_done(""))

        threading.Thread(target=work, daemon=True).start()
        self.lbl_tab_status.configure(
            text=tr("正在重新获取资源数 (%d)…") % len(rows))

    # -- column layout -------------------------------------------------
    def fit_columns(self) -> None:
        """Distribute the available width over the columns (default layout)."""
        if not self._auto_fit:
            return
        total = self.table.tree.winfo_width() - 20
        if total < 200:
            return
        self._fitting = True
        try:
            for logical, weight in enumerate(self._COL_WEIGHTS):
                self.table.tree.column(COL_KEYS[logical],
                                       width=max(48, int(total * weight)))
            self.table.set_last_widths(
                {c: self.table.tree.column(c, "width") for c in COL_KEYS})
        finally:
            self._fitting = False

    def layout_state(self) -> dict:
        tree = self.table.tree
        return {
            "table_columns": len(ResultModel.HEADERS),
            "table_order": list(self.col_order),
            "table_widths": {c: tree.column(c, "width") for c in COL_KEYS},
            "table_autofit": bool(self._auto_fit),
            "table_sort": [self.model.sort_col, bool(self.model.sort_desc)],
        }

    def apply_layout(self, state: Optional[dict]) -> None:
        """Restore a saved layout, or inherit the current tab's."""
        if not state:
            return
        if state.get("table_columns") != len(ResultModel.HEADERS):
            # Old five-column layouts must not hide the newly added source.
            self._auto_fit = True
            return
        order = state.get("table_order")
        if isinstance(order, list) and len(order) == len(self.col_order):
            self.col_order = [int(c) for c in order]
        widths = state.get("table_widths")
        if isinstance(widths, dict) and widths:
            for key, width in widths.items():
                try:
                    self.table.tree.column(key, width=int(width))
                except Exception:  # noqa: BLE001 - unknown column name
                    continue
            self.table.set_last_widths(
                {c: self.table.tree.column(c, "width") for c in COL_KEYS})
            self._auto_fit = bool(state.get("table_autofit", False))
        sort = state.get("table_sort")
        if isinstance(sort, list) and len(sort) == 2:
            try:
                self.model.set_sort(int(sort[0]), bool(sort[1]))
            except Exception:  # noqa: BLE001
                pass
        self._sync_headings()

    def reset_columns(self) -> None:
        self._auto_fit = True
        self.col_order = list(range(len(ResultModel.HEADERS)))
        self.model.set_sort(ResultModel.COL_SEEDS, True)
        self._sync_headings()
        self.fit_columns()
        self.owner.save_table_layout()
        self.owner.set_status(
            tr("列布局已重置为默认（名称/资源数/大小/类型/链接/来源）"))
