# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Anonymous public Internet Archive torrents, with metadata access checks."""
import html
from urllib.parse import quote
from ..http import HTTP
from ..models import KIND_TORRENT, SearchResult
from ..parallel import pmap
from .base import Source, SourceError, register


@register
class InternetArchive(Source):
    id = 'internetarchive'
    label = 'Internet Archive（公开种子）'
    kinds = (KIND_TORRENT,)
    weight = 12
    _base_timeout = 8.0

    def search(self, query, limit=60):
        if not query.strip() or limit <= 0:
            return []
        terms = ['"' + word.replace('\\', '\\\\').replace('"', '\\"') + '"'
                 for word in query.split()]
        expression = '(' + ' AND '.join('title:' + term for term in terms) + ')'
        data = HTTP.get_json('https://archive.org/advancedsearch.php',
                             params={'q': expression + ' AND format:"Archive BitTorrent"',
                                     'fl[]': ['identifier', 'title'], 'output': 'json',
                                     'rows': min(limit, 20), 'page': 1},
                             timeout=self.request_timeout)
        if not isinstance(data, dict) or not isinstance(data.get('response'), dict):
            raise SourceError('公开搜索接口不可用')
        results = pmap(self._resolve, data['response'].get('docs', []),
                       workers=3, timeout=18.0)
        return [row for _, row, error in results if error is None and row is not None][:limit]

    def _resolve(self, item):
        identifier = item.get('identifier', '')
        if not isinstance(identifier, str) or not identifier:
            return None
        base = 'https://archive.org/'
        encoded = quote(identifier, safe='')
        data = HTTP.get_json(base + 'metadata/' + encoded, timeout=self.request_timeout)
        if not isinstance(data, dict):
            return None
        metadata = data.get('metadata', {})
        if data.get('is_dark') or metadata.get('access-restricted-item') in (True, 'true', '1'):
            return None
        for file in data.get('files', []):
            name = file.get('name', '')
            if file.get('private') in (True, 'true', '1') or not name.endswith('.torrent'):
                continue
            link = base + 'download/' + encoded + '/' + quote(name, safe='')
            return SearchResult(name=html.unescape(str(item.get('title') or identifier)),
                                link=link, source=self.id, kind=KIND_TORRENT,
                                extra={'page': base + 'details/' + encoded,
                                       'torrent_url': link})
        return None
