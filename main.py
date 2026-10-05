"""TSearch-DS entry point.

A PyQt5 desktop search client for magnet / eD2k / thunder links with an
embedded mihomo proxy core.  Window title carries ``from ds``.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from typing import Dict, List

APP_TITLE = "TSearch-DS · 磁力 / 电驴 / 迅雷 搜索   from ds"


def _data_dir_raw() -> str:
    """Data directory, preferring the one the app itself resolved.

    Crash reporting must not depend on anything that could itself be the thing
    that is broken, so an import failure falls back to the plain ``%LOCALAPPDATA%``
    path computed with the standard library alone.
    """
    try:
        from src.core.proxy.core import data_dir
        return data_dir()
    except Exception:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "TSearchDS")


def _write_crash(context: str = "") -> str:
    """Append the current traceback (plus a note) to ``crash.txt``."""
    import traceback
    path = os.path.join(_data_dir_raw(), "crash.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("context   : %s\n" % context)
            fh.write("frozen    : %s\n" % getattr(sys, "frozen", False))
            fh.write("executable: %s\n" % sys.executable)
            fh.write("argv      : %r\n" % (sys.argv,))
            fh.write(traceback.format_exc())
    except Exception:
        pass
    return path


def _attach_console() -> None:
    """Borrow the parent terminal's console in a windowed build.

    A ``console=False`` exe starts with ``sys.stdout is None``, so ``print``
    goes nowhere and the command-line modes look like they hang with no output.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):
            return
    except Exception:
        return
    for name in ("stdout", "stderr"):
        try:
            setattr(sys, name, open("CONOUT$", "w", encoding="utf-8",
                                    errors="replace", buffering=1))
        except OSError:
            pass


def _enable_faulthandler() -> None:
    """Record a traceback when the process dies inside native code.

    Qt's ``qFatal`` path calls ``abort()``: no Python exception, no
    ``crash.txt``, just a process that disappears.
    """
    try:
        import faulthandler
        path = os.path.join(_data_dir_raw(), "fatal.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        faulthandler.enable(open(path, "a", encoding="utf-8", buffering=1))
    except Exception:
        pass


class Reporter:
    """Diagnostic sink: prints, and keeps the report file current line by line.

    Rewriting after every line matters because a frozen build can abort with no
    traceback at all -- the last line that reached the file is then the only
    evidence of how far it got.
    """

    def __init__(self, name: str) -> None:
        self.lines: List[str] = []
        self.path = os.path.join(_data_dir_raw(), name)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)
        try:
            print(text, flush=True)
        except Exception:
            pass
        self.flush()

    def flush(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self.lines))
        except OSError:
            pass


