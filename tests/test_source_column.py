"""Source column regressions: merged origins, streaming updates, old layouts."""
import os
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5 import QtCore, QtWidgets
from src.core.aggregator import SearchSession
from src.core.models import SearchResult
from src.ui.result_model import ResultModel
from src.ui.search_tab import SearchTab

class SourceColumnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_six_columns_and_friendly_multiple_sources(self):
        model = ResultModel()
        model.add([SearchResult(name='Linux', link='magnet:?xt=urn:btih:'+'a'*40,
                                source='rutor', extra={'srcs':['rutor','internetarchive','rutor']})])
        self.assertEqual(model.columnCount(), 6)
        self.assertEqual(model.headerData(5, QtCore.Qt.Horizontal), '来源')
        self.assertEqual(model.data(model.index(0, 5)), 'RuTor、Internet Archive（公开种子）')

    def test_duplicate_origin_triggers_refresh_without_extra_row(self):
        session = SearchSession('Linux', sources=[], enrich_seeders=False)
        first = SearchResult(name='Linux', link='magnet:?xt=urn:btih:'+'a'*40,
                             infohash='a'*40, source='apibay')
        second = SearchResult(name='Linux', link=first.link, infohash='a'*40, source='rutor')
        model = ResultModel()
        model.add(session._add([first]))
        changes = []
        model.dataChanged.connect(lambda *args: changes.append(args))
        update = session._add([second])
        self.assertEqual(update, [first])
        self.assertEqual(model.add(update), 0)
        self.assertEqual(model.rowCount(), 1)
        self.assertTrue(changes)
        self.assertIn('RuTor', model.data(model.index(0, 5)))
        self.assertEqual(len(session.results), 1)

    def test_duplicate_in_one_batch_does_not_duplicate_row(self):
        session = SearchSession('Linux', sources=[], enrich_seeders=False)
        rows = [SearchResult(name='Linux', link='magnet:?xt=urn:btih:'+'a'*40,
                             infohash='a'*40, source=s) for s in ['apibay','rutor']]
        model = ResultModel()
        model.add(session._add(rows))
        self.assertEqual(model.rowCount(), 1)

    def test_missing_and_custom_origin(self):
        self.assertEqual(ResultModel.source_display(SearchResult('x','x')), '未知')
        self.assertEqual(ResultModel.source_display(SearchResult('x','x',source='custom-index')), 'custom-index')

    def test_old_layout_is_reset_to_show_source(self):
        class Owner(QtWidgets.QWidget):
            settings = {}
            def save_table_layout(self):
                pass
        owner = Owner()
        tab = SearchTab(owner)
        tab._auto_fit = False
        tab.apply_layout({'table_state':'old-five-column-layout'})
        self.assertTrue(tab._auto_fit)
        self.assertEqual(len(tab._COL_WEIGHTS), 6)
        tab.close()

if __name__ == '__main__':
    unittest.main()
