# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Dialog for importing proxy content as text.

Accepts whatever the user has: a full mihomo / Clash config exported from
FlClash, Clash Verge, mihomo itself, a bare ``proxies:`` fragment, a base64
subscription blob, or a plain list of node URIs.
"""
from __future__ import annotations

from .i18n import tr

from typing import List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .constants import PLACEHOLDER  # noqa: F401  (re-exported for callers)


class ImportProxyDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, on_parse=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("导入代理内容 · from ds"))
        self.resize(820, 580)
        self._on_parse = on_parse
        self.parsed: List[dict] = []

        root = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            tr("支持 Clash / mihomo YAML 全文、proxies 片段、节点链接列表、base64 订阅。"))
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.edit = QtWidgets.QPlainTextEdit()
        self.edit.setPlaceholderText(tr(PLACEHOLDER))
        self.edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.edit.setFont(QtGui.QFont("Consolas, Courier New", 9))
        self.edit.textChanged.connect(self._schedule_check)
        root.addWidget(self.edit, 1)

        row = QtWidgets.QHBoxLayout()
        self.btn_paste = QtWidgets.QPushButton(tr("从剪贴板粘贴"))
        self.btn_file = QtWidgets.QPushButton(tr("从文件导入…"))
        self.btn_clear = QtWidgets.QPushButton(tr("清空"))
        row.addWidget(self.btn_paste)
        row.addWidget(self.btn_file)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        root.addLayout(row)

        self.lbl = QtWidgets.QLabel(tr("等待内容…"))
        self.lbl.setWordWrap(True)
        root.addWidget(self.lbl)

        btns = QtWidgets.QDialogButtonBox()
        self.btn_ok = btns.addButton(tr("导入并使用"), QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(tr("取消"), QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_ok.setEnabled(False)
        root.addWidget(btns)

        self.btn_paste.clicked.connect(self._paste_clipboard)
        self.btn_file.clicked.connect(self._load_file)
        self.btn_clear.clicked.connect(self.edit.clear)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._check)

        clip = QtWidgets.QApplication.clipboard().text()
        if clip and self._looks_like_config(clip):
            self.edit.setPlainText(clip)

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _looks_like_config(text: str) -> bool:
        t = text.strip()
        if len(t) < 40:
            return False
        markers = ("proxies:", "://", "proxy-groups:", "port:", "mixed-port:")
        return any(m in t for m in markers)

    def _schedule_check(self) -> None:
        self._timer.start()

    def _paste_clipboard(self) -> None:
        text = QtWidgets.QApplication.clipboard().text()
        if text:
            self.edit.setPlainText(text)

    def _load_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("选择配置文件"), "",
            tr("配置文件 (*.yaml *.yml *.txt *.conf *.json);;所有文件 (*)"))
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.edit.setPlainText(fh.read())
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, tr("读取失败"), str(exc))

    def _check(self) -> None:
        text = self.edit.toPlainText()
        if self._on_parse is None:
            self.btn_ok.setEnabled(bool(text.strip()))
            return
        proxies, err = self._on_parse(text)
        if err:
            self.parsed = []
            self.btn_ok.setEnabled(False)
            if not text.strip():
                self.lbl.setText(tr("等待内容…"))
                self.lbl.setStyleSheet("")
            else:
                self.lbl.setText("⚠ %s" % err)
                self.lbl.setStyleSheet("color:#b3261e;")
            return
        self.parsed = proxies
        self.btn_ok.setEnabled(True)
        kinds = {}
        for p in proxies:
            kinds[str(p.get("type"))] = kinds.get(str(p.get("type")), 0) + 1
        detail = ", ".join("%s×%d" % (k, v) for k, v in sorted(kinds.items()))
        self.lbl.setText(tr("✔ 识别到 %d 个节点 (%s)") % (len(proxies), detail))
        self.lbl.setStyleSheet("color:#1a7f37;")

    def accept(self) -> None:
        if not self.parsed:
            self._check()
        if not self.parsed:
            QtWidgets.QMessageBox.information(
                self, tr("没有可用节点"), tr("没有从内容中解析出任何可用节点。"))
            return
        super().accept()


def parse_for_preview(text: str) -> Tuple[List[dict], str]:
    from ..core.proxy.subscription import parse_pasted_content
    return parse_pasted_content(text)
