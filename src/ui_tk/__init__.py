# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Tkinter backend for the TSearch-DS interface.

Public surface mirrors :mod:`src.ui` so ``main.py`` can pick either one:
:class:`MainWindow`, :class:`SearchTab`, :class:`ResultModel`,
:class:`ImportProxyDialog` and :func:`parse_for_preview`.

Tcl/Tk ships with CPython itself, so this backend adds **no** third-party GUI
dependency at all -- see ``docs/UI_FRAMEWORK_COMPARISON.md``.
"""
