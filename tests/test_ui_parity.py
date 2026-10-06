# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""The same behaviour spec, run against both UI backends.

Every scenario below is written once and executed twice -- once through
``src.ui`` (PySide6) and once through ``src.ui_tk`` (Tkinter) -- through the
small adapter in :class:`Backend`.  If a scenario has to be skipped on one
side, that *is* the answer to "how big is the difference": the skip reason is
reported in the run output and in ``docs/UI_FRAMEWORK_COMPARISON.md``.

Run:  python -m unittest tests.test_ui_parity
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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.models import SearchResult  # noqa: E402
from src.ui import i18n  # noqa: E402

try:
    from PySide6 import QtCore, QtWidgets  # noqa: E402
    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False

import tkinter as tk  # noqa: E402

from src.ui_tk.main_window import MainWindow as TkMainWindow  # noqa: E402
from src.ui_tk.result_model import ResultModel as TkResultModel  # noqa: E402
from src.ui_tk.search_tab import SearchTab as TkSearchTab  # noqa: E402


def row(name: str, seeds: int = 0, size: int = 0, source: str = "nyaa"):
    return SearchResult(name=name, link="magnet:?xt=urn:btih:" + "a" * 40,
                        source=source, seeds=seeds, size=size)


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

class Backend:
    """Uniform handle over one widget toolkit."""

    name = "?"
    #: (feature, supported?) differences discovered while porting
    notes: list = []

    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        self.windows: list = []

    # -- lifecycle -----------------------------------------------------
    def window(self, **kwargs):
        raise NotImplementedError

    def pump(self, _window) -> None:
        raise NotImplementedError

    def close_all(self) -> None:
        raise NotImplementedError

    # -- widgets -------------------------------------------------------
    def tab_count(self, window) -> int:
        return window.tabs.count()

    def current_tab(self, window):
        return window.current_tab()

    def new_tab(self, window, query: str = ""):
        return window.new_tab(query=query)

    def add_rows(self, tab, rows) -> None:
        raise NotImplementedError

    def total_rows(self, tab) -> int:
        raise NotImplementedError

    def visible_rows(self, tab) -> int:
        raise NotImplementedError

    def set_filter(self, tab, text: str) -> None:
        raise NotImplementedError

    def sort_by(self, tab, column: int, descending: bool) -> None:
        raise NotImplementedError

    def button_text(self, window, attr: str) -> str:
        raise NotImplementedError

    def set_language(self, window, code: str) -> None:
        window._set_language(code)

    def settings(self, window) -> dict:
        return json.loads(pathlib.Path(window.settings_path).read_text(
            encoding="utf-8"))


class QtBackend(Backend):
    name = "qt"

    def __init__(self, case) -> None:
        super().__init__(case)
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.patcher = patch("src.ui.main_window.data_dir",
                             return_value=case.temp.name)
        self.patcher.start()

    def window(self, **kwargs):
        from src.ui.main_window import MainWindow
        win = MainWindow(start_proxy=False, **kwargs)
        self.windows.append(win)
        return win

    def pump(self, _window) -> None:
        for _ in range(4):
            self.app.processEvents()

    def close_all(self) -> None:
        for win in self.windows:
            try:
                win.close()
            except Exception:  # noqa: BLE001
                pass
        self.windows = []
        self.patcher.stop()

    def add_rows(self, tab, rows) -> None:
        tab.model.add(rows)
        tab.model.refresh_all()

    def total_rows(self, tab) -> int:
        return tab.model.rowCount()

    def visible_rows(self, tab) -> int:
        return tab.proxy_model.rowCount()

    def set_filter(self, tab, text: str) -> None:
        tab.proxy_model.set_keyword_filter(text)

    def sort_by(self, tab, column: int, descending: bool) -> None:
        tab.table.sortByColumn(column, QtCore.Qt.SortOrder.DescendingOrder
                               if descending else QtCore.Qt.SortOrder.AscendingOrder)

    def button_text(self, window, attr: str) -> str:
        return getattr(window, attr).text()


class TkBackend(Backend):
    name = "tk"

    def __init__(self, case) -> None:
        super().__init__(case)
        self.patcher = patch("src.ui_tk.main_window.data_dir",
                             return_value=case.temp.name)
        self.patcher.start()

    def window(self, **kwargs):
        win = TkMainWindow(start_proxy=False, **kwargs)
        win.root.withdraw()
        self.windows.append(win)
        return win

    def pump(self, window) -> None:
        for _ in range(4):
            window.root.update()
        window.drain()

    def close_all(self) -> None:
        for win in self.windows:
            try:
                win.close()
            except Exception:  # noqa: BLE001
                pass
        self.windows = []
        self.patcher.stop()

    def add_rows(self, tab, rows) -> None:
        tab.model.add(rows)

    def total_rows(self, tab) -> int:
        return len(tab.model.rows)

    def visible_rows(self, tab) -> int:
        return len(tab.model.order)

    def set_filter(self, tab, text: str) -> None:
        tab.model.set_filter(text)

    def sort_by(self, tab, column: int, descending: bool) -> None:
        tab.model.set_sort(column, descending)

    def button_text(self, window, attr: str) -> str:
        return getattr(window, attr).cget("text")


