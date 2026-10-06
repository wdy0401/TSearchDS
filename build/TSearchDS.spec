# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TSearch-DS.

Builds a single-file windowed exe.  The mihomo core is deliberately *not*
embedded: a 61 MB payload inside a onefile archive would make every launch pay
a multi-second extraction cost.  Instead the exe looks for ``mihomo.exe``
next to itself or in its data directory. Public source users obtain the core
separately from the official upstream; startup never downloads it.
"""
import os
import sys

block_cipher = None

ROOT = os.path.abspath(os.getcwd())

#: ``TSDS_CONSOLE=1`` builds the same app as a console binary.  The delivery
#: build is windowed (no console window, and ``print`` goes nowhere), so this
#: is the only way to read a traceback when the frozen app dies before it can
#: write a report file.
CONSOLE = os.environ.get("TSDS_CONSOLE") == "1"

#: Where a one-file build unpacks itself before any of its own code runs.
#:
#: The default is the system temp directory, and that is the one thing about
#: the machine this program cannot control or report on: where ``%TEMP%`` is
#: locked down, redirected, aggressively cleaned or simply unwritable, the
#: bootloader dies with a modal "Could not create temporary directory!" box
#: and the app never starts -- which looks exactly like "double-clicked it and
#: nothing happened".  A directory next to the executable is always available
#: and writable wherever the user can keep the program, so the app no longer
#: depends on ``%TEMP%`` at all.  (Relative paths are resolved against the
#: executable's directory by the bootloader.)
RUNTIME_TMPDIR = os.environ.get("TSDS_RUNTIME_TMPDIR") or "run"


def _conda_runtime_dlls():
    """Collect the OpenSSL/zlib DLLs a conda Python keeps outside the stdlib.

    PyInstaller's binary analysis does not always follow ``_ssl.pyd``'s
    ``libssl-3-x64.dll`` / ``libcrypto-3-x64.dll`` dependency when Python comes
    from a conda prefix, and the frozen app then dies with
    "Can't create an SSLContext object without an ssl module".
    """
    import glob
    import sysconfig

    roots = []
    for key in ("prefix", "base_prefix", "exec_prefix"):
        v = getattr(sys, key, None)
        if v:
            roots.append(v)
    roots.append(sysconfig.get_paths().get("data", ""))

    patterns = [
        "libssl-3-x64.dll", "libcrypto-3-x64.dll",
        "libssl-1_1-x64.dll", "libcrypto-1_1-x64.dll",
        # conda names libffi's runtime plain `ffi.dll` (plus versioned copies);
        # `_ctypes.pyd` links against it, and without it `import ctypes` dies
        # with "DLL load failed while importing _ctypes"
        "ffi.dll", "ffi-7.dll", "ffi-8.dll",
        "libffi-8.dll", "libffi-7.dll",
        "libbz2.dll", "liblzma.dll", "libzlib.dll", "zlib1.dll",
        "libmpdec-4.dll", "libxml2.dll", "libxslt.dll", "libexslt.dll",
        "yaml.dll", "libjpeg.dll",
    ]
    found = {}
    for root in dict.fromkeys(roots):
        if not root or not os.path.isdir(root):
            continue
        for sub in ("Library/bin", "DLLs", "bin", "Library/lib"):
            d = os.path.join(root, sub)
            if not os.path.isdir(d):
                continue
            for pat in patterns:
                if pat in found:
                    continue
                hits = glob.glob(os.path.join(d, pat))
                if hits:
                    found[pat] = hits[0]
    return [(path, ".") for path in found.values()]


EXTRA_BINARIES = _conda_runtime_dlls()

EXCLUDES = [
    # keep the bundle small; an Anaconda base env would otherwise drag in GBs
    "PyQt5", "PyQt6", "PySide2", "PySide6", "shiboken6", "src.ui.main_window",
    "src.ui.result_model", "src.ui.search_tab", "src.ui.import_dialog",
    "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "IPython", "jupyter", "notebook", "sqlalchemy", "sympy", "numba",
    "cupy", "pyarrow", "bokeh", "sphinx", "pytest", "setuptools",

]

hidden = [
    "yaml", "requests", "urllib3", "charset_normalizer", "idna", "certifi",
    # the stdlib XML parser lives in a C extension that PyInstaller's static
    # import scan does not always pick up, and every RSS feed source needs it
    "pyexpat", "xml", "xml.etree", "xml.etree.ElementTree",
    "xml.parsers", "xml.parsers.expat", "_elementtree", "_socket",
    "select", "ssl", "_ssl", "_hashlib", "zlib", "bz2", "_bz2", "lzma", "_lzma",
    # ctypes backs the Windows job-object that guarantees mihomo dies with us;
    # without _ctypes it fails silently and orphans the proxy process
    "ctypes", "ctypes.wintypes", "_ctypes",
    # faulthandler writes fatal.txt when native code aborts the process (Qt's
    # qFatal path), which is otherwise a silent disappearance
    "faulthandler",
    "src.core.parallel",
    "src.core.gateway",
    "src.core.logredact",
    "tkinter", "_tkinter", "src.ui_tk.main_window",
    "src.ui_tk.result_model", "src.ui_tk.search_tab", "src.ui_tk.import_dialog",
    "src.core.sources.torrents",
    "src.core.sources.archive",
    "src.core.sources.ed2k",
    "src.core.sources.kad_source",
    "src.core.proxy.core",
    "src.core.proxy.controller",
    "src.core.proxy.subscription",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=EXTRA_BINARIES,
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# PATH may contain a different Conda environment. Pair Tcl/Tk DLLs with
# the scripts collected by the tkinter hook, never an unrelated PATH copy.
for index, item in enumerate(a.binaries):
    name, source, kind = item
    if os.path.basename(name).lower() in ("tcl86t.dll", "tk86t.dll"):
        matching = os.path.join(sys.prefix, "Library", "bin", os.path.basename(name))
        if os.path.isfile(matching):
            a.binaries[index] = (name, matching, kind)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TSearchDS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=RUNTIME_TMPDIR,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "build", "app.ico")
    if os.path.isfile(os.path.join(ROOT, "build", "app.ico")) else None,
)
