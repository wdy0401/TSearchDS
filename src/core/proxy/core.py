# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Embedded mihomo core: binary discovery, config generation, process
supervision and automatic node fail-over.

The generated config is deliberately self-contained -- no remote
``rule-providers``, no ``geodata`` downloads -- so the core can boot on a
hostile network without a chicken-and-egg deadlock.  Routing is a single
``MATCH,PROXY`` rule and fail-over is driven by *this* module through the
External Controller, which gives us far faster and far more explicit
behaviour than relying on mihomo's own ``url-test`` interval.

Fail-over ladder
----------------
1. every ``probe_interval`` seconds test the selected node
2. after ``max_failures`` consecutive failures (or on a periodic refresh),
   run a parallel delay sweep across every node and pick the fastest
3. if the sweep finds nothing, re-download the subscription(s); if the node
   set actually changed, hot-reload the config with ``PUT /configs``
4. if there is still nothing alive, fall back to ``DIRECT`` so the app keeps
   working, and keep retrying in the background
"""
from __future__ import annotations

import logging
import os
import random
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import yaml

from ..http import HTTP, proxy_dict, wait_for_port
from ..parallel import pmap
from .controller import MihomoController, last_delay, wait_for_controller
from .subscription import (DEFAULT_SUBSCRIPTIONS, dedupe_proxies,
                           fetch_subscription, parse_subscription)

log = logging.getLogger("tsearch.mihomo")

GROUP_PROXY = "PROXY"
GROUP_AUTO = "AUTO"
TEST_URL = "http://www.gstatic.com/generate_204"

BINARY_NAMES = (
    "mihomo.exe",
    "mihomo-windows-amd64.exe",
    "mihomo-windows-amd64-compatible.exe",
    "mihomo",
    "clash-meta.exe",
    "Clash.Meta.exe",
    "verge-mihomo.exe",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def find_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return ``preferred`` when free, otherwise the OS' next free port."""
    for candidate in [preferred] + [0]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, candidate))
            port = s.getsockname()[1]
            s.close()
            if candidate == 0 or port == preferred:
                return port
        except OSError:
            continue
    return preferred


