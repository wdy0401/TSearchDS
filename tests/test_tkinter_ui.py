# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Behavioural tests for the Tkinter backend (``src.ui_tk``).

The scenarios mirror ``tests/test_language.py`` and ``tests/test_tabs.py`` so
the two backends can be compared feature by feature.  Everything here is
offline: rows are injected straight into the result store instead of being
fetched from the network.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tkinter as tk  # noqa: E402

from src.core.models import SearchResult  # noqa: E402
from src.ui import i18n  # noqa: E402
from src.ui_tk.main_window import MainWindow  # noqa: E402
from src.ui_tk.result_model import ResultModel  # noqa: E402
from src.ui_tk.search_tab import SearchTab  # noqa: E402


def make_row(name: str, seeds: int = 0, size: int = 0,
             source: str = "nyaa") -> SearchResult:
    return SearchResult(name=name, link="magnet:?xt=urn:btih:" + "a" * 40,
                        source=source, seeds=seeds, size=size)


def pump(window, times: int = 3) -> None:
    """Let Tk run its idle callbacks (Qt tests use ``app.processEvents()``)."""
    for _ in range(times):
        try:
            window.root.update()
        except tk.TclError:  # window already destroyed
            return
    window.drain()


class TkMainWindowTests(unittest.TestCase):
    """One Tk root per window; every root is destroyed in ``tearDown``."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.patcher = patch("src.ui_tk.main_window.data_dir",
                             return_value=self.temp.name)
        self.patcher.start()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
        self.windows: list = []

    def build(self, **kwargs) -> MainWindow:
        window = MainWindow(start_proxy=False, **kwargs)
        window.root.withdraw()          # keep the test run invisible
        self.windows.append(window)
        return window

    def tearDown(self) -> None:
        for window in self.windows:
            try:
                window.close()
            except Exception:  # noqa: BLE001
                pass
        self.windows = []
        self.patcher.stop()
        self.temp.cleanup()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    # -- basics --------------------------------------------------------
    def test_english_is_the_default(self) -> None:
        window = self.build()
        self.assertEqual(i18n.language(), "en")
        self.assertEqual(window.btn_search.cget("text"), "Search (new tab)")

    def test_window_opens_with_one_empty_tab(self) -> None:
        window = self.build()
        self.assertEqual(window.tabs.count(), 1)
        self.assertIsInstance(window.current_tab(), SearchTab)
        self.assertEqual(len(window.current_tab().model.rows), 0)

    def test_new_tabs_are_independent(self) -> None:
        window = self.build()
        first = window.current_tab()
        second = window.new_tab(query="debian")
        self.assertEqual(window.tabs.count(), 2)
        self.assertIsNot(first, second)
        self.assertIsNot(first.model, second.model)
        first.model.add([make_row("ubuntu")])
        self.assertEqual(len(first.model.rows), 1)
        self.assertEqual(len(second.model.rows), 0)

    # -- the table -----------------------------------------------------
    def test_rows_are_rendered_and_sorted(self) -> None:
        window = self.build()
        tab = window.current_tab()
        tab.model.add([make_row("aaa", seeds=1), make_row("bbb", seeds=99),
                       make_row("ccc", seeds=50)])
        pump(window)
        self.assertEqual(len(tab.model.rows), 3)
        # default sort: 资源数 descending
        self.assertEqual([r.name for r in tab.results()], ["bbb", "ccc", "aaa"])
        tab.model.set_sort(ResultModel.COL_NAME, False)
        self.assertEqual([r.name for r in tab.results()], ["aaa", "bbb", "ccc"])
        # the Treeview really holds them, not just the model
        self.assertEqual(len(tab.table.tree.get_children()), 3)

    def test_filter_only_affects_its_own_tab(self) -> None:
        window = self.build()
        first = window.current_tab()
        second = window.new_tab(query="other")
        for tab in (first, second):
            tab.model.add([make_row("ubuntu desktop"), make_row("debian net")])
        pump(window)
        first.model.set_filter("ubuntu")
        self.assertEqual(len(first.results()), 1)
        self.assertEqual(len(second.results()), 2)

    def test_selection_round_trip(self) -> None:
        window = self.build()
        tab = window.current_tab()
        rows = [make_row("one"), make_row("two")]
        tab.model.add(rows)
        pump(window)
        keys = list(tab.table.tree.get_children())
        tab.table.tree.selection_set(keys[0])
        self.assertEqual(tab.selected_results(),
                         [tab.table.tree.item(keys[0], "values") and
                          tab.selected_results()[0]])
        self.assertEqual(len(tab.selected_results()), 1)

    def test_copy_selected_and_copy_all(self) -> None:
        window = self.build()
        tab = window.current_tab()
        tab.model.add([make_row("one"), make_row("two")])
        pump(window)
        keys = list(tab.table.tree.get_children())
        tab.table.tree.selection_set(keys[0])
        window.copy_selected()
        self.assertIn("Copied 1", window.lbl_status.cget("text"))
        window.copy_all()
        self.assertIn("Copied 2", window.lbl_status.cget("text"))

    def test_link_format_applies_to_every_tab(self) -> None:
        window = self.build()
        window.new_tab(query="second")
        window._set_format("thunder")
        for i in range(window.tabs.count()):
            tab = window.tabs.widget(i)
            self.assertEqual(tab.model.fmt, "thunder")
        self.assertEqual(window.cmb_format.current(), 3)

    def test_column_order_is_remembered(self) -> None:
        window = self.build()
        tab = window.current_tab()
        tab._move_column(0, 1)          # swap 名称 and 资源数
        state = tab.layout_state()
        self.assertEqual(state["table_order"][0], ResultModel.COL_SEEDS)
        other = window.new_tab()
        other.apply_layout(state)
        self.assertEqual(other.col_order, tab.col_order)

    # -- language ------------------------------------------------------
    def test_live_switch_preserves_data(self) -> None:
        window = self.build()
        tab = window.current_tab()
        row = make_row("中文 Ubuntu", seeds=3)
        tab.model.add([row])
        pump(window)
        keys = list(tab.table.tree.get_children())
        tab.table.tree.selection_set(keys[0])
        window.edit.set_value("我的关键词")

        window._set_language("en")
        self.assertIs(window.current_tab(), tab)
        self.assertIs(tab.model.rows[0], row)
        self.assertEqual(len(tab.selected_results()), 1)
        self.assertEqual(window.edit.value(), "我的关键词")
        self.assertEqual(window.btn_search.cget("text"), "Search (new tab)")

        window._set_language("zh_CN")
        self.assertEqual(window.btn_search.cget("text"), "搜索（新标签）")
        self.assertEqual(row.name, "中文 Ubuntu")

    def test_preference_is_persisted(self) -> None:
        window = self.build()
        window.edit.set_value("private query")
        window._set_language("en")
        settings = json.loads(
            pathlib.Path(window.settings_path).read_text(encoding="utf-8"))
        self.assertEqual(settings["language"], "en")
        self.assertNotIn("private query", json.dumps(settings))
        for key in ("query", "last_query", "history", "recent", "search"):
            self.assertNotIn(key, settings)

    def test_english_ui_has_no_untranslated_chinese(self) -> None:
        i18n.set_language("en")
        window = self.build()
        leaks = []

        def scan(widget) -> None:
            try:
                value = widget.cget("text")
            except Exception:  # noqa: BLE001 - option does not apply
                value = ""
            if isinstance(value, str) and any("一" <= ch <= "鿿" for ch in value):
                leaks.append("%s.text=%r" % (type(widget).__name__, value[:40]))
            for child in widget.winfo_children():
                scan(child)

        scan(window.root)
        for label in self._menu_labels(window._menubar, window.root):
            # the language picker stays bilingual on purpose
            if label in ("中文", "语言 / Language"):
                continue
            if any("一" <= ch <= "鿿" for ch in label):
                leaks.append("menu:%r" % label[:40])
        self.assertEqual(leaks, [])

    @staticmethod
    def _menu_labels(menu, root, out=None) -> list:
        out = [] if out is None else out
        if menu is None:
            return out
        try:
            last = menu.index("end")
        except Exception:  # noqa: BLE001
            return out
        if last is None:
            return out
        for i in range(last + 1):
            try:
                kind = menu.type(i)
            except Exception:  # noqa: BLE001
                continue
            if kind == "tearoff":
                continue
            try:
                out.append(str(menu.entrycget(i, "label")))
            except Exception:  # noqa: BLE001
                pass
            if kind == "cascade":
                try:
                    child = root.nametowidget(str(menu.entrycget(i, "menu")))
                except Exception:  # noqa: BLE001
                    continue
                TkMainWindowTests._menu_labels(child, root, out)
        return out

    # -- tabs / lifecycle ----------------------------------------------
    def test_close_tab_cancels_its_session(self) -> None:
        window = self.build()
        tab = window.new_tab(query="cancel-me")
        cancelled = {"flag": False}

        class _StubSession:
            running = True

            def cancel(self) -> None:
                cancelled["flag"] = True

        tab.session = _StubSession()
        index = window.tabs.indexOf(tab)
        window.close_tab(index)
        self.assertTrue(cancelled["flag"])
        self.assertEqual(window.tabs.indexOf(tab), -1)

    def test_closing_the_last_tab_opens_a_fresh_one(self) -> None:
        window = self.build()
        self.assertEqual(window.tabs.count(), 1)
        window.close_tab(0)
        self.assertEqual(window.tabs.count(), 1)
        self.assertIsInstance(window.current_tab(), SearchTab)


class TkResultModelTests(unittest.TestCase):
    """Pure-logic checks: no Tk root needed for the store itself."""

    def setUp(self) -> None:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def tearDown(self) -> None:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_duplicates_are_merged_not_appended(self) -> None:
        model = ResultModel()
        a = make_row("same swarm", seeds=1)
        b = make_row("same swarm", seeds=9)   # same dedup key
        self.assertEqual(model.add([a]), 1)
        self.assertEqual(model.add([b]), 0)
        self.assertEqual(len(model.rows), 1)
        self.assertEqual(model.rows[0].seeds, 9)

    def test_in_place_update_does_not_duplicate(self) -> None:
        model = ResultModel()
        row = make_row("growing", seeds=1)
        model.add([row])
        row.seeds = 42
        self.assertEqual(model.add([row]), 0)
        self.assertEqual(len(model.rows), 1)

    def test_sources_are_merged(self) -> None:
        model = ResultModel()
        a = make_row("x", source="nyaa")
        b = make_row("x", source="apibay")
        model.add([a])
        model.add([b])
        self.assertEqual(len(model.rows), 1)
        self.assertEqual(sorted(model.rows[0].extra.get("srcs") or []),
                         ["apibay", "nyaa"])

    def test_header_translation(self) -> None:
        model = ResultModel()
        i18n.set_language("en")
        self.assertEqual([__import__("src.ui.i18n", fromlist=["tr"]).tr(h)
                          for h in ResultModel.HEADERS][-1], "Source")
        i18n.set_language("zh_CN")
        self.assertEqual(ResultModel.HEADERS[-1], "来源")


if __name__ == "__main__":
    unittest.main()
