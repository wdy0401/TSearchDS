# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Small Tk helpers for things Qt gives you for free.

Qt has ``setToolTip``, ``setPlaceholderText`` and a font database on every
widget; Tk has none of them, so they are re-implemented here.  Everything
registers itself in a way that lets the window re-label it on a language
switch (see :meth:`src.ui_tk.main_window.MainWindow._set_language`).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Optional

#: CJK-capable families, best first.  Tk picks a Latin-only default on some
#: installs and Chinese glyphs turn into boxes.
CJK_CANDIDATES = (
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

_NAMED_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
                "TkIconFont", "TkTooltipFont")


def apply_default_font(root: tk.Misc, size: int = 10) -> str:
    """Point Tk's named fonts at the first available CJK family."""
    try:
        families = set(tkfont.families(root))
    except Exception:  # noqa: BLE001 - headless / exotic Tk build
        return ""
    chosen = ""
    for name in CJK_CANDIDATES:
        if name in families:
            chosen = name
            break
    if not chosen:
        return ""
    for named in _NAMED_FONTS:
        try:
            tkfont.nametofont(named).configure(family=chosen, size=size)
        except Exception:  # noqa: BLE001 - font may not exist on this Tk
            continue
    return chosen


def monospace_font(root: tk.Misc, size: int = 9) -> tkfont.Font:
    try:
        families = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        families = set()
    for name in MONO_CANDIDATES:
        if name in families:
            return tkfont.Font(root, family=name, size=size)
    return tkfont.Font(root, family="Courier New", size=size)


class Tooltip:
    """Hover tooltip.  Qt: ``widget.setToolTip(text)``.

    Tk has no native tooltip widget, so this is a manually positioned
    ``Toplevel``.  It also remembers the *untranslated* text so the window can
    re-translate it when the language changes.
    """

    _WAIT = 550

    def __init__(self, widget: tk.Misc, text: str = "") -> None:
        self.widget = widget
        self.source = text
        self.text = text
        self.tip: Optional[tk.Toplevel] = None  # noqa: F821
        self._job = None
        self._id = widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def set(self, text: str, remember: bool = False) -> None:
        if remember:
            self.source = text
        self.text = text
        if self.tip is not None:
            self._show()

    def _on_enter(self, _event=None) -> None:
        self._schedule()

    def _on_leave(self, _event=None) -> None:
        self._cancel()
        self._hide()

    def _schedule(self) -> None:
        self._cancel()
        if not self.text:
            return
        self._job = self.widget.after(self._WAIT, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:  # noqa: BLE001
                pass
            self._job = None

    def _hide(self) -> None:
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.tip = None

    def _show(self) -> None:
        self._hide()
        if not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry("+%d+%d" % (x, y))
            label = tk.Label(tip, text=self.text, justify="left",
                             relief="solid", borderwidth=1,
                             background="#ffffe0", foreground="#24292f",
                             wraplength=520, padx=6, pady=4)
            label.pack()
            self.tip = tip
        except Exception:  # noqa: BLE001 - widget already gone
            self.tip = None


class PlaceholderEntry(tk.Entry):
    """``QLineEdit``-style entry with placeholder text and a clear button.

    Tk's ``ttk.Entry`` has neither; the placeholder is emulated with a grey
    foreground and focus handlers, and the "clear" affordance is a small
    button packed inside the entry.
    """

    def __init__(self, master=None, placeholder: str = "", **kwargs) -> None:
        kwargs.setdefault("relief", "solid")
        kwargs.setdefault("borderwidth", 1)
        super().__init__(master, **kwargs)
        self._source = placeholder
        self._placeholder = placeholder
        self._showing = False
        self._clear_btn: Optional[tk.Button] = None  # noqa: F821
        self._normal_fg = self.cget("foreground") or "#24292f"
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self._put_placeholder()

    # -- placeholder ---------------------------------------------------
    def set_placeholder(self, text: str, remember: bool = False) -> None:
        if remember:
            self._source = text
        self._placeholder = text
        if self._showing:
            self._put_placeholder()

    def _put_placeholder(self) -> None:
        if not self._placeholder:
            return
        if not self.get():
            self._showing = True
            super().delete(0, "end")
            super().insert(0, self._placeholder)
            self.configure(foreground="#8b949e")

    def _focus_in(self, _event=None) -> None:
        if self._showing:
            self._showing = False
            super().delete(0, "end")
            self.configure(foreground=self._normal_fg)

    def _focus_out(self, _event=None) -> None:
        self._put_placeholder()

    # -- value ---------------------------------------------------------
    def value(self) -> str:
        """The typed text -- never the placeholder."""
        if self._showing:
            return ""
        return self.get()

    def set_value(self, text: str) -> None:
        self._showing = False
        self.configure(foreground=self._normal_fg)
        super().delete(0, "end")
        if text:
            super().insert(0, text)

    def clear_value(self) -> None:
        self.set_value("")
        self._put_placeholder()

    # -- clear button --------------------------------------------------
    def enable_clear_button(self, command=None) -> None:
        """Qt: ``setClearButtonEnabled(True)``."""
        if self._clear_btn is not None:
            return
        btn = tk.Label(self, text="✕", foreground="#8b949e", cursor="hand2",
                       padx=2)
        btn.pack(side="right")
        btn.bind("<Button-1>", lambda _e: (self.clear_value(),
                                           command() if command else None))
        self._clear_btn = btn


class ScrolledTree(ttk.Frame):
    """``ttk.Treeview`` with a vertical scrollbar and a header context menu.

    Qt's ``QHeaderView`` lets the user drag columns into a new order and
    emits a signal when a section is resized.  Tk's Treeview supports neither,
    so column order is changed through this menu and a width change is
    detected by diffing the live widths against the last known ones.
    """

    def __init__(self, master=None, columns=(), on_widths_changed=None,
                 on_menu=None, **kwargs) -> None:
        super().__init__(master)
        self.columns = list(columns)
        self._on_widths = on_widths_changed
        self._on_menu = on_menu
        self._last_widths = {}
        self.tree = ttk.Treeview(self, columns=self.columns, show="headings",
                                 selectmode="extended", **kwargs)
        vsb = ttk.Scrollbar(self, orient="vertical",
                            command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal",
                            command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree.bind("<ButtonRelease-1>", lambda _e: self.after_idle(
            self._check_widths), add="+")

    def _check_widths(self) -> None:
        if self._on_widths is None:
            return
        now = {c: self.tree.column(c, "width") for c in self.columns}
        if now != self._last_widths:
            self._last_widths = now
            self._on_widths(now)

    def set_last_widths(self, widths: dict) -> None:
        self._last_widths = dict(widths)

    # -- delegations ---------------------------------------------------
    def __getattr__(self, name):
        # only reached for attributes this Frame does not define; guard the
        # lookup so a missing ``tree`` cannot recurse back into here
        if name.startswith("_"):
            raise AttributeError(name)
        tree = self.__dict__.get("tree")
        if tree is None:
            raise AttributeError(name)
        return getattr(tree, name)
