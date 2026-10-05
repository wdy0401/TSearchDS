# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Public-source bootstrap and diagnostic privacy regressions."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.core.http import _redact
from src.core.proxy.core import MihomoCore


class DistributionPolicyTests(unittest.TestCase):
    def test_missing_core_does_not_download(self):
        with patch('src.core.proxy.core.locate_binary', return_value=None), \
                patch('src.core.proxy.core.download_binary') as download:
            self.assertIsNone(MihomoCore(subscription_urls=[]).ensure_binary())
            download.assert_not_called()

    def test_url_credentials_and_path_are_redacted(self):
        result = _redact('https://user:secret@example.com/private-token/search-word?q=secret#secret')
        self.assertEqual(result, 'https://example.com/<已隐去>')

    def test_malformed_url_is_not_logged(self):
        self.assertEqual(_redact('https://[bad/token'), '<已隐去>')


if __name__ == '__main__':
    unittest.main()
