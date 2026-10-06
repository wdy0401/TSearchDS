# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Dialog for importing proxy content as text (Tk backend).

Accepts whatever the user has: a full mihomo / Clash config exported from
FlClash, Clash Verge, mihomo itself, a bare ``proxies:`` fragment, a base64
subscription blob, or a plain list of node URIs.

Qt version: ``dlg.exec() == QDialog.Accepted``.  Tk has no modal loop of its
own, so :meth:`show` grabs the keyboard and blocks on ``wait_window``.
"""
from __future__ import annotations

from ..ui.i18n import tr
from ..ui.constants import PLACEHOLDER

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional, Tuple

from .widgets import monospace_font

FILE_TYPES = (("配置文件", "*.yaml *.yml *.txt *.conf *.json"),
              ("所有文件", "*"))


class ImportProxyDialog:
    """Modal dialog; :attr:`parsed` holds the nodes after ``show() == True``."""

    def __init__(self, parent=None, on_parse=None) -> None:
        self.parent = parent
        self._on_parse = on_parse
        self.parsed: List[dict] = []
        self.accepted = False
        self._check_job = None

        root = self._root()
        self.top = tk.Toplevel(root)
        self.top.title(tr("导入代理内容 · from ds"))
        self.top.geometry("820x580")
        self.top.transient(root)
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(1, weight=1)

        hint = ttk.Label(
            self.top,
            text=tr("支持 Clash / mihomo YAML 全文、proxies 片段、节点链接列表、base64 订阅。"),
            wraplength=780, justify="left")
        hint.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        self.edit = tk.Text(self.top, wrap="none", relief="solid",
                            borderwidth=1, font=monospace_font(self.top, 9))
        self.edit.grid(row=1, column=0, sticky="nsew", padx=10)
        self._put_placeholder()
        self.edit.bind("<KeyRelease>", self._schedule_check)

        row = ttk.Frame(self.top)
        row.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        self.btn_paste = ttk.Button(row, text=tr("从剪贴板粘贴"),
                                    command=self._paste_clipboard)
        self.btn_file = ttk.Button(row, text=tr("从文件导入…"),
                                   command=self._load_file)
        self.btn_clear = ttk.Button(row, text=tr("清空"),
                                    command=self._clear)
        self.btn_paste.pack(side="left")
        self.btn_file.pack(side="left", padx=(6, 0))
        self.btn_clear.pack(side="left", padx=(6, 0))

        self.lbl = ttk.Label(self.top, text=tr("等待内容…"),
                             wraplength=780, justify="left")
        self.lbl.grid(row=3, column=0, sticky="ew", padx=10)

        btns = ttk.Frame(self.top)
        btns.grid(row=4, column=0, sticky="e", padx=10, pady=10)
        self.btn_ok = ttk.Button(btns, text=tr("导入并使用"),
                                 command=self._on_ok, state="disabled")
        self.btn_cancel = ttk.Button(btns, text=tr("取消"),
                                     command=self._on_cancel)
        self.btn_cancel.pack(side="right")
        self.btn_ok.pack(side="right", padx=(0, 6))

        self.top.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._prefill_from_clipboard()

    # -- lifecycle -----------------------------------------------------
    def _root(self) -> tk.Misc:
        if self.parent is None:
            return tk._default_root or tk.Tk()
        return getattr(self.parent, "root", self.parent)

    def show(self) -> bool:
        """Run modally; ``True`` when the user accepted."""
        self.top.grab_set()
        self.top.wait_window()
        return self.accepted

    #: Qt call sites read ``dlg.exec()``; keep a familiar alias
    run = show

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except Exception:  # noqa: BLE001
            pass

    # -- content -------------------------------------------------------
    def _placeholder(self) -> str:
        return tr(PLACEHOLDER)

    def _put_placeholder(self) -> None:
        self._showing_placeholder = True
        self.edit.configure(foreground="#8b949e")
        self.edit.delete("1.0", "end")
        self.edit.insert("1.0", self._placeholder())

    def text(self) -> str:
        """Typed content -- never the placeholder."""
        if getattr(self, "_showing_placeholder", False):
            return ""
        return self.edit.get("1.0", "end").strip()

    def set_text(self, value: str) -> None:
        self._showing_placeholder = False
        self.edit.configure(foreground="#24292f")
        self.edit.delete("1.0", "end")
        if value:
            self.edit.insert("1.0", value)
        self._schedule_check()

    def _clear(self) -> None:
        self._put_placeholder()
        self._schedule_check()

    @staticmethod
    def _looks_like_config(text: str) -> bool:
        t = text.strip()
        if len(t) < 40:
            return False
        markers = ("proxies:", "://", "proxy-groups:", "port:", "mixed-port:")
        return any(m in t for m in markers)

    def _prefill_from_clipboard(self) -> None:
        try:
            clip = self.top.clipboard_get()
        except Exception:  # noqa: BLE001 - clipboard empty or unowned
            return
        if clip and self._looks_like_config(clip):
            self.set_text(clip)

    def _paste_clipboard(self) -> None:
        try:
            clip = self.top.clipboard_get()
        except Exception:  # noqa: BLE001
            return
        if clip:
            self.set_text(clip)

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.top, title=tr("选择配置文件"), filetypes=FILE_TYPES)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                self.set_text(fh.read())
        except OSError as exc:
            messagebox.showwarning(tr("读取失败"), str(exc), parent=self.top)

    # -- validation ----------------------------------------------------
    def _schedule_check(self, _event=None) -> None:
        if getattr(self, "_showing_placeholder", False):
            self.edit.configure(foreground="#24292f")
            self.edit.delete("1.0", "end")
            self._showing_placeholder = False
        if self._check_job is not None:
            try:
                self.top.after_cancel(self._check_job)
            except Exception:  # noqa: BLE001
                pass
        self._check_job = self.top.after(350, self._check)

    def _check(self) -> None:
        self._check_job = None
        text = self.text()
        if self._on_parse is None:
            self.btn_ok.configure(
                state="normal" if text.strip() else "disabled")
            return
        proxies, err = self._on_parse(text)
        if err:
            self.parsed = []
            self.btn_ok.configure(state="disabled")
            if not text.strip():
                self.lbl.configure(text=tr("等待内容…"), foreground="#24292f")
            else:
                self.lbl.configure(text="⚠ %s" % err, foreground="#b3261e")
            return
        self.parsed = proxies
        self.btn_ok.configure(state="normal")
        kinds = {}
        for p in proxies:
            kinds[str(p.get("type"))] = kinds.get(str(p.get("type")), 0) + 1
        detail = ", ".join("%s×%d" % (k, v) for k, v in sorted(kinds.items()))
        self.lbl.configure(text=tr("✔ 识别到 %d 个节点 (%s)")
                           % (len(proxies), detail),
                           foreground="#1a7f37")

    def _on_ok(self) -> None:
        if not self.parsed:
            self._check()
        if not self.parsed:
            messagebox.showinfo(tr("没有可用节点"),
                                tr("没有从内容中解析出任何可用节点。"),
                                parent=self.top)
            return
        self.accepted = True
        self.destroy()

    def _on_cancel(self) -> None:
        self.accepted = False
        self.destroy()


def parse_for_preview(text: str) -> Tuple[List[dict], str]:
    from ..core.proxy.subscription import parse_pasted_content
    return parse_pasted_content(text)


#: kept so callers can ``isinstance``-free check either backend
__all__ = ["ImportProxyDialog", "parse_for_preview", "PLACEHOLDER"]


def placeholder_text() -> Optional[str]:  # pragma: no cover - convenience
    return tr(PLACEHOLDER)
