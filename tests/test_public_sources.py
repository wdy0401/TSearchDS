# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Offline regressions for anonymous sources and access restrictions."""
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from src.core.sources.torrents import Rutor
from src.core.sources.archive import InternetArchive
from src.core.enrich import enrich, enrich_one
from src.core.models import KIND_TORRENT, SearchResult

ROW = '''<tr class="gai"><td>date</td><td><a href="//d.rutor.info/download/1">D</a>
<a href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=rutor.info">M</a>
<a href="/torrent/1/linux">Linux ISO</a></td><td align="right">1.50&nbsp;GB</td>
<td><span class="green"><img alt="S">&nbsp;0</span><span class="red">&nbsp;7</span></td></tr>'''

class PublicSourcesTests(unittest.TestCase):
    def test_rutor_download_first_and_counts_with_icons(self):
        rows = Rutor().parse(ROW)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.infohash, 'a' * 40)
        self.assertEqual(row.name, 'Linux ISO')
        self.assertEqual(row.peers, 7)
        self.assertEqual(row.seeds_display, '0')
        self.assertEqual(row.size, 1500000000)

    def test_rutor_rejects_missing_magnet_and_respects_limit(self):
        self.assertEqual(Rutor().parse('<tr><td>login page</td></tr>'), [])
        self.assertEqual(len(Rutor().parse(ROW * 3, 2)), 2)
        self.assertEqual(Rutor().parse(ROW, 0), [])

    def test_archive_public_file_and_unknown_size(self):
        with patch('src.core.sources.archive.HTTP.get_json', return_value={
                'metadata': {}, 'files': [{'name': 'linux_archive.torrent'}]}):
            row = InternetArchive()._resolve({'identifier': 'linux', 'title': 'Linux'})
        self.assertEqual(row.kind, 'torrent')
        self.assertEqual(row.size, 0)  # .torrent size is not the payload size
        self.assertEqual(row.extra['torrent_url'], row.link)
        self.assertTrue(row.link.endswith('/linux_archive.torrent'))

    def test_archive_skips_restricted_private_and_missing_files(self):
        for data in ({'is_dark': True},
                     {'metadata': {'access-restricted-item': 'true'}},
                     {'files': [{'name': 'x.torrent', 'private': 'true'}]},
                     {'files': [{'name': 'x.pdf'}]}):
            with self.subTest(data=data), patch('src.core.sources.archive.HTTP.get_json', return_value=data):
                self.assertIsNone(InternetArchive()._resolve({'identifier': 'x'}))

    def test_archive_query_and_limit(self):
        with patch('src.core.sources.archive.HTTP.get_json', return_value={
                'response': {'docs': []}}) as get:
            self.assertEqual(InternetArchive().search('Linux Mint', 80), [])
            params = get.call_args.kwargs['params']
            self.assertIn('title:"Linux" AND title:"Mint"', params['q'])
            self.assertEqual(params['rows'], 20)

    def test_torrent_results_enter_enrichment(self):
        row = SearchResult(name='Linux', link='https://archive.org/download/x/x.torrent',
                           kind=KIND_TORRENT)
        with patch('src.core.enrich.enrich_one', return_value=row) as resolve:
            enrich([row], workers=1, use_dht=False)
            resolve.assert_called_once()

    def test_torrent_is_converted_to_magnet_with_payload_size(self):
        row = SearchResult(name='Linux', link='https://archive.org/download/x/x.torrent',
                           kind=KIND_TORRENT)
        with patch('src.core.enrich.resolve_torrent', return_value=('a' * 40, [], 'Linux', 1234)), \
             patch('src.core.enrich.http_scrape', return_value=(2, 3)):
            enrich_one(row, use_dht=False)
        self.assertTrue(row.link.startswith('magnet:?'))
        self.assertEqual(row.kind, 'magnet')
        self.assertEqual(row.size, 1234)
        self.assertEqual(row.seeds, 2)

if __name__ == '__main__':
    unittest.main()
