# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""BitTorrent / magnet index sources."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Iterable, List

from ..http import HTTP
from ..links import build_magnet, magnet_size, magnet_infohash
from ..models import KIND_MAGNET, SearchResult
from .base import Source, register
from .rssutil import parse_feed, parse_size, to_int

# ---------------------------------------------------------------------------
# JSON APIs
# ---------------------------------------------------------------------------


@register
class ApiBay(Source):
    """The Pirate Bay's public JSON endpoint (apibay.org)."""

    id = "apibay"
    label = "The Pirate Bay (apibay)"
    kinds = (KIND_MAGNET,)
    weight = 5
    needs_proxy = True

    URL = "https://apibay.org/q.php"
    URL_TOP = "https://apibay.org/precompiled/data_top100_recent.json"

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        data = None
        if not query.strip():
            data = HTTP.get_json(self.URL_TOP, timeout=self.request_timeout)
        else:
            for cat in ("0", "200", "300", "500", "600"):
                data = HTTP.get_json(self.URL, timeout=self.request_timeout,
                                     params={"q": query, "cat": cat})
                if data and not (isinstance(data, list) and len(data) == 1
                                 and str(data[0].get("name", "")).startswith("No results")):
                    break
        if not isinstance(data, list):
            return out
        for item in data:
            if not isinstance(item, dict):
                continue
            ih = str(item.get("info_hash") or "").strip()
            name = html.unescape(str(item.get("name") or "").strip())
            if not ih or not name or name.startswith("No results"):
                continue
            if set(ih) == {"0"}:
                continue
            out.append(SearchResult(
                name=name,
                link=build_magnet(ih, name, to_int(item.get("size"))),
                seeds=to_int(item.get("seeders")),
                peers=to_int(item.get("leechers")),
                source=self.id,
                kind=KIND_MAGNET,
                size=to_int(item.get("size")),
                infohash=ih.lower(),
                seeds_verified=True,
                extra={"category": item.get("category"), "added": item.get("added")},
            ))
            if len(out) >= limit:
                break
        return out


@register
class SolidTorrents(Source):
    """solidtorrents.to JSON API -- reliable seeders, broad catalogue."""

    id = "solidtorrents"
    label = "SolidTorrents"
    kinds = (KIND_MAGNET,)
    weight = 5
    #: Reachable only through the proxy (and often only via the curl fallback),
    #: so it needs headroom -- but measured runs showed it burning 18-26s and
    #: then returning nothing at all, which is worse than reporting no answer.
    #: The HTTP layer's own budget caps the whole transport chain at ~1.6x.
    _base_timeout = 12.0

    URL = "https://solidtorrents.to/api/v1/search"

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        if not query.strip():
            return out
        data = HTTP.get_json(self.URL, timeout=self.request_timeout,
                             params={"q": query, "sort": "seeders",
                                     "limit": min(100, max(20, limit))})
        if not isinstance(data, dict):
            return out
        for item in (data.get("results") or []):
            if not isinstance(item, dict):
                continue
            ih = str(item.get("infohash") or "").strip().lower()
            name = html.unescape(str(item.get("title") or "").strip())
            if not ih or not name:
                continue
            size = to_int(item.get("size"))
            out.append(SearchResult(
                name=name,
                link=build_magnet(ih, name, size),
                seeds=to_int(item.get("seeders")),
                peers=to_int(item.get("leechers")),
                source=self.id,
                kind=KIND_MAGNET,
                size=size,
                infohash=ih,
                seeds_verified=True,
                extra={"category": item.get("category")},
            ))
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# RSS feeds
# ---------------------------------------------------------------------------