def _setup_logging(verbose: bool = False) -> None:
    from src.core.logredact import install as install_redaction
    from src.core.proxy.core import data_dir
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(
            os.path.join(data_dir(), "tsds.log"), encoding="utf-8"))
    except OSError:
        pass
    # 搜索关键词就在索引 URL 的查询串里，而 urllib3 在 DEBUG 级别会把整个请求
    # 行打出来 —— 所以「不保存搜索记录」必须靠过滤器保证，而不是靠日志级别
    install_redaction(*handlers)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers)
    # urllib3's per-retry chatter is useful in the log but drowns everything
    # else at INFO level
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def _prepare_frozen_qt() -> None:
    """Make conda's hand-collected Qt runtime findable in the frozen app.

    The bundle carries the Qt libraries and plugins at the archive root (see
    ``build/TSearchDS.spec``), so the DLL search path and the Qt plugin paths
    have to be pointed at the extraction directory *before* PyQt5 is imported.
    """
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    candidates = [
        base,
        os.path.join(base, "PyQt5", "Qt5", "bin"),
        os.path.join(base, "PyQt5", "Qt5", "plugins"),
        os.path.join(base, "Library", "bin"),
    ]
    for path in candidates:
        if not os.path.isdir(path):
            continue
        try:
            os.add_dll_directory(path)
        except (AttributeError, OSError):
            pass
        try:
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass
    plug = os.path.join(base, "PyQt5", "Qt5", "plugins")
    if os.path.isdir(plug):
        os.environ.setdefault("QT_PLUGIN_PATH", plug)
    for sub in ("platforms", "styles", "imageformats", "iconengines"):
        d = os.path.join(base, sub)
        if os.path.isdir(d):
            os.environ.setdefault(
                "QT_QPA_PLATFORM_PLUGIN_PATH" if sub == "platforms"
                else "QT_PLUGIN_PATH", d)
            if sub != "platforms":
                os.environ["QT_PLUGIN_PATH"] = (
                    d + os.pathsep + os.environ.get("QT_PLUGIN_PATH", ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="TSearch-DS", add_help=True)
    parser.add_argument("--no-proxy", action="store_true",
                        help="不启动内置 mihomo 代理")
    parser.add_argument("--query", "-q", default="",
                        help="启动后立即搜索的关键词")
    parser.add_argument("--self-test", action="store_true",
                        help="只做自检（依赖、mihomo、订阅），不进 GUI")
    parser.add_argument("--smoke", metavar="关键词", default="",
                        help="无界面跑一次完整流程（启动代理+搜索），结果写文件后退出")
    parser.add_argument("--smoke-seconds", type=float, default=90.0,
                        help="--smoke 的总时间预算")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test or args.smoke:
        # these modes are normally driven from a terminal, and in a windowed
        # build the standard streams do not exist until we ask for them
        _attach_console()
        _enable_faulthandler()

    _setup_logging(args.verbose)
    log = logging.getLogger("tsds")
    _prepare_frozen_qt()

    if args.self_test:
        return _self_test()
    if args.smoke:
        return _smoke(args.smoke, args.smoke_seconds)

    # Qt must see the high-DPI flags before QApplication is constructed
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: F811

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("TSearch-DS")
    app.setApplicationDisplayName(APP_TITLE)
    app.setQuitOnLastWindowClosed(True)

    def _excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        log.error("unhandled exception:\n%s", text)
        try:
            _write_crash("sys.excepthook")
        except Exception:
            pass
        try:
            QtWidgets.QMessageBox.critical(
                None, "TSearch-DS 出错", text[-2000:])
        except Exception:
            pass

    sys.excepthook = _excepthook

    from src.core.fonts import apply_default_font
    apply_default_font(app)

    from src.ui.main_window import MainWindow
    win = MainWindow(start_proxy=not args.no_proxy)
    win.show()

    if args.query:
        if args.no_proxy:
            win.edit.setText(args.query)
            QtCore.QTimer.singleShot(400, win.start_search)
        else:
            # wait for the embedded proxy before hitting the indexes
            win.queue_search(args.query)

    return app.exec_()


def _self_test() -> int:
    """Headless diagnostics -- useful when the packaged exe misbehaves."""
    out = Reporter("selftest.txt")

    ok = True
    out("== TSearch-DS self test ==")
    out("python     : %s" % sys.version.split()[0])
    out("frozen     : %s" % getattr(sys, "frozen", False))
    out("executable : %s" % sys.executable)
    try:
        import PyQt5  # noqa: F401
        out("PyQt5      : ok")
    except Exception as exc:  # noqa: BLE001
        out("PyQt5      : FAIL %s" % exc)
        ok = False

    # importing the package is not enough -- Qt's own DLLs are a separate
    # failure mode that only shows up when a real Qt module is loaded
    try:
        _prepare_frozen_qt()
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: F401
        out("QtCore     : %s" % QtCore.QT_VERSION_STR)
        out("QtWidgets  : ok")
        _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["selftest"])
        _scr = QtWidgets.QWidget()
        _scr.resize(120, 60)
        _scr.show()
        _app.processEvents()
        _scr.close()
        out("Qt window  : ok")
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb
        out("Qt module  : FAIL %s" % exc)
        out(_tb.format_exc()[-900:])
        ok = False

    # the tabbed window is what the user actually double-clicks into, so build
    # it here too: a missing module in the bundle only shows up at that point
    try:
        from src.ui.main_window import MainWindow, ResultModel
        _win = MainWindow(start_proxy=False)
        _t1 = _win.current_tab()
        _t2 = _win.new_tab(query="selftest")
        out("window     : ok (%d tabs)" % _win.tabs.count())
        out("tabs       : %s" % ", ".join(
            _win.tabs.tabText(i) for i in range(_win.tabs.count())))
        if _win.tabs.count() != 2 or _t1 is _t2:
            raise AssertionError("tab handling is broken")
        if _t2.model.columnCount() != len(ResultModel.HEADERS):
            raise AssertionError("column count is wrong")
        _win.close()
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb2
        out("window     : FAIL %s" % exc)
        out(_tb2.format_exc()[-700:])
        ok = False

    try:
        from src.core.proxy.core import (data_dir, data_dir_label,
                                         locate_binary)
        d = data_dir()
        out("data dir   : %s" % d)
        out("mode       : %s" % data_dir_label())
        b = locate_binary()
        out("mihomo     : %s" % (b or "NOT FOUND (will auto-download)"))
        if b:
            import subprocess
            p = subprocess.run(
                [b, "-v"], capture_output=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out("mihomo -v  : %s"
                % (p.stdout or p.stderr).decode("utf-8", "replace").strip())
    except Exception as exc:  # noqa: BLE001
        out("mihomo     : FAIL %s" % exc)
        ok = False

    try:
        from src.core.sources import REGISTRY, load_dynamic_sources
        load_dynamic_sources()
        srcs = REGISTRY.all()
        out("sources    : %d" % len(srcs))
        for s in srcs:
            out("   - %-16s %s" % (s.id, s.label))
    except Exception as exc:  # noqa: BLE001
        out("sources    : FAIL %s" % exc)
        ok = False

    try:
        from src.core.kad import md4
        assert md4(b"abc").hex() == "a448017aaf21d8525fc10ae87aa6729d"
        out("md4        : ok")
    except Exception as exc:  # noqa: BLE001
        out("md4        : FAIL %s" % exc)
        ok = False

    # ctypes + Job Objects are what guarantee the proxy child cannot outlive us
    try:
        import ctypes  # noqa: F401
        from ctypes import wintypes  # noqa: F401
        _k = ctypes.WinDLL("kernel32", use_last_error=True)
        _job = _k.CreateJobObjectW(None, None)
        if not _job:
            raise OSError("CreateJobObjectW failed")
        _k.CloseHandle(wintypes.HANDLE(_job))
        out("ctypes/job : ok (子进程可随主进程一并终止)")
    except Exception as exc:  # noqa: BLE001
        out("ctypes/job : FAIL %s" % exc)
        out("             -> 强杀程序时 mihomo 可能残留，下次启动会自动清理")
        ok = False

    try:
        from src.core.links import (build_ed2k, thunder_decode, thunder_encode)
        u = "magnet:?xt=urn:btih:" + "a" * 40
        assert thunder_decode(thunder_encode(u)) == u
        assert build_ed2k("x.mkv", 10, "b" * 32).startswith("ed2k://|file|")
        out("links      : ok")
    except Exception as exc:  # noqa: BLE001
        out("links      : FAIL %s" % exc)
        ok = False

    out("== %s ==" % ("ALL OK" if ok else "PROBLEMS FOUND"))
    out.flush()
    return 0 if ok else 1


def _smoke(query: str, budget: float) -> int:
    """Headless end-to-end run: start the proxy, search, dump a report.

    This is the check that actually proves the *frozen* bundle works --
    imports alone say nothing about threads, UDP sockets, TLS or the mihomo
    subprocess.
    """
    import time
    from src.core.proxy.core import MihomoCore, data_dir
    from src.core.sources import REGISTRY, load_dynamic_sources
    from src.core.aggregator import SearchSession

    lines: List[str] = []
    out = Reporter("smoke.txt")
    out("== TSearch-DS smoke test ==")
    out("query      : %s" % query)
    out("frozen     : %s" % getattr(sys, "frozen", False))
    out("budget     : %.0fs" % budget)
    t_start = time.monotonic()

    subscriptions = []
    try:
        import json
        with open(os.path.join(data_dir(), "settings.json"), encoding="utf-8") as stream:
            subscriptions = json.load(stream).get("subscriptions", [])
    except (OSError, ValueError, AttributeError):
        pass
    core = MihomoCore(subscription_urls=subscriptions,
                      on_event=lambda k, m: out("  [proxy] %-5s %s" % (k, m)))
    proxy_ok = False
    try:
        proxy_ok = core.start()
        out("proxy up   : %s (%d nodes, port %s)"
            % (proxy_ok, len(core.proxies), core.mixed_port))
        if proxy_ok:
            best = core.manual_sweep()
            out("selected   : %s" % best)
            from src.core.http import HTTP
            r = HTTP.get("https://api.ipify.org", timeout=20)
            if r is not None:
                out("exit ip    : %s" % r.text.strip()[:40])
            else:
                out("exit ip    : (request failed)")
    except Exception as exc:  # noqa: BLE001
        out("proxy FAIL : %s" % exc)

    load_dynamic_sources()
    sources = REGISTRY.enabled()
    out("sources    : %d enabled" % len(sources))
    try:
        # the search gets its *own* budget -- the proxy warm-up above must not
        # eat into it, otherwise the report is written before results land
        search_budget = max(45.0, budget)
        sess = SearchSession(query, sources=sources, enrich_seeders=True,
                             per_source_limit=30)
        t_search = time.monotonic()
        sess.start()
        sess.join(search_budget)
        if sess.running:
            out("NOTE       : search still running after %.0fs, reporting "
                "partial results" % search_budget)
        out("")
        out(sess.summary())
        out("search took: %.1fs" % (time.monotonic() - t_search))
        out("per-source : %s" % sess.counts)
        out("errors     : %s" % {k: v[:70] for k, v in sess.errors.items()})
        out("")
        out("%-5s | %-8s | %-52s | %-9s | %-8s | %s"
            % ("资源数", "文件大小", "名称", "文件类型", "来源", "链接"))
        out("-" * 158)
        for r in sess.results[:25]:
            out("%-5s | %-8s | %-52s | %-9s | %-8s | %s"
                % (r.seeds_display, r.size_display, r.name[:52],
                   r.type_display, r.source[:8], r.link[:56]))
        # a breakdown so the two new columns are actually visible in the report
        types: Dict[str, int] = {}
        sized = 0
        for r in sess.results:
            types[r.type_display] = types.get(r.type_display, 0) + 1
            if r.size_bytes:
                sized += 1
        out("")
        out("文件类型分布 : %s" % dict(sorted(types.items(), key=lambda kv: -kv[1])))
        out("有文件大小   : %d/%d" % (sized, len(sess.results)))
        out("")
        out("total results: %d" % len(sess.results))
    except Exception as exc:  # noqa: BLE001
        import traceback
        out("search FAIL: %s\n%s" % (exc, traceback.format_exc()))

    try:
        core.stop()
    except Exception:
        pass
    out("elapsed    : %.1fs" % (time.monotonic() - t_start))
    out("== smoke done ==")
    out("report     : %s" % out.path)
    out.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # a windowed exe has no console, so make sure the traceback survives
        _write_crash("top level")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "TSearch-DS 启动失败，详情见 crash.txt",
                "TSearch-DS", 0x10)
        except Exception:
            pass
        raise
