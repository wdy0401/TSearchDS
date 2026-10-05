# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""出口网闸的回归测试（不联网，几秒跑完）。

跑法： python tests\\test_gate.py

这个程序以前在**错误的层**上限流：限「同时在查几个源」和「同时在补几行
资源数」。可是一行补资源数会扇出十几个 tracker 请求，所以那些上限根本没有
限住流量 —— 实测一个搜索就有 96 个请求同时在路上，两个搜索 144 个。

网闸现在挂在真正的出口上（``http.py`` 的每次发送，以及 UDP tracker 的
socket）。这里逐条钉住它的不变量：

* 在途请求数有硬上限；
* 后台（补资源数）不能把前台（正在查的索引）挤掉；
* 但没人等的时候后台可以用满 —— 保留槽不该白留；
* 先排队的先走（信号量会让刚到的插队）；
* 同一个站点最多这么多个并发；
* UDP tracker 也走同一个闸（它不走 http.py，以前完全没人管）；
* 阶段截止时间 / 取消事件能传到网络层：没人等了就不再发，而不是发完再丢；
* 两道门（全局 + 单站点）共用一个截止，排队不会把等待翻倍。
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.core import gateway as G  # noqa: E402
from src.core.parallel import pmap  # noqa: E402

fails = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


def quiet() -> None:
    """Let abandoned threads finish and give the gate back its slots."""
    for _ in range(150):
        if G.stats()[0] <= 0:
            break
        time.sleep(0.05)
    with G._NET._cv:
        G._NET._used = 0
    G._HOSTS.clear()


# -- 1. 硬上限 ----------------------------------------------------------
print()
print("== 1. 在途请求有硬上限 ==")
quiet()
peak = [0]
live = [0]
lock = threading.Lock()


def hold(url: str, dur: float = 0.30) -> None:
    with G.slot(10.0, url) as h:
        if h is None:
            return
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            time.sleep(dur)
        finally:
            with lock:
                live[0] -= 1


ths = [threading.Thread(target=hold, args=("https://x%d.example/q" % i,))
       for i in range(60)]
for t in ths:
    t.start()
for t in ths:
    t.join(30)
check(peak[0] <= G.CAP, "60 个并发请求也不超过上限",
      "峰值 %d / 上限 %d" % (peak[0], G.CAP))
check(peak[0] > 1, "确实是并发跑的（不是被串行化了）", "峰值 %d" % peak[0])

# -- 2. 后台不能挤掉前台 ------------------------------------------------
print()
print("== 2. 后台占满时，前台照样能发 ==")
quiet()
n_low = [0]
stop = threading.Event()


def background() -> None:
    while not stop.is_set():
        with G.slot(5.0, "https://bg.example/q") as h:
            if h is None:
                return
            n_low[0] += 1
            time.sleep(0.15)


for _ in range(G.CAP + 6):
    threading.Thread(target=background, daemon=True).start()
time.sleep(0.6)
check(G.stats()[0] > 0, "后台确实占住了槽", G.stats())

t0 = time.monotonic()
got = []
with G.scope(high=True):
    with G.slot(5.0, "https://fg.example/q") as h:
        got.append(h is not None)
        if h is not None:
            time.sleep(0.05)
waited = time.monotonic() - t0
stop.set()
check(got and got[0], "前台没有被后台堵死", "等到槽 %s" % got)
check(waited < 1.0, "前台几乎没等", "%.2fs" % waited)

# -- 3. 没前台时后台可以用满（保留槽不白留） ---------------------------
print()
print("== 3. 没人等的时候后台可以用满 ==")
quiet()
live2 = [0]
peak2 = [0]
lk2 = threading.Lock()


def bg_only(dur: float, i: int = 0) -> None:
    # distinct hosts: otherwise this measures PER_HOST, not the global cap
    with G.scope(high=False):
        with G.slot(10.0, "https://bg2-%d.example/q" % i) as h:
            if h is None:
                return
            with lk2:
                live2[0] += 1
                peak2[0] = max(peak2[0], live2[0])
            try:
                time.sleep(dur)
            finally:
                with lk2:
                    live2[0] -= 1


ths = [threading.Thread(target=bg_only, args=(0.35, i)) for i in range(40)]
for t in ths:
    t.start()
for t in ths:
    t.join(30)
check(peak2[0] > G.LOW_CAP, "没有前台在等时，后台能超过 LOW_CAP",
      "峰值 %d（LOW_CAP=%d，CAP=%d）" % (peak2[0], G.LOW_CAP, G.CAP))
check(peak2[0] <= G.CAP, "但不会超过 CAP", peak2[0])

# -- 4. 先到先得（信号量会让刚到的插队） -------------------------------
print()
print("== 4. 先排队的先走 ==")
quiet()
gate = G.Gate(1)
order = []
gate_holder = []


def take(name: str, delay: float) -> None:
    time.sleep(delay)
    if gate.acquire_until(time.monotonic() + 10, True):
        order.append(name)
        time.sleep(0.2)
        gate.release()


a = threading.Thread(target=take, args=("first", 0.0))
b = threading.Thread(target=take, args=("second", 0.05))
a.start()
b.start()
a.join(15)
b.join(15)
check(order == ["first", "second"], "先排队的先拿到槽", order)

# -- 5. 单站点上限 ------------------------------------------------------
print()
print("== 5. 同一个站点最多这么多个并发 ==")
quiet()
per = [0]
peak_per = [0]
lk3 = threading.Lock()


def same_host() -> None:
    with G.slot(10.0, "https://same.example/q") as h:
        if h is None:
            return
        with lk3:
            per[0] += 1
            peak_per[0] = max(peak_per[0], per[0])
        try:
            time.sleep(0.30)
        finally:
            with lk3:
                per[0] -= 1


