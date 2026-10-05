# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""隐私回归：搜索关键词不许以任何形式离开程序 / 落到别处。

跑法： python tests\\test_privacy.py

覆盖两件事：

1. ``safe_page_url()`` —— 「在浏览器中打开页面」拿到的 URL 必须不带关键词。
   有些站点把结果页做成 ``/torrent-list/<关键词>/``，把这种链接交给浏览器，
   就等于把用户搜了什么写进浏览器历史。
2. iDope 这类源的 ``page`` 字段必须是**每个结果自己的详情页**，绝不能是
   带关键词的搜索页。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.core.links import safe_page_url  # noqa: E402

fails = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


print("== safe_page_url：关键词在路径里 -> 拒绝打开 ==")
check(safe_page_url("https://idope.se/torrent-list/%E7%81%AB%E5%BD%B1%E5%BF%8D%E8%80%85/",
                    "火影忍者") == "",
      "百分号编码的中文关键词（/torrent-list/火影忍者/）")
check(safe_page_url("https://idope.se/torrent-list/火影忍者/", "火影忍者") == "",
      "未编码的中文关键词")
check(safe_page_url("https://example.com/search/naruto/1080p", "naruto 1080p") == "",
      "空格分隔的关键词（naruto 1080p）")

print("\n== safe_page_url：关键词只在查询串/锚点里 -> 去掉那一段 ==")
check(safe_page_url("https://apibay.org/q.php?q=火影忍者&cat=0", "火影忍者")
      == "https://apibay.org/q.php", "查询串里的关键词被去掉")
check(safe_page_url("https://example.com/s?q=naruto", "naruto")
      == "https://example.com/s", "拉丁关键词同理")
check(safe_page_url("https://example.com/page#naruto", "naruto")
      == "https://example.com/page", "锚点里的关键词被去掉")

print("\n== safe_page_url：干净的详情页原样保留 ==")
for url in ("https://nyaa.si/view/1234567",
            "https://share.dmhy.org/topics/view/12345_xxx.html",
            "https://www.torrentdownloads.pro/torrent/abc-123",
            "https://idope.se/torrent/naruto-1080p-abc123/"):
    check(safe_page_url(url, "火影忍者") == url, "原样保留 %s" % url[:52])
check(safe_page_url("https://example.com/x?id=42&page=2", "火影忍者")
      == "https://example.com/x?id=42&page=2",
      "不含关键词的查询串也要保留（详情页参数不能丢）")
check(safe_page_url("https://example.com/x?q=other", "naruto")
      == "https://example.com/x?q=other", "别人的查询串与我无关")

print("\n== safe_page_url：不合法输入 ==")
check(safe_page_url("", "x") == "", "空字符串")
check(safe_page_url("javascript:alert(1)", "x") == "", "非 http(s) 协议")
check(safe_page_url("file:///C:/tmp/x.html", "x") == "", "file:// 也不放行")
check(safe_page_url("https://example.com/view/1", "") == "https://example.com/view/1",
      "没有关键词时照常返回")

print("\n== iDope 的 page 必须是详情页 ==")
from src.core.http import HTTP  # noqa: E402
from src.core.sources.torrents import Idope  # noqa: E402

FIXTURE = """
<html><body>
<div class="resultdiv">
  <a href="/torrent/naruto-1080p-abc123/">Naruto 1080p</a>
  <div class="resultdivtopname"><a href="magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&amp;dn=x">Naruto 1080p</a></div>
  <div class="resultdivbottontime">1 files</div>
  seeders 42
  1.40 GB
</div>
<div class="resultdiv">
  <div class="resultdivtopname"><a href="magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB&amp;dn=y">Naruto 720p</a></div>
  <div class="resultdivbottontime">2 files</div>
  seeders 7
  700 MB
</div>
</body></html>
"""

real = HTTP.get_text
HTTP.get_text = lambda *a, **k: FIXTURE          # type: ignore[assignment]
try:
    rows = list(Idope().search("火影忍者", limit=10))
finally:
    HTTP.get_text = real                          # type: ignore[assignment]

check(len(rows) == 2, "解析出 2 条", len(rows))
if rows:
    check(rows[0].extra.get("page") == "https://idope.se/torrent/naruto-1080p-abc123/",
          "有详情链接时 page = 详情页", rows[0].extra.get("page"))
    check("火影" not in str(rows[0].extra.get("page")),
          "page 里没有关键词")
if len(rows) > 1:
    check(rows[1].extra.get("page") == "",
          "没有详情链接时 page 留空（绝不用搜索页顶替）", repr(rows[1].extra.get("page")))
for r in rows:
    check("torrent-list" not in str(r.extra.get("page")),
          "page 不是 /torrent-list/ 搜索页", r.extra.get("page"))

print("\n== eD2k 网页索引：不带 page（它只有搜索 URL）==")
from src.core.sources.ed2k import _WebEd2kSource  # noqa: E402

spec = {"id": "webx", "label": "x", "url": "https://example.com/s?q={query}",
        "link_re": r"ed2k://\|file\|[^|]+\|\d+\|[0-9a-fA-F]{32}\|/"}
src = _WebEd2kSource(spec)
check(src is not None, "构造成功")
real_get = HTTP.get
HTTP.get = lambda *a, **k: type("R", (), {"status_code": 200, "content": (
    b'<a href="ed2k://|file|ubuntu.iso|1024|' + b"a" * 32 + b'|/">u</a>'),
    "text": "", "encoding": "utf-8"})()          # type: ignore[assignment]
try:
    rows2 = list(src.search("火影忍者", limit=5))
finally:
    HTTP.get = real_get                            # type: ignore[assignment]
check(len(rows2) == 1, "解析出 1 条", len(rows2))
if rows2:
    check(not rows2[0].extra.get("page"),
          "extra 里没有 page（否则会打开带关键词的搜索 URL）", rows2[0].extra)

print("\n%s" % ("ALL OK" if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