class _RssTorrentSource(Source):
    """Shared implementation for RSS feeds whose items carry a magnet/infohash."""

    URL = ""
    param = "q"
    info_hash_key = "infohash"
    seeder_key = "seeders"
    size_key = "size"
    extra_params: dict = {}

    def build_url(self, query: str) -> str:
        sep = "&" if "?" in self.URL else "?"
        qs = {self.param: query}
        qs.update(self.extra_params)
        return self.URL + sep + urllib.parse.urlencode(qs)

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        text = HTTP.get_text(self.build_url(query), timeout=self.request_timeout)
        if not text:
            return out
        for item in parse_feed(text, max_items=limit * 2):
            name = item.title
            if not name:
                continue
            ih = (item.get(self.info_hash_key) or "").strip()
            magnet = ""
            for enc in item.enclosures:
                if enc.lower().startswith("magnet:"):
                    magnet = enc
                    break
                if enc.lower().endswith(".torrent") or "torrent" in enc.lower():
                    item.extra.setdefault("torrent_url", enc)
            if not magnet and ih:
                magnet = build_magnet(ih, name)
            turl = item.extra.get("torrent_url") or ""
            if magnet:
                link = magnet
            elif turl:
                # the feed only exposes a .torrent: hand that out directly and
                # let the enricher turn it into a magnet with a 资源数
                link = turl
            elif item.link:
                link = item.link
            else:
                continue
            seeds = to_int(item.get(self.seeder_key), 0) if self.seeder_key else 0
            size = parse_size(item.get(self.size_key)) if self.size_key else 0
            if not size and link.lower().startswith("magnet:"):
                # the magnet's xl= is an exact byte count, better than the
                # name heuristic models.py falls back to
                size = magnet_size(link)
            out.append(SearchResult(
                name=name,
                link=link,
                seeds=seeds,
                source=self.id,
                kind=KIND_MAGNET,
                size=size,
                infohash=ih.lower(),
                seeds_verified=bool(seeds),
                extra={"page": item.link,
                       "torrent_url": turl,
                       "needs_resolve": not magnet,
                       "pubdate": item.pubdate},
            ))
            if len(out) >= limit:
                break
        return out


@register
class Nyaa(_RssTorrentSource):
    id = "nyaa"
    label = "Nyaa"
    URL = "https://nyaa.si/"
    param = "q"
    info_hash_key = "infohash"
    seeder_key = "seeders"
    size_key = "size"

    def build_url(self, query: str) -> str:
        return "https://nyaa.si/?" + urllib.parse.urlencode(
            {"page": "rss", "q": query, "c": "0_0", "f": "0"})


@register
class Sukebei(_RssTorrentSource):
    id = "sukebei"
    label = "Sukebei (Nyaa adult)"
    URL = "https://sukebei.nyaa.si/"
    default_enabled = False

    def build_url(self, query: str) -> str:
        return "https://sukebei.nyaa.si/?" + urllib.parse.urlencode(
            {"page": "rss", "q": query, "c": "0_0", "f": "0"})


@register
class Dmhy(_RssTorrentSource):
    """動漫花園 -- RSS carries a fully-populated magnet per item."""

    id = "dmhy"
    label = "動漫花園 (dmhy)"
    URL = "https://share.dmhy.org/topics/rss/rss.xml"
    param = "keyword"
    info_hash_key = ""
    seeder_key = ""
    size_key = ""


@register
class AcgRip(_RssTorrentSource):
    """acg.rip -- RSS exposes a .torrent enclosure (needs enrichment)."""

    id = "acgrip"
    label = "ACG.RIP"
    URL = "https://acg.rip/.xml"
    param = "term"
    info_hash_key = ""
    seeder_key = ""
    size_key = "contentlength"


@register
class Mikan(_RssTorrentSource):
    """蜜柑计划 Mikan Project -- .torrent enclosure (needs enrichment)."""

    id = "mikan"
    label = "蜜柑计划 (Mikan)"
    URL = "https://mikanani.me/RSS/Search"
    param = "searchstr"
    info_hash_key = ""
    seeder_key = ""
    size_key = "contentlength"

    def build_url(self, query: str) -> str:
        alt = "https://mikanime.tv/RSS/Search?" + urllib.parse.urlencode(
            {"searchstr": query})
        return alt


