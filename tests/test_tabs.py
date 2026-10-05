# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""多标签 + 不保存搜索记录 的回归测试（需要联网，但不走代理）。

跑法： python tests\\test_tabs.py

用 apibay 一个源就够了：它不需要代理就能直连，而且每次搜索都会返回结果。
测的是"两个搜索能不能同时在跑、结果各归各的标签"，不是搜索质量本身。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402

from src.core.logredact import install as install_redaction  # noqa: E402
from src.core.links import safe_page_url  # noqa: E402
from src.core.proxy.core import data_dir, data_dir_label  # noqa: E402
from src.core.sources import REGISTRY, load_dynamic_sources  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.search_tab import SearchTab  # noqa: E402

fails = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


def pump(app, tabs, timeout=120.0):
    """Run the event loop until every tab's session has stopped."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if tabs and not any(t.running for t in tabs):
            break
        time.sleep(0.05)
    for _ in range(30):          # let the last queued batches land
        app.processEvents()
        time.sleep(0.02)


print("== 启动 ==")
app = QtWidgets.QApplication(["tabs-test"])

log_path = os.path.join(tempfile.gettempdir(), "tsds_tabtest.log")
if os.path.exists(log_path):
    os.remove(log_path)
fh = logging.FileHandler(log_path, encoding="utf-8")
fh.setLevel(logging.DEBUG)
# 和程序里 _setup_logging() 用的是同一个过滤器；这里故意把 urllib3 也放开到
# DEBUG，让它把完整请求行打出来，再看过滤器有没有把查询串抹掉
install_redaction(fh)
logging.getLogger().addHandler(fh)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger("urllib3").setLevel(logging.DEBUG)

load_dynamic_sources()
# 这个测试会改「哪些源启用」并写 settings.json —— 而 settings.json 是程序真正
# 在用的那份，所以先备份，结束时还原，免得把用户的设置改成"只开一个源"
_saved_sources = REGISTRY.snapshot()
_settings_backup = None
_settings_path_early = os.path.join(data_dir(), "settings.json")
if os.path.isfile(_settings_path_early):
    _settings_backup = open(_settings_path_early, encoding="utf-8").read()

win = MainWindow(start_proxy=False)
for s in REGISTRY.all():
    s.enabled = (s.id == "apibay")     # 直连可用，测试不用等代理
check(win.tabs.count() == 1, "启动时有一个空标签")
check(isinstance(win.current_tab(), SearchTab), "空标签就是 SearchTab")
check(win.current_tab().model.rowCount() == 0, "空标签没有结果")

print("\n== 一个搜索 = 一个标签 ==")
win.edit.setText("ubuntu")
win.start_search()
tab1 = win.current_tab()
check(win.tabs.count() == 1, "第一个搜索复用启动时那个空标签（不浪费）",
      win.tabs.count())
check(tab1.query == "ubuntu", "标签记住了查询词", tab1.query)

win.edit.setText("debian")
win.start_search()
tab2 = win.current_tab()
check(win.tabs.count() == 2, "第二个搜索新开标签", win.tabs.count())
check(tab1 is not tab2, "两个标签是不同对象")
check(tab2.query == "debian", "新标签的查询词", tab2.query)
check(tab1.model is not tab2.model, "两个标签各自持有结果模型")
check(tab1.model.rowCount() == 0 or tab2.model.rowCount() == 0,
      "结果不会串到别的标签")

print("\n== 并行 ==")
check(tab1.running and tab2.running,
      "两个搜索同时在跑（不是排队）",
      "tab1=%s tab2=%s" % (tab1.running, tab2.running))

pump(app, [tab1, tab2], timeout=150)
check(not tab1.running and not tab2.running, "两个搜索都结束了")
check(tab1.model.rowCount() > 0, "标签1 有结果", tab1.model.rowCount())
check(tab2.model.rowCount() > 0, "标签2 有结果", tab2.model.rowCount())
print("      标签1: %d 条 / 标签2: %d 条"
      % (tab1.model.rowCount(), tab2.model.rowCount()))

print("\n== 标签标题 / 独立性 ==")
check(win.tabs.tabText(0).startswith("ubuntu"), "标签1 标题", win.tabs.tabText(0))
check(win.tabs.tabText(1).startswith("debian"), "标签2 标题", win.tabs.tabText(1))
check("(" in win.tabs.tabText(1), "标题里带结果条数", win.tabs.tabText(1))

names1 = {r.name for r in tab1.model.rows}
names2 = {r.name for r in tab2.model.rows}
check(names1 != names2, "两个标签的结果集不同")

# 每个结果的可打开页面都不能带查询词（否则会写进浏览器历史）
leaky = [r.name for r in tab1.model.rows
         if "ubuntu" in str(r.extra.get("page") or "").lower()]
check(not leaky, "真实结果里没有把关键词放进 page 的", len(leaky))
unsanitized = [r.extra.get("page") for r in tab1.model.rows
               if r.extra.get("page")
               and safe_page_url(str(r.extra["page"]), tab1.query)
               != r.extra["page"]]
check(not unsanitized, "表格里的 page 都已经过滤过（进表前就过了一遍）",
      unsanitized[:2])

# 过滤只影响自己的标签
tab1.proxy_model.set_keyword_filter("ubuntu")
check(tab1.proxy_model.rowCount() <= tab1.model.rowCount(), "过滤生效")
check(tab2.proxy_model.rowCount() == tab2.model.rowCount(), "另一个标签不受影响")

# 复制链接：每个标签只复制自己的
links1 = tab1.links_for(tab1.results())
links2 = tab2.links_for(tab2.results())
check("\r\n" in links2, "多选复制就是按行分割的文本")
check(len(links1.splitlines()) == tab1.proxy_model.rowCount(),
      "标签1 复制条数 = 过滤后行数",
      "%d vs %d" % (len(links1.splitlines()), tab1.proxy_model.rowCount()))
check(links1 != links2, "两个标签复制出不同的链接")

print("\n== 关标签 = 停搜索 ==")
win.edit.setText("kubuntu")
win.start_search()
tab3 = win.current_tab()
sess3 = tab3.session
i3 = win.tabs.indexOf(tab3)
check(tab3.running, "第三个搜索在跑")
win.close_tab(i3)
check(win.tabs.indexOf(tab3) == -1, "标签被关掉了")
check(sess3 is not None and sess3.cancelled, "关标签会取消它的搜索")
rows_at_close = tab3.model.rowCount()
deadline = time.monotonic() + 6.0          # 取消是协作式的，给它几秒收尾
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.1)
check(tab3.model.rowCount() == rows_at_close,
      "关掉的标签不会再被塞进新结果", tab3.model.rowCount())
check(win.tabs.count() == 2, "剩下的标签还在", win.tabs.count())

print("\n== 不保存搜索记录 ==")
leak = "zqxleakprobe%d" % (int(time.time()) % 100000)
win.edit.setText(leak)
win.start_search()
tab4 = win.current_tab()
pump(app, [tab4], timeout=60)
win._save_settings()

with open(win.settings_path, encoding="utf-8") as f2:
    cfg = json.load(f2)
check(not [k for k in cfg if k.lower() in
           ("query", "last_query", "history", "recent", "search")],
      "settings.json 里没有任何查询相关的键", sorted(cfg))
check(leak not in json.dumps(cfg, ensure_ascii=False),
      "settings.json 里没有刚才搜的词")
check(leak not in open(win.settings_path, encoding="utf-8").read(),
      "settings.json 原文里也搜不到该词")

time.sleep(0.6)
logging.shutdown()
blob = open(log_path, encoding="utf-8", errors="replace").read()
check(leak not in blob, "调试日志里没有搜索关键词（URL 查询串已隐去）", log_path)
check("已隐去" in blob, "日志里确实出现过被隐去的查询串（过滤器真的在起作用）")
check("apibay.org" in blob or "q.php" in blob,
      "隐去查询串之后日志仍然有用（主机/路径还在）")
check(len(blob) > 2000, "调试日志确实写了东西（否则上面几条不算数）", len(blob))

print("\n== 数据目录（便携判定见 tests\\test_portable.py）==")
print("      %s -> %s" % (data_dir_label(), data_dir()))
check(bool(data_dir()) and os.path.isdir(data_dir()), "数据目录存在且可用")

win.close()

# 还原：源开关回滚，settings.json 用测试开始前的内容覆盖回去
for sid, on in _saved_sources.items():
    s = REGISTRY.get(sid)
    if s is not None:
        s.enabled = bool(on)
try:
    if _settings_backup is not None:
        with open(_settings_path_early, "w", encoding="utf-8") as f3:
            f3.write(_settings_backup)
        print("  (已还原 settings.json 与数据源开关)")
    else:
        os.remove(_settings_path_early)
        print("  (已删掉测试期间生成的 settings.json)")
except OSError as exc:  # noqa: BLE001
    print("  [WARN] 还原 settings.json 失败: %s" % exc)

print("\n%s" % ("ALL OK" if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
