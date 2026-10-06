# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Font selection.

Qt's default font on a Chinese Windows install can render CJK glyphs as
boxes when it picks a Latin-only family, so pick the first available
CJK-capable family and fall back gracefully.
"""
from __future__ import annotations

from PySide6 import QtGui, QtWidgets

CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "Segoe UI",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "PingFang SC",
    "SimHei",
    "SimSun",
)

MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New")


def apply_default_font(app: QtWidgets.QApplication, size: int = 10) -> str:
    families = set(QtGui.QFontDatabase().families())
    chosen = ""
    for name in CANDIDATES:
        if name in families:
            chosen = name
            break
    font = QtGui.QFont(chosen or app.font().family(), size)
    font.setStyleStrategy(QtGui.QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    mono = ""
    for name in MONO_CANDIDATES:
        if name in families:
            mono = name
            break
    if mono:
        mf = QtGui.QFont(mono, size - 1)
        app.setFont(font)  # keep UI font; only the details pane uses mono
        return chosen or app.font().family()
    return chosen or app.font().family()


def monospace_font(size: int = 9) -> QtGui.QFont:
    families = set(QtGui.QFontDatabase().families())
    for name in MONO_CANDIDATES:
        if name in families:
            return QtGui.QFont(name, size)
    f = QtGui.QFont("Courier New", size)
    f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    return f
