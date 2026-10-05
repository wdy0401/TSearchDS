# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Isolated offscreen tests for public first-run subscription setup."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5 import QtWidgets
from src.ui.main_window import MainWindow
from src.core.proxy.core import MihomoCore
from src.core.proxy.subscription import DEFAULT_SUBSCRIPTIONS

class SubscriptionSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data_patch = patch('src.ui.main_window.data_dir', return_value=self.directory.name)
        self.data_patch.start()
        self.window = MainWindow(start_proxy=False)

    def tearDown(self):
        self.window.close()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_no_embedded_default_or_empty_list_fallback(self):
        self.assertEqual(DEFAULT_SUBSCRIPTIONS, ())
        self.assertEqual(MihomoCore(workdir=self.directory.name, subscription_urls=[]).subscriptions, [])

    def test_public_cancel_does_not_start_or_fetch(self):
        with patch('src.ui.main_window.MihomoCore') as factory, \
             patch.object(self.window, '_configure_subscription', return_value=False):
            core = factory.return_value
            core.subscriptions = []
            core.load_user_nodes.return_value = []
            self.window._start_proxy()
            core.start.assert_not_called()
            core.refresh_subscription.assert_not_called()

    def test_subscription_saved_and_start_requested(self):
        with patch.object(QtWidgets.QInputDialog, 'getText', return_value=('https://example.com/sub', True)), \
             patch.object(self.window, '_start_proxy') as start:
            self.assertTrue(self.window._configure_subscription())
            start.assert_called_once()
        with open(self.window.settings_path, encoding='utf-8') as stream:
            self.assertEqual(json.load(stream)['subscriptions'], ['https://example.com/sub'])

    def test_personal_configuration_starts_without_prompt(self):
        self.window.settings['subscriptions'] = ['https://example.com/sub']
        with patch('src.ui.main_window.MihomoCore') as factory, \
             patch('src.ui.main_window.threading.Thread') as worker, \
             patch.object(self.window, '_configure_subscription') as prompt:
            factory.return_value.subscriptions = ['https://example.com/sub']
            self.window._start_proxy()
            self.assertEqual(factory.call_args.kwargs['subscription_urls'], ['https://example.com/sub'])
            worker.return_value.start.assert_called_once()
            prompt.assert_not_called()

    def test_invalid_or_cancelled_input_does_not_save(self):
        for value, accepted in [('file:///secret', True), ('https://[invalid', True), ('', False)]:
            with patch.object(QtWidgets.QInputDialog, 'getText', return_value=(value, accepted)), \
                 patch.object(QtWidgets.QMessageBox, 'warning'), \
                 patch.object(self.window, '_start_proxy') as start:
                self.assertFalse(self.window._configure_subscription())
                start.assert_not_called()
                self.assertNotIn('subscriptions', self.window.settings)

if __name__ == '__main__':
    unittest.main()
