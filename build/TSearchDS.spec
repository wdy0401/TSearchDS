# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TSearch-DS.

Builds a single-file windowed exe.  The mihomo core is deliberately *not*
embedded: a 61 MB payload inside a onefile archive would make every launch pay
a multi-second extraction cost.  Instead the exe looks for ``mihomo.exe``
next to itself (shipped in ``dist/``) and falls back to downloading it into
``%LOCALAPPDATA%\\TSearchDS`` on first run.
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


def _conda_qt_binaries():
    """Collect conda's Qt runtime.

    A conda ``PyQt5`` links against ``Qt5Core_conda.dll`` (conda renames the Qt
    libraries to avoid clashing in ``Library/bin``) while PyInstaller's PyQt5
    hook only looks for the standard ``PyQt5/Qt5/bin/Qt5Core.dll`` layout.  The
    result is a frozen app that dies at ``import PyQt5.QtCore`` with
    "DLL load failed".  Everything therefore has to be added by hand.
    """
    import glob
    import sys as _sys

    roots = [getattr(_sys, "prefix", ""), getattr(_sys, "base_prefix", "")]
    out = []
    seen = set()
    # Only the Qt modules this app can actually touch.  Bundling all 75
    # Qt5*.dll from Library/bin adds ~80 MB of WebEngine/Quick/3D/Charts that
    # is never loaded.
    qt_patterns = ("Qt5Core*.dll", "Qt5Gui*.dll", "Qt5Widgets*.dll",
                   "Qt5Svg*.dll", "Qt5Network*.dll")
    for root in dict.fromkeys(r for r in roots if r):
        bindir = os.path.join(root, "Library", "bin")
        if os.path.isdir(bindir):
            for pat in qt_patterns + ("libGLESv2.dll", "libEGL.dll",
                                      "libgcc_s_seh-1.dll", "libstdc++-6.dll",
                                      "libwinpthread-1.dll"):
                for f in glob.glob(os.path.join(bindir, pat)):
                    if f not in seen:
                        seen.add(f)
                        # root: QtCore.pyd lives there, so Windows resolves
                        # its dependencies from the module's own directory
                        out.append((f, "."))
        pdir = os.path.join(root, "Library", "plugins")
        if os.path.isdir(pdir):
            for sub in ("platforms", "styles", "imageformats",
                        "iconengines", "platformthemes", "accessible"):
                d = os.path.join(pdir, sub)
                if not os.path.isdir(d):
                    continue
                for f in glob.glob(os.path.join(d, "*.dll")):
                    if (f, sub) in seen:
                        continue
                    seen.add((f, sub))
                    # both the layout PyInstaller's runtime hook exports via
                    # QT_PLUGIN_PATH and a flat copy at the bundle root
                    out.append((f, "PyQt5/Qt5/plugins/" + sub))
                    out.append((f, sub))
    return out


EXTRA_BINARIES = _conda_runtime_dlls() + _conda_qt_binaries()

EXCLUDES = [
    # keep the bundle small; an Anaconda base env would otherwise drag in GBs
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "IPython", "jupyter", "notebook", "sqlalchemy", "sympy", "numba",
    "cupy", "pyarrow", "bokeh", "sphinx", "pytest", "setuptools",
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngine",
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets", "PyQt5.Qt3DCore",
    "PyQt5.QtBluetooth", "PyQt5.QtDesigner", "PyQt5.QtHelp",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets", "PyQt5.QtNfc",
    "PyQt5.QtOpenGL", "PyQt5.QtPositioning", "PyQt5.QtSql",
    "PyQt5.QtSerialPort", "PyQt5.QtTest", "PyQt5.QtXml", "PyQt5.QtXmlPatterns",
    "PyQt5.QtLocation", "PyQt5.QtSensors", "PyQt5.QtWebSockets",
    "PyQt5.QtWebChannel", "PyQt5.QtPrintSupport",
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
    "src.ui.result_model",
    "src.ui.search_tab",
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

# NOTE: PyInstaller's bundled PyQt5 hook already collects the Qt plugins and
# translations we need, so no manual datas entries are required here.

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
