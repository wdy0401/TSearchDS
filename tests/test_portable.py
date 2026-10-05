# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""便携模式 / 数据目录回退 的判定测试（不联网，几秒跑完）。

跑法： python tests\\test_portable.py

判定逻辑全在 ``proxy/core.py`` 的 ``_portable_kind()`` / ``data_dir()`` 里，
这里把 ``app_dir()`` 指到临时目录、把写探针换成假的，把五种情况都走一遍。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.core.proxy import core as pc  # noqa: E402

fails = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  -- " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


def probe(marker=False, data=False, writable=("appdir", "preferred"),
          localappdata=None):
    """Resolve data_dir() for a synthetic layout."""
    tmp = tempfile.mkdtemp(prefix="tsds_portable_")
    appdir = os.path.join(tmp, "app")
    local = localappdata or os.path.join(tmp, "local")
    os.makedirs(appdir)
    os.makedirs(local)
    if marker:
        open(os.path.join(appdir, "portable.txt"), "w").close()
    if data:
        os.makedirs(os.path.join(appdir, "data"))

    real_app_dir, real_writable = pc.app_dir, pc._dir_is_writable
    real_local = os.environ.get("LOCALAPPDATA")
    pc.app_dir = lambda: appdir
    os.environ["LOCALAPPDATA"] = local
    pc._data_dir_cache = ""
    pc._data_dir_reason = ""

    def fake_writable(path):
        if path == appdir or path.startswith(appdir + os.sep):
            return "appdir" in writable
        if path.startswith(local + os.sep):
            return "preferred" in writable
        return "temp" in writable

    pc._dir_is_writable = fake_writable
    try:
        where = pc.data_dir()
        return {"dir": where, "reason": pc.data_dir_reason(),
                "label": pc.data_dir_label(), "portable": pc.is_portable(),
                "appdir": appdir, "local": local, "tmp": tmp}
    finally:
        pc.app_dir, pc._dir_is_writable = real_app_dir, real_writable
        pc._data_dir_cache = ""
        pc._data_dir_reason = ""
        if real_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = real_local


print("== 便携判定 ==")
r = probe(marker=True)
check(r["reason"] == "portable-marker", "有 portable.txt -> 便携", r["reason"])
check(r["dir"] == os.path.join(r["appdir"], "data"),
      "数据目录 = 程序旁的 data\\", r["dir"])
check(r["portable"] is True, "is_portable() = True")
check("便携" in r["label"], "标签说明了便携", r["label"])
shutil.rmtree(r["tmp"], ignore_errors=True)

r = probe(data=True)
check(r["reason"] == "portable-data", "已有 data\\ 目录 -> 也算便携", r["reason"])
check(r["dir"] == os.path.join(r["appdir"], "data"), "数据目录仍是程序旁", r["dir"])
shutil.rmtree(r["tmp"], ignore_errors=True)

print("\n== 常规模式 ==")
r = probe()
check(r["reason"] == "per-user", "什么都没有 -> 用 %LOCALAPPDATA%", r["reason"])
check(r["dir"] == os.path.join(r["local"], "TSearchDS"),
      "数据目录 = %LOCALAPPDATA%\\TSearchDS", r["dir"])
check(r["portable"] is False, "is_portable() = False")
check("常规" in r["label"], "标签说明了常规", r["label"])
check(not os.path.isdir(os.path.join(r["appdir"], "data")),
      "常规模式不在程序旁建 data\\")
shutil.rmtree(r["tmp"], ignore_errors=True)

print("\n== 回退 ==")
r = probe(writable=("appdir",))          # LOCALAPPDATA 写不进去
check(r["reason"] == "fallback", "%LOCALAPPDATA% 不可写 -> 回退", r["reason"])
check(r["dir"] == os.path.join(r["appdir"], "data"), "回退到程序旁的 data\\", r["dir"])
check(r["portable"] is False, "回退不算「便携模式」（是它自己的标签）", r["portable"])
check("回退" in r["label"], "标签说明了回退", r["label"])
shutil.rmtree(r["tmp"], ignore_errors=True)

r = probe(writable=("temp",))            # 哪儿都写不进去
check(r["reason"] == "temp", "全都不可写 -> 系统临时目录", r["reason"])
check("临时" in r["label"], "标签说明了临时目录", r["label"])
shutil.rmtree(r["tmp"], ignore_errors=True)

print("\n== 真实环境（本机）==")
real = pc.data_dir()
print("      data dir : %s" % real)
print("      reason   : %s" % pc.data_dir_reason())
print("      label    : %s" % pc.data_dir_label())
check(bool(real) and os.path.isdir(real), "真实数据目录存在且可用", real)

print("\n%s" % ("ALL OK" if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
