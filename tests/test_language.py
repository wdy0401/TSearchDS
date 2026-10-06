# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
import ast
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from PySide6 import QtCore, QtWidgets
from src.ui import i18n
from src.ui.main_window import MainWindow
from src.core.models import SearchResult


class LanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=patch('src.ui.main_window.data_dir', return_value=self.temp.name)
        self.directory.start()
        i18n.set_language('zh_CN')

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        self.directory.stop()
        self.temp.cleanup()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_english_is_the_default(self):
        self.assertEqual(i18n.DEFAULT_LANGUAGE, 'en')
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
        self.assertEqual(i18n.language(), 'en')
        self.assertEqual(i18n.tr('搜索（新标签）'), 'Search (new tab)')
        i18n.set_language('zh_CN')
        self.assertEqual(i18n.tr('搜索（新标签）'), '搜索（新标签）')
        # the reverse table restores English text back to Chinese
        self.assertEqual(i18n.tr('Search (new tab)'), '搜索（新标签）')

    def test_default_interface_has_no_untranslated_chinese(self):
        """With English selected no shipped control may still read Chinese."""
        i18n.set_language('en')
        window = MainWindow(start_proxy=False)
        leaks = []
        for widget in [window] + list(window.findChildren(QtWidgets.QWidget)):
            for getter in ('text', 'placeholderText', 'toolTip', 'windowTitle'):
                method = getattr(widget, getter, None)
                if method is None:
                    continue
                try:
                    value = method()
                except Exception:
                    continue
                if value and any('一' <= ch <= '鿿' for ch in value):
                    leaks.append('%s.%s=%r' % (type(widget).__name__, getter, value[:40]))
        # The language menu is deliberately bilingual: a language picker shows
        # each language in its own name ("中文" / "English"), which is the
        # established convention and must not be translated.
        exempt = {id(a) for a in window.language_actions.values()}
        for action in window.menuBar().actions():
            entries = ([action] + action.menu().actions()) if action.menu() else [action]
            for sub in entries:
                if id(sub) in exempt or sub.text() == '语言 / Language':
                    continue
                if sub.text() and any('一' <= ch <= '鿿' for ch in sub.text()):
                    leaks.append('menu:%r' % sub.text()[:40])
        self.assertEqual(leaks, [])

    def test_live_switch_preserves_data(self):
        window=MainWindow(start_proxy=False)
        tab=window.current_tab()
        row=SearchResult(name='中文 Ubuntu',link='magnet:?xt=urn:btih:'+'a'*40,source='nyaa')
        tab.model.add([row])
        tab.table.selectRow(0)
        window.edit.setText('我的关键词')
        window._set_language('en')
        self.assertIs(window.current_tab(),tab)
        self.assertIs(tab.model.rows[0],row)
        self.assertEqual(tab.selected_results(),[row])
        self.assertEqual(window.edit.text(),'我的关键词')
        self.assertEqual(window.btn_search.text(),'Search (new tab)')
        self.assertEqual(tab.model.headerData(5,QtCore.Qt.Orientation.Horizontal),'Source')
        self.assertEqual(tab.details.placeholderText(),'Select a row to see details')
        window._set_language('zh_CN')
        self.assertEqual(window.btn_search.text(),'搜索（新标签）')
        self.assertEqual(tab.model.headerData(5,QtCore.Qt.Orientation.Horizontal),'来源')
        self.assertEqual(row.name,'中文 Ubuntu')

    def test_preference_restores_without_query(self):
        window=MainWindow(start_proxy=False)
        window.edit.setText('private query')
        window._set_language('en')
        settings=json.loads(pathlib.Path(window.settings_path).read_text(encoding='utf-8'))
        self.assertEqual(settings['language'],'en')
        self.assertNotIn('private query',json.dumps(settings))
        again=MainWindow(start_proxy=False)
        self.assertEqual(again.btn_search.text(),'Search (new tab)')

    def test_dynamic_status_round_trip(self):
        i18n.set_language('invalid')
        self.assertEqual(i18n.language(), i18n.DEFAULT_LANGUAGE)
        i18n.set_language('en')
        self.assertEqual(i18n.retranslate('搜索中… (4 个数据源)','zh_CN'),'Searching… (4 sources)')
        summary='8 条结果 · 2/3 个源有响应 · 1.2s（40s 上限已到，慢的源按无结果计）'
        english=i18n.translate_status(summary)
        self.assertNotIn('结果',english)
        i18n.set_language('zh_CN')
        self.assertEqual(i18n.retranslate(english,'en'),summary)

    def test_catalog_covers_literals(self):
        root=pathlib.Path(__file__).resolve().parents[1]/'src/ui'
        for name in ('main_window.py','search_tab.py','import_dialog.py','result_model.py'):
            tree=ast.parse((root/name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='tr' and node.args and isinstance(node.args[0],ast.Constant):
                    self.assertIn(node.args[0].value,i18n.CATALOG,(name,node.args[0].value))


if __name__=='__main__':
    unittest.main()