ths = [threading.Thread(target=same_host) for _ in range(12)]
for t in ths:
    t.start()
for t in ths:
    t.join(30)
check(peak_per[0] <= G.PER_HOST, "单站点并发不超上限",
      "峰值 %d / 上限 %d" % (peak_per[0], G.PER_HOST))

# -- 6. UDP tracker 也走同一个闸 ----------------------------------------
print()
print("== 6. UDP tracker 也进闸（它不走 http.py） ==")
quiet()
check(G.host_of("udp://tracker.opentrackr.org:1337/announce")
      == "tracker.opentrackr.org:1337", "UDP URL 能解析出站点",
      G.host_of("udp://tracker.opentrackr.org:1337/announce"))
udp_peak = [0]
udp_live = [0]
lk4 = threading.Lock()


def udp_like() -> None:
    with G.slot(10.0, "udp://tracker.opentrackr.org:1337/announce") as h:
        if h is None:
            return
        with lk4:
            udp_live[0] += 1
            udp_peak[0] = max(udp_peak[0], udp_live[0])
        try:
            time.sleep(0.30)
        finally:
            with lk4:
                udp_live[0] -= 1


ths = [threading.Thread(target=udp_like) for _ in range(12)]
for t in ths:
    t.start()
for t in ths:
    t.join(30)
check(udp_peak[0] <= G.PER_HOST, "UDP 也受单站点上限约束",
      "峰值 %d / 上限 %d" % (udp_peak[0], G.PER_HOST))

# -- 7. 预算 / 取消能传到网络层 ----------------------------------------
print()
print("== 7. 没人等了就不发包 ==")
quiet()
sent = [0]


def try_send() -> None:
    h = G.acquire(5.0, "https://deadline.example/q")
    if h is not None:
        sent[0] += 1
        G.release(h)


try_send()
check(sent[0] == 1, "默认（前台、无截止）照常发", sent[0])

sent[0] = 0
with G.scope(high=True, deadline=time.monotonic() - 1.0):
    try_send()
check(sent[0] == 0, "阶段已过截止时间 -> 一个包都不发", sent[0])

sent[0] = 0
ev = threading.Event()
ev.set()
with G.scope(high=True, stop=ev):
    try_send()
check(sent[0] == 0, "搜索已取消 -> 一个包都不发", sent[0])

sent[0] = 0
with G.scope(high=True, deadline=time.monotonic() + 5.0):
    try_send()
check(sent[0] == 1, "阶段还在进行 -> 不受影响", sent[0])

# -- 8. 线程池继承上下文 ------------------------------------------------
print()
print("== 8. pmap 的工作线程继承调用者的上下文 ==")
quiet()
inherited = {}


def probe(i: int):
    ctx = G.current()
    inherited[i] = (ctx.high, ctx.deadline > 0,
                    ctx.stop.is_set() if ctx.stop else False)
    return i


ev2 = threading.Event()
ev2.set()
with G.scope(high=False, deadline=time.monotonic() + 30.0, stop=ev2):
    pmap(probe, [1, 2, 3], workers=3, timeout=10)
vals = list(inherited.values())
check(all(v[0] is False for v in vals) and len(vals) == 3,
      "后台标记传到了子线程", vals)
check(all(v[1] for v in vals), "截止时间传到了子线程", vals)
check(all(v[2] for v in vals), "取消事件传到了子线程", vals)
check(G.current().high is True, "出了作用域恢复成前台默认", G.current().high)

# -- 9. 两道门共用一个截止，排队不会翻倍 --------------------------------
print()
print("== 9. 排队不会把等待翻倍 ==")
quiet()
gate_a = G.Gate(1)
held = threading.Event()
blocker_done = threading.Event()


def blocker() -> None:
    gate_a.acquire_until(time.monotonic() + 3.0, True)
    held.set()
    time.sleep(1.5)
    gate_a.release()
    blocker_done.set()


threading.Thread(target=blocker, daemon=True).start()
held.wait(5)
t0 = time.monotonic()
ok = gate_a.acquire_until(time.monotonic() + 1.0, True)
waited2 = time.monotonic() - t0
if ok:
    gate_a.release()
check(waited2 <= 1.35, "等待不超过自己给的截止时间（不是两倍）",
      "%.2fs / 给了 1.0s" % waited2)
blocker_done.wait(5)

# -- 10. 两个搜索平分出口（先到先得会饿死后一个） ----------------------
print()
print("== 10. 两个搜索大致平分，而不是先到的全拿 ==")
quiet()
samples = {"A": [], "B": []}
stop2 = threading.Event()


def grab(owner: str) -> None:
    while not stop2.is_set():
        with G.scope(high=False, owner=owner):
            h = G.acquire(5.0, "")
        if h is None:
            continue
        time.sleep(0.10)
        G.release(h)


def sample2() -> None:
    while not stop2.is_set():
        with G._NET._cv:
            for o in ("A", "B"):
                samples[o].append(G._NET._by_owner.get(o, 0))
        time.sleep(0.01)


for o in ("A", "B"):
    for _ in range(12):
        threading.Thread(target=grab, args=(o,), daemon=True).start()
threading.Thread(target=sample2, daemon=True).start()
time.sleep(2.0)
stop2.set()
time.sleep(0.3)
avg_a = sum(samples["A"]) / max(1, len(samples["A"]))
avg_b = sum(samples["B"]) / max(1, len(samples["B"]))
check(avg_b > 0.5, "后加入的搜索拿到了有意义的份额",
      "A 平均 %.1f，B 平均 %.1f" % (avg_a, avg_b))
ratio = min(avg_a, avg_b) / max(0.01, max(avg_a, avg_b))
check(ratio > 0.5, "两者份额大致相当（不是一边独吞）", "比值 %.2f" % ratio)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL OK")