@register
class BangumiMoe(_RssTorrentSource):
    """bangumi.moe -- anime torrent index with a magnet in the feed."""

    id = "bangumi"
    label = "Bangumi Moe"
    URL = "https://bangumi.moe/api/torrent/search"
    default_enabled = False

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        payload = json.dumps({"search": query}).encode("utf-8")
        r = HTTP.get(self.URL, timeout=self.request_timeout, method="POST",
                     data=payload, headers={"Content-Type": "application/json"})
        # HTTP.get only issues GET; fall back to a direct session call
        if r is None:
            try:
                sess = HTTP.session()
                r = sess.post(self.URL, data=payload, timeout=self.request_timeout,
                              headers={"Content-Type": "application/json"})
            except Exception:
                return out
        try:
            data = r.json()
        except Exception:
            return out
        for item in (data.get("torrents") or []):
            ih = str(item.get("infoHash") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            if not ih or not title:
                continue
            outs = item.get("size") or 0
            m = item.get("magnet") or build_magnet(ih, title, to_int(outs))
            out.append(SearchResult(
                name=title, link=m, seeds=0, source=self.id, kind=KIND_MAGNET,
                size=to_int(outs), infohash=ih,
                extra={"page": item.get("page_url", "")}))
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# HTML indexes
# ---------------------------------------------------------------------------


@register
class Idope(Source):
    """idope.se -- server-rendered list with a real seeder count."""

    id = "idope"
    label = "iDope"
    kinds = (KIND_MAGNET,)
    weight = 12

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        safe = urllib.parse.quote(query.strip().replace("/", " "))
        if not safe:
            return out
        url = "https://idope.se/torrent-list/%s/" % safe
        text = HTTP.get_text(url, timeout=self.request_timeout,
                             headers={"Referer": "https://idope.se/"})
        if not text:
            return out
        blocks = re.split(r'<div class="resultdiv">', text)[1:]
        for blk in blocks:
            m = re.search(r'href="(magnet:\?[^"]+)"', blk)
            if not m:
                m = re.search(r'(magnet:\?xt=urn:btih:[0-9a-zA-Z]+[^"\'<\s]*)', blk)
            if not m:
                continue
            magnet = html.unescape(m.group(1))
            ih = ""
            mi = re.search(r"btih:([0-9a-zA-Z]{32,40})", magnet)
            if mi:
                ih = mi.group(1).lower()
            tm = re.search(r'class="resultdivtopname"[^>]*>(.*?)</div>', blk, re.S)
            name = ""
            if tm:
                name = re.sub(r"<[^>]+>", "", tm.group(1))
            if not name:
                nm = re.search(r"<a[^>]*>([^<]{4,200})</a>", blk)
                name = nm.group(1) if nm else ""
            name = html.unescape(name).strip()
            if not name:
                continue
            sm = re.search(r"class=\"resultdivbottontime\"[^>]*>(.*?)</div>", blk, re.S)
            seeds = 0
            fm = re.search(r"(\d[\d,]*)\s*(?:个)?\s*(?:files|文件)", blk)
            sd = re.search(r"seeders?[^\d]{0,40}([\d,]+)", blk, re.I)
            if sd:
                seeds = to_int(sd.group(1))
            size = 0
            zm = re.search(r"([\d.]+\s*[KMGT]i?B)", blk, re.I)
            if zm:
                size = parse_size(zm.group(1))
            # The per-result detail link, never ``url``: that one is the *search*
            # URL (``/torrent-list/<关键词>/``), and handing it to a browser puts
            # the user's keyword into the browser's history.
            dm = re.search(r'href="(/torrent/[^"]+)"', blk)
            page = "https://idope.se" + html.unescape(dm.group(1)) if dm else ""
            out.append(SearchResult(
                name=name, link=magnet, seeds=seeds, source=self.id,
                kind=KIND_MAGNET, size=size, infohash=ih,
                seeds_verified=bool(seeds), extra={"page": page}))
            if len(out) >= limit:
                break
        return out


@register
class Rutor(Source):
    """rutor.info -- Russian tracker mirror, table based, has seeder counts."""

    id = "rutor"
    label = "RuTor"
    kinds = (KIND_MAGNET,)
    weight = 18
    default_enabled = True

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        q = query.strip()
        if not q:
            return out
        # rutor expects the query inside the path, spaces url-encoded as %20
        url = "https://rutor.info/search/0/0/000/0/" + urllib.parse.quote(q)
        text = HTTP.get_text(url, timeout=self.request_timeout)
        if not text:
            return out
        return self.parse(text, limit)

    def parse(self, text: str, limit: int = 60) -> List[SearchResult]:
        out: List[SearchResult] = []
        if limit <= 0:
            return out
        for blk in re.findall(r'<tr\b[^>]*>.*?</tr>', text, re.S | re.I):
            links = re.findall(r'href="(/torrent/\d+/[^"]*)"[^>]*>([^<]{3,300})</a>',
                               blk, re.S)
            if not links:
                continue
            href, name = links[0]
            name = html.unescape(re.sub(r"\s+", " ", name)).strip()
            detail = "https://rutor.info" + href
            # mtor link carries the infohash
            mm = re.search(r'href="(magnet:\?[^"]+)"', blk)
            if not mm:
                continue
            magnet = html.unescape(mm.group(1))
            ih = magnet_infohash(magnet)
            if not ih:
                continue
            def count(colour):
                span = re.search(r'<span\b[^>]*class="' + colour + r'"[^>]*>(.*?)</span>', blk, re.S | re.I)
                value = html.unescape(re.sub(r'<[^>]+>', '', span.group(1))) if span else ''
                return to_int(value.strip()), bool(re.fullmatch(r'\d+', value.strip()))
            seeds, verified = count('green')
            peers, _ = count('red')
            plain = html.unescape(re.sub(r'<[^>]+>', ' ', blk))
            zm = re.search(r'([\d.,]+\s*[KMGT]i?B)\b', plain, re.I)
            size = parse_size(zm.group(1)) if zm else 0
            out.append(SearchResult(
                name=name, link=magnet, seeds=seeds, peers=peers, source=self.id,
                kind=KIND_MAGNET, size=size, infohash=ih,
                seeds_verified=verified, extra={"page": detail}))
            if len(out) >= limit:
                break
        return out


@register
class TorrentDownloads(Source):
    """torrentdownloads.pro -- HTML list; seeder data is only a health icon."""

    id = "torrentdownloads"
    label = "TorrentDownloads"
    kinds = (KIND_MAGNET,)
    weight = 20
    default_enabled = False

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        q = query.strip()
        if not q:
            return out
        url = "https://www.torrentdownloads.pro/search/?search=" + urllib.parse.quote(q)
        text = HTTP.get_text(url, timeout=self.request_timeout)
        if not text:
            return out
        for blk in re.findall(r'<div class="grey_bar3[^"]*">(.*?)</div>\s*</div>',
                              text, re.S)[:limit * 2]:
            m = re.search(r'href="(/torrent/[^"]+)"[^>]*>([^<]{3,300})</a>', blk)
            if not m:
                m = re.search(r'href="([^"]*?(?:torrent|download)[^"]*)"[^>]*>([^<]{3,300})</a>',
                              blk, re.I)
            if not m:
                continue
            href, name = m.group(1), html.unescape(m.group(2)).strip()
            if not name or "sponsor" in href.lower():
                continue
            page = href if href.startswith("http") else "https://www.torrentdownloads.pro" + href
            hm = re.search(r'health_img(\d)', blk)
            seeds = (int(hm.group(1)) * 25) if hm else 0
            zm = re.search(r"<span>([\d.]+\s*[KMGT]?B)</span>", blk, re.I)
            size = parse_size(zm.group(1)) if zm else 0
            out.append(SearchResult(
                name=name, link=page, seeds=seeds, source=self.id,
                kind=KIND_MAGNET, size=size,
                seeds_verified=False, extra={"page": page, "needs_resolve": True}))
            if len(out) >= limit:
                break
        return out