def random_secret(n: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def app_dir() -> str:
    """Directory that holds the running executable / script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _dir_is_writable(path: str) -> bool:
    """True only if a file can actually be created in ``path``.

    Existing is not the same as writable: a locked-down or sandboxed profile
    happily hands out a directory that ``makedirs`` creates (or that already
    exists) but that rejects every write.  Without this probe the program
    starts, looks fine, and silently drops its config, its cache, its log and
    the mihomo configuration -- which is indistinguishable from "the search
    just returns nothing".
    """
    probe = os.path.join(path, ".tsds-write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False


_data_dir_cache = ""


#: A copy of the program counts as **portable** when one of these sits next to
#: the executable, or when a ``data`` folder is already there.  Everything the
#: app writes then stays inside its own folder, so the whole thing can be
#: zipped up, moved to another machine and unpacked ready to use.
PORTABLE_MARKERS = ("portable.txt", "portable", "TSearchDS.portable")


def _portable_kind() -> str:
    """``"marker"`` / ``"data"`` / ``""`` -- does this copy keep data beside itself?

    An explicit ``portable.txt`` wins; an existing ``data`` folder counts too,
    so a portable copy that was unpacked on another machine stays portable
    even if that machine's ``%LOCALAPPDATA%`` happens to be writable.
    """
    base = app_dir()
    for name in PORTABLE_MARKERS:
        if os.path.isfile(os.path.join(base, name)):
            return "marker"
    return "data" if os.path.isdir(os.path.join(base, "data")) else ""


def data_dir_reason() -> str:
    """Why :func:`data_dir` chose what it chose (``portable-marker`` / ...)."""
    data_dir()
    return _data_dir_reason


def data_dir_label() -> str:
    """Human-readable form of :func:`data_dir_reason` for the UI."""
    return {
        "portable-marker": "便携模式（程序旁的 portable.txt）",
        "portable-data": "便携模式（数据就在程序旁的 data\\）",
        "per-user": "常规模式（%LOCALAPPDATA%\\TSearchDS）",
        "fallback": "回退模式（%LOCALAPPDATA% 不可写，数据放在程序旁的 data\\）",
        "temp": "临时模式（哪里都写不进去，数据放在系统临时目录）",
    }.get(data_dir_reason(), data_dir_reason())


def is_portable() -> bool:
    """True when this copy keeps its data next to the executable."""
    return data_dir_reason().startswith("portable")


_data_dir_cache = ""
_data_dir_reason = ""


def data_dir() -> str:
    """Writable directory for config, cache and the core binary.

    Portable copies use ``<program folder>\\data`` and nothing else.  A normal
    install uses ``%LOCALAPPDATA%\\TSearchDS``, but that is not always usable,
    so it falls back to the program folder and finally to the system temp
    directory.  The resolved path is cached: probing on every call would add a
    file create/delete to every log write.
    """
    global _data_dir_cache, _data_dir_reason
    if _data_dir_cache:
        return _data_dir_cache
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    preferred = os.path.join(local, "TSearchDS")
    beside = os.path.join(app_dir(), "data")
    kind = _portable_kind()
    if kind:
        candidates = [(beside, "portable-" + kind)]
    else:
        candidates = [(preferred, "per-user"),
                      (beside, "fallback"),
                      (os.path.join(tempfile.gettempdir(), "TSearchDS"), "temp")]
    for cand, reason in candidates:
        try:
            os.makedirs(cand, exist_ok=True)
        except OSError:
            continue
        if _dir_is_writable(cand):
            _data_dir_cache, _data_dir_reason = cand, reason
            if reason == "fallback":
                log.warning("数据目录 %s 不可写，改用 %s", preferred, cand)
            elif reason.startswith("portable"):
                log.info("便携模式：数据目录 = %s", cand)
            return cand
    # Nothing is writable.  Return the preferred path anyway so that the
    # failure is reported by whoever tries to write, not here.
    _data_dir_cache, _data_dir_reason = preferred, "none"
    return _data_dir_cache


def locate_binary(explicit: Optional[str] = None) -> Optional[str]:
    """Find a mihomo executable next to the app or in the data dir."""
    cands: List[str] = []
    if explicit:
        cands.append(explicit)
    roots = [app_dir(), data_dir(),
             os.path.join(app_dir(), "resources"),
             os.path.join(app_dir(), "src", "resources"),
             os.path.join(app_dir(), "_internal"),
             os.path.join(app_dir(), "_internal", "resources"),
             os.getcwd()]
    if getattr(sys, "_MEIPASS", None):
        roots.insert(0, os.path.join(sys._MEIPASS, "resources"))
        roots.insert(0, sys._MEIPASS)
    for root in roots:
        for name in BINARY_NAMES:
            cands.append(os.path.join(root, name))
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    which = shutil.which("mihomo") or shutil.which("mihomo.exe")
    return os.path.abspath(which) if which else None


# ---------------------------------------------------------------------------
# mihomo binary bootstrap
# ---------------------------------------------------------------------------

MIHOMO_VERSION = "v1.19.32"
_ASSET = "mihomo-windows-amd64-compatible-%s.zip" % MIHOMO_VERSION
_GH = "https://github.com/MetaCubeX/mihomo/releases/download/%s/%s" % (
    MIHOMO_VERSION, _ASSET)

#: Explicit helper downloads use the official upstream only; startup never calls it.
DOWNLOAD_MIRRORS: Tuple[str, ...] = (_GH,)


def download_binary(dest_dir: Optional[str] = None, timeout: float = 300.0,
                    on_progress: Optional[Callable[[str], None]] = None
                    ) -> Optional[str]:
    """Fetch mihomo-windows-amd64 into ``dest_dir``.  Returns the exe path."""
    import io
    import zipfile

    dest_dir = dest_dir or data_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return None
    target = os.path.join(dest_dir, "mihomo.exe")
    if os.path.isfile(target):
        return target

    for url in DOWNLOAD_MIRRORS:
        if on_progress:
            on_progress("正在下载 mihomo 内核… %s" % url.split("/")[2])
        data = HTTP.get_bytes(url, timeout=timeout, max_bytes=80 << 20,
                              allow_direct_fallback=False)
        if not data or len(data) < (5 << 20):
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".exe")]
                if not names:
                    continue
                names.sort(key=lambda n: ("compatible" not in n, len(n)))
                payload = zf.read(names[0])
        except (zipfile.BadZipFile, OSError, KeyError):
            continue
        if len(payload) < (5 << 20):
            continue
        try:
            tmp = target + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, target)
        except OSError:
            continue
        return target
    return None


def pick_healthy(nodes: Sequence[str], ctl: MihomoController,
                 timeout_ms: int = 5000, workers: int = 16,
                 url: str = TEST_URL, verify: int = 0) -> List[tuple]:
    """Parallel delay sweep.  Returns ``[(name, delay_ms)]`` sorted fastest.

    ``timeout_ms`` is deliberately generous: a 3 s budget makes slow-but-fine
    nodes look dead and the resulting "best" node often dies a second later.
    ``verify`` re-tests the fastest ``verify`` candidates a second time so the
    winner is one that answered twice in a row.
    """
    if not nodes:
        return []
    results: List[tuple] = []

    def _delay(name: str):
        return ctl.delay(name, timeout_ms, url)

    def _keep(name: str, d, _err) -> None:
        if d:
            results.append((name, d))

    pmap(_delay, nodes, workers=min(workers, max(1, len(nodes))),
         timeout=max(45.0, len(nodes) * 3.0), on_result=_keep)
    results.sort(key=lambda kv: kv[1])

    if verify and len(results) > 1:
        cands = results[:max(2, verify)]
        confirmed: List[tuple] = []
        first_seen = dict(cands)

        def _recheck(name: str, d, _err) -> None:
            if d:
                confirmed.append((name, min(first_seen[name], d)))

        pmap(_delay, [n for n, _d in cands],
             workers=min(workers, len(cands)), timeout=30.0,
             on_result=_recheck)
        confirmed.sort(key=lambda kv: kv[1])
        if confirmed:
            # keep the confirmed winners first, then the rest
            done = {n for n, _ in confirmed}
            results = confirmed + [(n, d) for n, d in results if n not in done]
    return results


def bind_child_lifetime(proc: subprocess.Popen, holder: dict) -> bool:
    """Put ``proc`` in a Windows Job Object that dies with this process.

    A plain child process survives its parent on Windows, so force-quitting the
    GUI (crash, Task Manager, ``taskkill /F``) would leave mihomo running and
    listening on the loopback port forever.  A job object with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` makes the kernel terminate the child
    as soon as our process goes away -- by any means -- which is the only way
    to guarantee the app never leaves anything behind on the user's machine.

    Returns True when the assignment succeeded.  ``holder`` keeps the job handle
    alive for the lifetime of the process (closing it would kill the child).
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation",
                         JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = \
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            wintypes.HANDLE(job), JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return False
        handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not handle:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return False
        try:
            ok = bool(kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(handle)))
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        if not ok:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return False
        # keep the handle open: closing it is what triggers the kill
        holder["job"] = job
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("job object assignment failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

class MihomoCore:
    """Owns the mihomo process and its configuration."""

    def __init__(self,
                 binary: Optional[str] = None,
                 subscription_urls: Optional[Sequence[str]] = None,
                 workdir: Optional[str] = None,
                 mixed_port: int = 17890,
                 controller_port: int = 19090,
                 on_event: Optional[Callable[[str, str], None]] = None) -> None:
        self.binary = binary
        self.workdir = workdir or data_dir()
        os.makedirs(self.workdir, exist_ok=True)
        self.subscriptions: List[str] = list(DEFAULT_SUBSCRIPTIONS if subscription_urls is None else subscription_urls)
        self.mixed_port = mixed_port
        self.controller_port = controller_port
        self.secret = random_secret()
        self.on_event = on_event or (lambda kind, msg: None)

        self.proc: Optional[subprocess.Popen] = None
        self.ctl: Optional[MihomoController] = None
        #: keeps the job-object handle open for our lifetime
        self._job_holder: Dict[str, Any] = {}
        self.proxies: List[Dict[str, Any]] = []
        self.config_path = os.path.join(self.workdir, "config.yaml")
        self.log_path = os.path.join(self.workdir, "mihomo.log")
        self._log_fh = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._fails = 0
        self._last_sweep = 0.0
        self._last_refresh = 0.0
        self._current: str = ""
        self.running = False
        self.last_error = ""
        self.status = "未启动"

        # tunables
        self.probe_interval = 20.0
        self.max_failures = 2
        self.sweep_interval = 300.0
        self.subscription_interval = 1800.0
        self.use_proxy_for_sources = True

    # -- state ---------------------------------------------------------
    def _emit(self, kind: str, msg: str) -> None:
        self.status = msg
        log.info("[%s] %s", kind, msg)
        try:
            self.on_event(kind, msg)
        except Exception:
            pass

    # -- binary --------------------------------------------------------
    def ensure_binary(self) -> Optional[str]:
        path = locate_binary(self.binary)
        if path:
            self.binary = path
            return path
        self._emit("info", "未找到 mihomo 内核。请从 MetaCubeX/mihomo 官方项目获取，"
                   "将 mihomo.exe 放在程序目录；也可使用直连搜索。")
        return None

    # -- config --------------------------------------------------------
    def _pick_ports(self) -> None:
        self.mixed_port = find_free_port(self.mixed_port)
        self.controller_port = find_free_port(self.controller_port)

    def build_config(self, proxies: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Build a self-contained, **app-local** mihomo config.

        Isolation guarantees -- this proxy exists only for this process:

        * ``bind-address: 127.0.0.1`` + ``allow-lan: false`` -> no listener is
          reachable from the LAN
        * ``tun.enable: false`` -> mihomo never creates a virtual adapter,
          never installs routes and never rewrites system DNS
        * ``redir-port`` / ``tproxy-port`` / ``socks-port`` / ``port`` are all
          0 -> no transparent-proxy or extra listeners
        * ``dns.listen: ""`` -> no DNS server is exposed to the system
        * nothing here (and nowhere else in the app) touches the WinINET proxy
          registry keys, so the machine's own proxy settings are untouched

        The app's HTTP traffic is routed through this port by setting
        ``proxies`` on its own ``requests`` sessions only.
        """
        names = [p["name"] for p in proxies]
        cfg: Dict[str, Any] = {
            "mixed-port": self.mixed_port,
            # disabled listeners -- mixed-port is the only thing we want open
            "port": 0,
            "socks-port": 0,
            "redir-port": 0,
            "tproxy-port": 0,
            # reachable from this machine only
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            # never touch the host's routing / DNS
            "tun": {"enable": False},
            "mode": "rule",
            "log-level": "warning",
            "ipv6": False,
            "unified-delay": True,
            "tcp-concurrent": True,
            "find-process-mode": "off",
            "external-controller": "127.0.0.1:%d" % self.controller_port,
            "secret": self.secret,
            "profile": {"store-selected": False, "store-fake-ip": False},
            # Deliberately minimal DNS: no fake-ip (we never use TUN) and no
            # geoip/fallback-filter, both of which make mihomo try to download
            # GeoIP/MMDB databases at boot and hang on a blocked network.
            # `listen: ""` means no DNS socket is opened for the system at all;
            # resolution happens in-process for our own outbound requests.
            "dns": {
                "enable": True,
                "listen": "",
                "ipv6": False,
                "enhanced-mode": "normal",
                "use-hosts": False,
                "default-nameserver": ["223.5.5.5", "119.29.29.29"],
                "nameserver": ["223.5.5.5", "119.29.29.29", "1.1.1.1"],
                "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"],
            },
            "proxies": list(proxies),
            "proxy-groups": [
                {
                    "name": GROUP_AUTO,
                    "type": "url-test",
                    "url": TEST_URL,
                    "interval": 180,
                    "tolerance": 60,
                    "timeout": 3000,
                    "lazy": True,
                    "hidden": True,
                    "proxies": names or ["DIRECT"],
                },
                {
                    "name": GROUP_PROXY,
                    "type": "select",
                    "proxies": [GROUP_AUTO, "DIRECT"] + names,
                },
            ],
            "rules": [
                "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
                "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
                "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
                "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
                "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
                "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
                "MATCH," + GROUP_PROXY,
            ],
        }
        return cfg

    @staticmethod
    def audit_config(cfg: Dict[str, Any]) -> List[str]:
        """Return a list of isolation violations in ``cfg`` (empty == clean).

        Runs on every generated config so a future edit -- or a config key
        inherited from an imported profile -- cannot silently start touching
        the host.
        """
        problems: List[str] = []

        def truthy(v) -> bool:
            return v is True or (isinstance(v, str) and v.lower() in ("true", "1", "yes"))

        tun = cfg.get("tun") or {}
        if isinstance(tun, dict) and truthy(tun.get("enable")):
            problems.append("tun.enable is on -> mihomo would modify system routes/DNS")
        for key in ("redir-port", "tproxy-port", "socks-port", "port"):
            v = cfg.get(key, 0) or 0
            if v:
                problems.append("%s=%s -> extra system-visible listener" % (key, v))
        if truthy(cfg.get("allow-lan")):
            problems.append("allow-lan is on -> the proxy would be exposed to the LAN")
        bind = str(cfg.get("bind-address") or "")
        if bind and bind not in ("127.0.0.1", "localhost", "::1"):
            problems.append("bind-address=%s -> not loopback" % bind)
        ctl = str(cfg.get("external-controller") or "")
        if ctl and not (ctl.startswith("127.0.0.1") or ctl.startswith("localhost")):
            problems.append("external-controller=%s -> not loopback" % ctl)
        dns = cfg.get("dns") or {}
        if isinstance(dns, dict) and dns.get("listen"):
            problems.append("dns.listen=%s -> opens a DNS socket for the system"
                            % dns.get("listen"))
        return problems

    def write_config(self, proxies: Sequence[Dict[str, Any]]) -> str:
        cfg = self.build_config(proxies)
        problems = self.audit_config(cfg)
        if problems:
            # never hand mihomo a config that could reach outside this process
            self._emit("error", "配置隔离检查未通过，已拒绝写入: " + "; ".join(problems))
            raise RuntimeError("unsafe mihomo config: " + "; ".join(problems))
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, width=4096)
        os.replace(tmp, self.config_path)
        return self.config_path

    # -- config validation ---------------------------------------------
    def self_test(self, proxies: Sequence[Dict[str, Any]],
                  path: Optional[str] = None,
                  temporary: bool = False) -> tuple:
        """Run ``mihomo -t`` on a candidate config.

        Returns ``(ok, error_text)``.  A single malformed node from the
        provider kills the whole core, so every generated config is checked
        before it is handed to the long-lived process.  ``temporary`` deletes
        the scratch config again -- pruning a 60-node subscription otherwise
        leaves 60 files in the user's data directory forever.
        """
        binary = self.binary or locate_binary()
        if not binary:
            return True, ""
        path = path or os.path.join(self.workdir, "_selftest.yaml")
        cfg = self.build_config(proxies)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False,
                               default_flow_style=False, width=4096)
        except OSError as exc:
            return True, str(exc)
        try:
            try:
                proc = subprocess.run(
                    [binary, "-d", self.workdir, "-t", "-f", path],
                    capture_output=True, timeout=45,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError) as exc:
                return True, str(exc)  # cannot test -> do not block the app
            out = (proc.stdout or b"").decode("utf-8", "replace") + \
                  (proc.stderr or b"").decode("utf-8", "replace")
            if proc.returncode == 0:
                return True, ""
            reason = ""
            for line in out.splitlines():
                if "level=error" in line or "level=fatal" in line:
                    reason = line.split("msg=", 1)[-1].strip().strip('"')
            return False, reason or out.strip()[:300]
        finally:
            if temporary:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def prune_invalid(self, proxies: Sequence[Dict[str, Any]],
                      workers: int = 8) -> List[Dict[str, Any]]:
        """Drop nodes mihomo refuses to load (bad REALITY ids, bad ciphers...)."""
        proxies = list(proxies)
        if not proxies:
            return proxies
        ok, err = self.self_test(proxies, os.path.join(self.workdir, "_selftest.yaml"))
        if ok:
            return proxies
        self._emit("warn", "配置校验失败（%s），正在剔除无效节点…" % (err or "unknown"))

        def _probe(p: Dict[str, Any]) -> bool:
            # one scratch file per worker thread: the probes run in parallel
            # and must not rewrite each other's config mid-validation
            good, _ = self.self_test(
                [p], os.path.join(self.workdir,
                                  "_t_%d.yaml" % (threading.get_ident() % 100000)),
                temporary=True)
            return good

        kept: List[Dict[str, Any]] = []

        def _keep(p: Dict[str, Any], good, _err) -> None:
            if good:
                kept.append(p)

        pmap(_probe, proxies, workers=min(workers, len(proxies)),
             on_result=_keep)
        # keep the original order
        order = {id(p): i for i, p in enumerate(proxies)}
        kept.sort(key=lambda p: order.get(id(p), 0))
        dropped = len(proxies) - len(kept)
        if dropped:
            self._emit("warn", "已剔除 %d 个无效节点，保留 %d 个" % (dropped, len(kept)))
        return kept

    # -- subscription --------------------------------------------------
    #: where the last good node list is cached between runs
    @property
    def cache_path(self) -> str:
        return os.path.join(self.workdir, "proxies_cache.json")

    #: nodes the user pasted / imported by hand; these always win
    @property
    def user_nodes_path(self) -> str:
        return os.path.join(self.workdir, "user_nodes.json")

    def load_user_nodes(self) -> List[Dict[str, Any]]:
        try:
            import json
            with open(self.user_nodes_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            out = data.get("proxies") if isinstance(data, dict) else data
            if isinstance(out, list):
                return [p for p in out if isinstance(p, dict)]
        except (OSError, ValueError):
            pass
        return []

    def save_user_nodes(self, proxies: Sequence[Dict[str, Any]]) -> None:
        try:
            import json
            tmp = self.user_nodes_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"saved": time.time(), "proxies": list(proxies)}, fh,
                          ensure_ascii=False)
            os.replace(tmp, self.user_nodes_path)
        except OSError:
            pass

    def import_text(self, text: str, merge: bool = True) -> tuple:
        """Accept pasted mihomo/Clash YAML or node links.

        Returns ``(count, error)``.  Imported nodes are stored separately and
        are always merged *in front of* whatever the subscriptions return, so a
        user-supplied config keeps working even when the provider dies.
        """
        from .subscription import parse_pasted_content
        proxies, err = parse_pasted_content(text)
        if err:
            return 0, err
        if merge:
            existing = self.load_user_nodes()
            by_key = {(p.get("type"), p.get("server"), p.get("port"),
                       str(p.get("uuid") or p.get("password") or "")): p
                      for p in existing}
            for p in proxies:
                by_key[(p.get("type"), p.get("server"), p.get("port"),
                        str(p.get("uuid") or p.get("password") or ""))] = p
            proxies = dedupe_proxies(list(by_key.values()))
        self.save_user_nodes(proxies)
        return len(proxies), ""

    def clear_user_nodes(self) -> None:
        try:
            os.unlink(self.user_nodes_path)
        except OSError:
            pass

    def _save_cache(self, proxies: Sequence[Dict[str, Any]]) -> None:
        try:
            import json
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"saved": time.time(), "proxies": list(proxies)}, fh,
                          ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _load_cache(self) -> List[Dict[str, Any]]:
        try:
            import json
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            out = data.get("proxies")
            if isinstance(out, list):
                return [p for p in out if isinstance(p, dict)]
        except (OSError, ValueError):
            pass
        return []

    def load_cached(self) -> bool:
        """Restore the node list from the previous successful fetch."""
        cached = self._load_cache()
        if not cached:
            return False
        with self._lock:
            self.proxies = dedupe_proxies(cached)
        return bool(self.proxies)

    def refresh_subscription(self, timeout: float = 25.0,
                             attempts: int = 3) -> bool:
        """Re-download every configured subscription URL.

        The providers used here reset connections, time out and return 403
        depending on the moment and the TLS/UA fingerprint, so every URL is
        retried and the previous good node list is kept as a fallback.

        Nodes the user imported by hand are collected first and always survive,
        so a provider outage can never leave the app without a proxy.
        """
        self._last_refresh = time.monotonic()
        user_nodes = self.load_user_nodes()
        for inst in self._instance_nodes():
            user_nodes.extend(inst)
        collected: List[Dict[str, Any]] = list(user_nodes)
        downloaded: List[Dict[str, Any]] = []

        for url in self.subscriptions:
            parsed = fetch_subscription(
                url, timeout=timeout, attempts=attempts,
                use_proxy=self.running and bool(self.proxies))
            if parsed:
                downloaded.extend(parsed.get("proxies") or [])

        if not downloaded:
            # a subscription file the user dropped into the data dir
            for name in ("subscription.yaml", "sub.yaml", "proxies.yaml",
                         "subscription.txt", "nodes.txt", "imported.yaml"):
                p = os.path.join(self.workdir, name)
                if os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as fh:
                            parsed = parse_subscription(fh.read())
                    except OSError:
                        parsed = None
                    if parsed:
                        downloaded.extend(parsed.get("proxies") or [])

        if not downloaded and not collected and self.load_cached():
            collected = list(self.proxies)

        if not collected and not downloaded:
            self.last_error = "订阅获取失败（服务端连接不稳定），也没有可用的本地节点"
            return False

        # user-imported nodes keep their names and come first
        merged = dedupe_proxies(list(user_nodes) + list(downloaded))
        if not merged:
            self.last_error = "没有可用节点"
            return False
        with self._lock:
            self.proxies = merged
        if downloaded:
            self._save_cache(self.proxies)
        self.last_error = ""
        if not downloaded and user_nodes:
            self._emit("info", "订阅不可用，使用本地导入的 %d 个节点" % len(user_nodes))
        return True

    def _instance_nodes(self) -> List[Dict[str, Any]]:
        """Nodes pushed in by the GUI (import dialog) during this session."""
        return list(getattr(self, "_pushed_nodes", []) or [])

    def apply_user_nodes(self, proxies: Sequence[Dict[str, Any]]) -> None:
        """Make newly imported nodes take effect immediately."""
        self._pushed_nodes = list(proxies)
        existing = self.load_user_nodes()
        merged = dedupe_proxies(list(proxies) + existing)
        self.save_user_nodes(merged)
        self.proxies = dedupe_proxies(list(merged) + list(self.proxies))
        self.proxies = self.prune_invalid(self.proxies)
        self.write_config(self.proxies)
        if self.ctl is not None and self.running:
            if self.ctl.reload_config(self.config_path):
                self._emit("info", "已应用导入的节点，共 %d 个" % len(self.proxies))
                self._last_sweep = 0.0
                threading.Thread(target=self._sweep, daemon=True).start()
            else:
                self._emit("warn", "热重载失败，重启内核生效")
                self.stop()
                self.start()
        else:
            self._emit("info", "已保存 %d 个导入节点" % len(self.proxies))

    def _node_set_changed(self, new_names: List[str]) -> bool:
        return sorted(new_names) != sorted(p["name"] for p in self.proxies)

    # -- process -------------------------------------------------------
    @property
    def pid_path(self) -> str:
        return os.path.join(self.workdir, "mihomo.pid")

    def _kill_orphan(self) -> None:
        """Terminate a mihomo left behind by a previous hard-killed run.

        The core is a plain child process, so force-quitting the GUI (or a
        crash) would otherwise leave it listening on the loopback port forever.
        We only ever touch a PID we wrote ourselves *and* whose executable is
        our own bundled binary, so this can never kill an unrelated process or
        the user's own Clash/mihomo.
        """
        try:
            with open(self.pid_path, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return
        if pid <= 0 or pid == os.getpid():
            return
        binary = (self.binary or locate_binary() or "").lower()
        try:
            if os.name != "nt":
                raise OSError("windows-only cleanup")
            import subprocess as _sp
            out = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Process -Id %d -ErrorAction SilentlyContinue).Path" % pid],
                capture_output=True, timeout=15,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            path = out.stdout.decode("utf-8", "replace").strip().lower()
        except Exception:
            path = ""
        if path and binary and os.path.basename(path) == os.path.basename(binary):
            try:
                import subprocess as _sp
                _sp.run(["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=15,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                log.info("terminated orphaned mihomo pid=%d from a previous run", pid)
            except Exception:
                pass
        try:
            os.unlink(self.pid_path)
        except OSError:
            pass

    def _write_pid(self) -> None:
        if self.proc is None:
            return
        try:
            with open(self.pid_path, "w", encoding="utf-8") as fh:
                fh.write(str(self.proc.pid))
        except OSError:
            pass

    def start(self, wait: float = 25.0) -> bool:
        if self.running:
            return True
        self._kill_orphan()
        binary = self.ensure_binary()
        if not binary:
            self.last_error = ("未找到 mihomo 可执行文件。请将 mihomo.exe 放在程序同目录，"
                               "请自行从 https://github.com/MetaCubeX/mihomo 获取。")
            self._emit("error", self.last_error)
            return False
        if not self.proxies:
            self._emit("info", "正在获取代理订阅…")
            if not self.refresh_subscription():
                self._emit("warn", "订阅获取失败，将以直连模式启动")
        self.proxies = self.prune_invalid(self.proxies)
        self._pick_ports()
        self.write_config(self.proxies)

        args = [binary, "-d", self.workdir, "-f", self.config_path]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._log_fh = open(self.log_path, "ab", buffering=0)
            self.proc = subprocess.Popen(
                args, cwd=self.workdir,
                stdout=self._log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags)
        except OSError as exc:
            self.last_error = "启动 mihomo 失败: %s" % exc
            self._emit("error", self.last_error)
            return False

        self.ctl = MihomoController("127.0.0.1", self.controller_port, self.secret)
        if not wait_for_controller(self.ctl, timeout=wait):
            tail = self._log_tail()
            self.last_error = "mihomo 控制接口无响应。" + ((" 日志: " + tail) if tail else "")
            self._emit("error", self.last_error)
            self.stop()
            return False
        if not wait_for_port("127.0.0.1", self.mixed_port, timeout=10.0):
            self.last_error = "mihomo 混合端口未就绪"
            self._emit("error", self.last_error)
            self.stop()
            return False

        self.running = True
        self._stop.clear()
        # make the kernel kill mihomo whenever this process dies, by any means
        if bind_child_lifetime(self.proc, self._job_holder):
            log.debug("mihomo bound to a kill-on-close job object")
        else:
            # not fatal -- the pidfile cleanup catches it on the next start --
            # but a silent failure here is how orphans happen, so say so
            log.warning("job object unavailable; mihomo would outlive a hard kill "
                        "and is cleaned up on the next launch instead")
        self._write_pid()
        HTTP.set_proxy(proxy_dict(self.mixed_port))
        self._emit("info", "mihomo 已启动 (端口 %d，节点 %d 个)"
                   % (self.mixed_port, len(self.proxies)))

        self._thread = threading.Thread(target=self._health_loop,
                                        name="mihomo-health", daemon=True)
        self._thread.start()
        # pick an initial node in the background so the UI is not blocked
        threading.Thread(target=self._initial_select, daemon=True).start()
        return True

    def _initial_select(self) -> None:
        try:
            best = self._sweep()
            if best:
                # _sweep already emitted the detailed "node|delay|count" event,
                # so this is just a short confirmation -- and it must not try to
                # format two placeholders from a single string
                self._emit("info", "已选节点 %s" % best)
            else:
                self._emit("warn", "暂无可用节点，正在重试…")
        except Exception as exc:  # noqa: BLE001
            log.debug("initial select failed: %s", exc)

    def _log_tail(self, n: int = 800) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - n))
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self.ctl is not None:
            self.ctl.close()
            self.ctl = None
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        try:
            os.unlink(self.pid_path)
        except OSError:
            pass
        HTTP.set_proxy(None)
        self._emit("info", "mihomo 已停止")
        self._current = ""

    # -- fail-over -----------------------------------------------------
    def _all_node_names(self) -> List[str]:
        if not self.ctl:
            return []
        proxies = self.ctl.proxies()
        info = proxies.get(GROUP_PROXY) or {}
        out = []
        for name in (info.get("all") or []):
            if name in (GROUP_AUTO, "DIRECT", "REJECT"):
                continue
            out.append(name)
        return out

    def _sweep(self) -> Optional[str]:
        """Test every node and select the fastest.  Returns its name."""
        if not self.ctl:
            return None
        nodes = self._all_node_names()
        if not nodes:
            return None
        self._emit("info", "正在测速 %d 个节点…" % len(nodes))
        self._last_sweep = time.monotonic()
        healthy = pick_healthy(nodes, self.ctl, timeout_ms=5000, workers=20,
                               verify=4)
        if not healthy:
            self._current = ""
            return None
        best, delay = healthy[0]
        if self.ctl.select(GROUP_PROXY, best):
            self._current = best
            self._fails = 0
            self._emit("node", "%s|%d|%d" % (best, delay, len(healthy)))
            return best
        return None

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.probe_interval):
                break
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.debug("health tick failed: %s", exc)

    def _tick(self) -> None:
        if not self.ctl or not self.running:
            return
        if self.proc is not None and self.proc.poll() is not None:
            self._emit("error", "mihomo 进程已退出，正在重启…")
            self.stop()
            time.sleep(1.0)
            self.start()
            return

        now = time.monotonic()
        if now - self._last_sweep > self.sweep_interval:
            if self._sweep():
                return

        cur = self._current or GROUP_AUTO
        if cur == GROUP_AUTO:
            # let mihomo's url-test drive; only intervene when nothing works
            proxies = self.ctl.proxies()
            d = last_delay(proxies, GROUP_AUTO)
            if d is None:
                self._fails += 1
                if self._fails >= self.max_failures:
                    if not self._sweep():
                        self._recover()
            else:
                self._fails = 0
            return

        # one retry with a longer budget before believing the node is dead --
        # a single transient stall should not trigger a full 27-node sweep
        d = self.ctl.delay(cur, timeout_ms=3000) or \
            self.ctl.delay(cur, timeout_ms=6000)
        if d:
            self._fails = 0
            return

        self._fails += 1
        self._emit("warn", "节点 %s 探测失败 (%d/%d)"
                   % (cur, self._fails, self.max_failures))
        if self._fails < self.max_failures:
            return
        if self._sweep():
            return
        self._recover()

    def _recover(self) -> None:
        """All nodes dead: refresh the subscription (rate limited), else DIRECT."""
        now = time.monotonic()
        if now - self._last_refresh > 120.0:
            self._emit("warn", "所有节点失效，正在重新获取订阅…")
            before = [p["name"] for p in self.proxies]
            if self.refresh_subscription():
                after = [p["name"] for p in self.proxies]
                if before != after:
                    self.proxies = self.prune_invalid(self.proxies)
                    self.write_config(self.proxies)
                    if self.ctl and self.ctl.reload_config(self.config_path):
                        self._emit("info", "配置已热重载，节点 %d 个" % len(self.proxies))
                        self._last_sweep = 0.0
                        time.sleep(1.0)
                        if self._sweep():
                            return
                else:
                    self._emit("warn", "订阅节点未变化")
        if self.ctl and self.ctl.select(GROUP_PROXY, "DIRECT"):
            self._current = "DIRECT"
            self._fails = 0
            self._emit("warn", "无可用节点，已切换为直连")

    # -- manual controls ----------------------------------------------
    def manual_select(self, name: str) -> bool:
        if not self.ctl:
            return False
        ok = self.ctl.select(GROUP_PROXY, name)
        if ok:
            self._current = name
            self._fails = 0
        return ok

    def manual_sweep(self) -> Optional[str]:
        return self._sweep()

    @property
    def current_node(self) -> str:
        return self._current or "自动"

    @property
    def proxy_url(self) -> Optional[str]:
        if not self.running:
            return None
        return "http://127.0.0.1:%d" % self.mixed_port
