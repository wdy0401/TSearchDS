"""超时必须有硬上限的回归测试（不联网，几秒跑完）。

跑法： python tests\\test_timeouts.py

「搜索」这件事原本没有阶段级别的上限：某个源不回应就一路等下去，
补资源数更是能拖到两分钟。这里用假源（sleep）验证：

* 索引阶段超过 ``index_budget`` 就必须收工，并把慢源记为无结果；
* 补资源数超过 ``enrich_budget`` 就必须停，剩下的行保持 ``-``；
* 单次请求的兜底链也有总预算（``http.HTTP.get``），不会 4 段超时叠加。
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.core import aggregator as A  # noqa: E402
from src.core import enrich as E  # noqa: E402
from src.core.models import KIND_MAGNET, SearchResult  # noqa: E402
from src.core.sources.base import Source  # noqa: E402

fails = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


class SlowSource(Source):
    """A source that never answers in time."""

    id = "slow"
    label = "Slow"
    kinds = (KIND_MAGNET,)

    def __init__(self, delay: float = 30.0, rows: int = 1) -> None:
        super().__init__()
        self.delay = delay
        self.rows = rows
        self.entered = threading.Event()

    def search(self, query, limit=60):
        self.entered.set()
        time.sleep(self.delay)
        return [SearchResult(name="late-%d" % i, link="magnet:?xt=urn:btih:" + "a" * 40)
                for i in range(self.rows)]


class FastSource(Source):
    id = "fast"
    label = "Fast"
    kinds = (KIND_MAGNET,)

    def search(self, query, limit=60):
        time.sleep(0.2)
        # the name must contain the query token, otherwise the relevance
        # filter (correctly) drops the row and the test proves nothing
        return [SearchResult(name="ok-result", link="magnet:?xt=urn:btih:" + "b" * 40,
                             seeds=5, kind=KIND_MAGNET)]


print("== 默认预算存在 ==")
s = A.SearchSession("x", sources=[])
check(s.index_budget == 40.0, "索引阶段默认 40s 上限", s.index_budget)
check(s.enrich_budget == 20.0, "补资源数默认 20s 上限", s.enrich_budget)

print("\n== 索引阶段：慢源不能把搜索拖住 ==")
slow = SlowSource(delay=30.0)
fast = FastSource()
sess = A.SearchSession("ok", sources=[slow, fast], enrich_seeders=False,
                       index_budget=1.5)
done = []
sess.on_done = lambda rows: done.append(list(rows))
t0 = time.monotonic()
sess.start()
sess.join(6.0)
dt = time.monotonic() - t0
check(dt < 3.0, "1.5s 上限下约 1.5-2s 收工", "%.2fs" % dt)
check(not sess.running, "会话已经结束")
check(sess.timed_out is True, "标记了「超时截断」")
check(any(r.name == "ok-result" for r in sess.results), "快的源的结果照样拿到了",
      [r.name for r in sess.results])
check(not any(r.name.startswith("late") for r in sess.results),
      "慢源的结果没有被等进来")
check("上限已到" in sess.summary(), "摘要里说明了截断", sess.summary())
check(len(done) >= 1, "on_done 仍然被调用（界面不会一直转）", len(done))

print("\n== 补资源数：到点就停 ==")
rows = [SearchResult(name="r%d" % i, link="magnet:?xt=urn:btih:" + "c" * 40,
                     kind=KIND_MAGNET) for i in range(12)]
seen = []
real_one = E.enrich_one


def fake_one(result, use_dht=True, timeout=10.0):
    time.sleep(5.0)                      # 每行都比总预算长
    result.seeds = 1
    return result


E.enrich_one = fake_one                  # type: ignore[assignment]
try:
    t0 = time.monotonic()
    E.enrich(rows, workers=4, use_dht=False, on_done=lambda r: seen.append(r),
             deadline=time.monotonic() + 1.5)
    dt = time.monotonic() - t0
finally:
    E.enrich_one = real_one              # type: ignore[assignment]
check(dt < 3.0, "20s/1.5s 预算下按时返回", "%.2fs" % dt)
check(len(seen) < len(rows), "没有把所有行都跑完（真的被预算截断）",
      "%d/%d" % (len(seen), len(rows)))
check(all(r.seeds == 0 or r.seeds == 1 for r in rows), "没跑到的行保持原样")

print("\n== 没有 deadline 时行为不变 ==")
rows2 = [SearchResult(name="q", link="magnet:?xt=urn:btih:" + "d" * 40,
                      kind=KIND_MAGNET)]
got = []
E.enrich(rows2, workers=1, use_dht=False, on_done=lambda r: got.append(r))
check(len(got) == 1, "不传 deadline 就照常跑完", len(got))

print("\n== HTTP 兜底链有总预算 ==")
from src.core.http import HTTP  # noqa: E402

calls = []


class _Boom:
    def get(self, *a, **k):
        calls.append(("requests", k.get("timeout")))
        raise OSError("nope")


real_session, real_curl = HTTP._session, HTTP.curl_get
HTTP._session = lambda *a, **k: _Boom()            # type: ignore[assignment]
HTTP.curl_get = lambda *a, **k: calls.append(("curl", k.get("timeout")))  # type: ignore[assignment]
HTTP.set_proxy({"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"})
try:
    t0 = time.monotonic()
    HTTP.get("https://example.com/x", timeout=0.4, budget=0.5)
    dt = time.monotonic() - t0
finally:
    HTTP._session, HTTP.curl_get = real_session, real_curl  # type: ignore[assignment]
    HTTP.set_proxy(None)
check(dt < 1.0, "budget=0.5 时整条链约 0.5s 内结束", "%.2fs" % dt)
check(len(calls) <= 2, "预算用完后不再尝试后面的 transport", calls)
if calls:
    check(all(t <= 0.6 for _k, t in calls), "每段尝试都受预算约束", calls)

print("\n%s" % ("ALL OK" if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
