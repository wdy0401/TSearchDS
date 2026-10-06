# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Framework-independent constants shared by every UI backend.

``APP_TITLE`` and ``LINK_FORMATS`` used to live inside the Qt result model.
They are pure data, so both the Qt backend (:mod:`src.ui`) and the Tk backend
(:mod:`src.ui_tk`) import them from here -- the Tk build must never have to
import PySide6 to get at a string.
"""
from __future__ import annotations

APP_TITLE = "TSearch-DS · 磁力 / 电驴 / 迅雷 搜索   from ds"

#: (label, key) pairs for the link-format selector.  Labels go through ``tr``.
LINK_FORMATS = [
    ("原始链接", "auto"),
    ("磁力 (magnet)", "magnet"),
    ("电驴 (ed2k)", "ed2k"),
    ("迅雷 (thunder://)", "thunder"),
]

#: Placeholder of the proxy-import box.  Pure text, so it lives with the other
#: constants instead of inside either backend's dialog module.
PLACEHOLDER = """在此粘贴代理配置或节点内容，例如：

  • FlClash / Clash Verge / mihomo 导出的完整配置（含 proxies: 段）
  • 只有 proxies: 的 YAML 片段
  • 一行一个的节点链接：vless:// vmess:// trojan:// ss:// hysteria2:// tuic://
  • base64 编码的订阅内容

导入后会自动剔除 mihomo 无法加载的节点，并与订阅合并使用。"""