BACKENDS = [TkBackend]
if HAVE_QT:
    BACKENDS.append(QtBackend)


# ---------------------------------------------------------------------------
# the shared spec
# ---------------------------------------------------------------------------

class ParityTests(unittest.TestCase):
    """Executed once per backend (``setUp`` installs the adapter)."""

    backend_class = None

    def setUp(self) -> None:
        if self.backend_class is None:
            self.skipTest("abstract base -- see TkParity / QtParity")
        self.temp = tempfile.TemporaryDirectory()
        self.backend = self.backend_class(self)      # type: ignore[operator]
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def tearDown(self) -> None:
        if self.backend_class is None:
            return
        self.backend.close_all()
        self.temp.cleanup()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    # -- scenarios -----------------------------------------------------
    def spec(self, b: Backend, win) -> None:
        tag = b.name

        # 1. one pristine tab at start-up
        self.assertEqual(b.tab_count(win), 1, tag)
        first = b.current_tab(win)
        self.assertIsNotNone(first, tag)

        # 2. every search gets its own tab, with its own store
        second = b.new_tab(win, "debian")
        self.assertEqual(b.tab_count(win), 2, tag)
        self.assertIsNot(first, second, tag)
        b.add_rows(first, [row("ubuntu desktop", seeds=5)])
        b.add_rows(second, [row("debian net", seeds=1),
                            row("debian iso", seeds=2)])
        b.pump(win)
        self.assertEqual(b.total_rows(first), 1, tag)
        self.assertEqual(b.total_rows(second), 2, tag)

        # 3. sorting
        b.sort_by(second, 1, True)          # 资源数 descending
        b.pump(win)
        names = [r.name for r in second.results()]
        self.assertEqual(names, ["debian iso", "debian net"], tag)

        # 4. filtering is per tab
        b.set_filter(second, "iso")
        b.pump(win)
        self.assertEqual(b.visible_rows(second), 1, tag)
        self.assertEqual(b.visible_rows(first), 1, tag)
        b.set_filter(second, "")
        b.pump(win)
        self.assertEqual(b.visible_rows(second), 2, tag)

        # 5. copy payload: one link per visible row
        links = second.links_for(second.results())
        self.assertEqual(len(links.splitlines()), 2, tag)
        self.assertTrue(all(x.startswith("magnet:") for x in
                            links.splitlines()), tag)

        # 6. link format applies to every tab
        win._set_format("thunder")
        b.pump(win)
        for i in range(b.tab_count(win)):
            self.assertEqual(b.tab_count(win) and
                             win.tabs.widget(i).model.fmt, "thunder", tag)
        self.assertTrue(second.links_for(second.results()).startswith(
            "thunder://"), tag)

        # 7. live language switch keeps rows and relabels the chrome
        before = b.total_rows(second)
        b.set_language(win, "en")
        b.pump(win)
        self.assertEqual(b.button_text(win, "btn_search"),
                         "Search (new tab)", tag)
        self.assertEqual(b.total_rows(second), before, tag)
        b.set_language(win, "zh_CN")
        b.pump(win)
        self.assertEqual(b.button_text(win, "btn_search"), "搜索（新标签）", tag)

        # 8. nothing search-related is persisted
        win._save_settings()
        settings = b.settings(win)
        for key in ("query", "last_query", "history", "recent", "search"):
            self.assertNotIn(key, settings, tag)
        self.assertNotIn("ubuntu desktop", json.dumps(settings), tag)

        # 9. closing every tab always leaves one behind
        while b.tab_count(win) > 1:
            win.close_tab(0)
            b.pump(win)
        win.close_tab(0)
        b.pump(win)
        self.assertEqual(b.tab_count(win), 1, tag)
        self.assertIsNotNone(b.current_tab(win), tag)

    def test_shared_spec(self) -> None:
        win = self.backend.window()
        self.spec(self.backend, win)


class TkParity(ParityTests):
    backend_class = TkBackend


if HAVE_QT:
    class QtParity(ParityTests):
        backend_class = QtBackend
else:  # pragma: no cover - only when PySide6 is absent
    class QtParity(unittest.TestCase):
        def test_skipped(self) -> None:
            self.skipTest("PySide6 is not installed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
