"""Assert the embedded proxy is strictly local to this process.

The requirement: using the proxy must affect *only* TSearch-DS.  It must never
change the machine's proxy settings, never create a TUN adapter, never install
routes, and never expose a listener beyond loopback.

Run:  python tests/test_proxy_isolation.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(ok: bool, label: str, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  -- " + detail) if detail else ""))
    return ok


def section(t: str) -> None:
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def main() -> int:
    from src.core.proxy.core import MihomoCore
    from src.core.proxy.subscription import parse_clash_yaml, parse_pasted_content
    from src.core.http import HTTP

    fixture = os.path.join(ROOT, "tests", "fixtures", "flclash_config.yaml")

    # ------------------------------------------------------------------
    section("1. 导入的配置带 tun: enable: true 时，解析阶段就丢掉它")
    raw = open(fixture, encoding="utf-8").read()
    check("tun:" in raw and "enable: true" in raw,
          "fixture 本身确实开了 TUN（否则这个测试没意义）")
    parsed = parse_clash_yaml(raw)
    check(parsed is not None, "clash yaml 解析成功")
    check("tun" not in (parsed or {}),
          "parse_clash_yaml 没有把 tun 传出来",
          "keys=%s" % sorted((parsed or {}).keys()))
    proxies, err = parse_pasted_content(raw)
    check(not err and proxies, "粘贴导入仍能拿到节点", "%d nodes" % len(proxies))

    # ------------------------------------------------------------------
    section("2. 生成的 config 必须是纯本地代理")
    core = MihomoCore(binary=os.path.join(ROOT, "src", "resources", "mihomo.exe"),
                      workdir=r"C:\tmp\tsds_isolation",
                      mixed_port=17895, controller_port=19095)
    os.makedirs(core.workdir, exist_ok=True)
    cfg = core.build_config(proxies)

    tun = cfg.get("tun") or {}
    check(tun.get("enable") is False, "tun.enable = false",
          "-> 不会创建虚拟网卡 / 不改系统路由 / 不改系统 DNS")
    for key in ("port", "socks-port", "redir-port", "tproxy-port"):
        check(not cfg.get(key), "%s = 0" % key,
              "-> 没有多余监听口，也没有透明代理")
    check(cfg.get("allow-lan") is False, "allow-lan = false",
          "-> 局域网其它机器连不上")
    check(cfg.get("bind-address") == "127.0.0.1", "bind-address = 127.0.0.1")
    ctl = str(cfg.get("external-controller", ""))
    check(ctl.startswith("127.0.0.1:"), "external-controller 绑定 loopback",
          ctl)
    check(bool(cfg.get("secret")), "external-controller 有 secret")
    dns = cfg.get("dns") or {}
    check(dns.get("listen", "") == "", "dns.listen 为空",
          "-> 不为系统开 DNS 端口（DNS 只在本进程内用）")
    check(dns.get("enhanced-mode") != "fake-ip", "没有用 fake-ip 模式")

    check(core.audit_config(cfg) == [], "audit_config 认为该配置干净")

    # ------------------------------------------------------------------
    section("3. audit_config 能抓住故意做坏的配置")
    bad = dict(cfg)
    bad["tun"] = {"enable": True}
    check(bool(core.audit_config(bad)), "抓到 tun.enable=true")
    bad2 = dict(cfg); bad2["allow-lan"] = True
    check(bool(core.audit_config(bad2)), "抓到 allow-lan=true")
    bad3 = dict(cfg); bad3["redir-port"] = 7892
    check(bool(core.audit_config(bad3)), "抓到 redir-port")
    bad4 = dict(cfg); bad4["bind-address"] = "0.0.0.0"
    check(bool(core.audit_config(bad4)), "抓到 bind-address=0.0.0.0")
    bad5 = dict(cfg); bad5["dns"] = dict(dns, listen="0.0.0.0:53")
    check(bool(core.audit_config(bad5)), "抓到 dns.listen")

    # ------------------------------------------------------------------
    section("4. 源码里没有任何写系统设置的代码")
    banned = [
        (r"\bimport\s+winreg\b", "import winreg"),
        (r"Internet Settings", "写 WinINET 代理注册表"),
        (r"ProxyEnable|ProxyServer|AutoConfigURL", "改系统代理"),
        (r"\bnetsh\b", "netsh"),
        (r"SetEnvironmentVariable", "改系统环境变量"),
        (r"HTTP_PROXY|HTTPS_PROXY|ALL_PROXY", "设置全局代理环境变量"),
        (r"\broute\s+add\b", "改路由表"),
        (r"sc\s+create|schtasks", "装服务/计划任务"),
    ]
    py_files = []
    for base, _dirs, files in os.walk(os.path.join(ROOT, "src")):
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(base, f))
    py_files.append(os.path.join(ROOT, "main.py"))
    for pattern, label in banned:
        hits = []
        for p in py_files:
            for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                if re.search(pattern, line):
                    hits.append("%s:%d" % (os.path.basename(p), i))
        check(not hits, "源码里没有: %s" % label, ", ".join(hits[:3]))

    # ------------------------------------------------------------------
    section("5. 我们自己的 HTTP 会话：不走系统代理、代理只挂在本进程会话上")
    check(HTTP.session(use_proxy=False).trust_env is False,
          "requests session trust_env=False",
          "-> 不会读取系统/环境里的代理设置")

    # ------------------------------------------------------------------
    section("6. 实机验证：mihomo 起来后只监听 loopback")
    core.start()
    if not core.running:
        check(False, "mihomo 启动（后续实机检查跳过）")
    else:
        time.sleep(1.5)
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetTCPConnection -State Listen | "
                 "Where-Object { $_.OwningProcess -in "
                 "(Get-Process mihomo -ErrorAction SilentlyContinue).Id } | "
                 "ForEach-Object { \"$($_.LocalAddress):$($_.LocalPort)\" }"],
                capture_output=True, timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            listeners = [x for x in out.stdout.decode("utf-8", "replace").split()
                         if ":" in x]
        except Exception as exc:  # noqa: BLE001
            listeners = []
            print("      (listener probe failed: %s)" % exc)

        print("      mihomo listeners: %s" % (listeners or "(none seen)"))
        non_loop = [l for l in listeners
                    if not (l.startswith("127.0.0.1") or l.startswith("[::1]")
                            or l.startswith("0.0.0.0:0"))]
        check(not non_loop, "所有监听地址都是 loopback", ", ".join(non_loop))

        # the generated config on disk must also say tun is off
        disk = open(core.config_path, encoding="utf-8").read()
        check("enable: false" in disk and "tun:" in disk,
              "落盘的 config.yaml 里 tun.enable 是 false")

        # the machine's own WinINET proxy setting must be untouched
        try:
            reg = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\"
                 "CurrentVersion\\Internet Settings').ProxyServer"],
                capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proxyserver = reg.stdout.decode("utf-8", "replace").strip()
        except Exception:
            proxyserver = ""
        print("      system ProxyServer = %r" % proxyserver)
        check(proxyserver not in ("127.0.0.1:17895", "127.0.0.1:%d" % core.mixed_port),
              "程序没有把自己的端口写进系统代理设置")
        core.stop()

    # ------------------------------------------------------------------
    section("7. 孤儿清理：只杀我们自己启动的 mihomo，绝不误伤别的进程")

    def alive(pid: int) -> bool:
        o = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Id %d -ErrorAction SilentlyContinue).Id" % pid],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return o.stdout.decode("utf-8", "replace").strip() != ""

    wd = r"C:\tmp\tsds_orphan_test"
    os.makedirs(wd, exist_ok=True)
    seed_core = MihomoCore(binary=os.path.join(ROOT, "src", "resources", "mihomo.exe"),
                           workdir=wd, mixed_port=17897, controller_port=19097)
    seed_core.load_cached()
    seed_core.proxies = seed_core.prune_invalid(seed_core.proxies)
    seed_core._pick_ports()
    seed_core.write_config(seed_core.proxies)
    orphan = subprocess.Popen([seed_core.binary, "-d", wd, "-f", seed_core.config_path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    check(alive(orphan.pid), "孤儿 mihomo 已在后台运行")

    cleaner = MihomoCore(binary=os.path.join(ROOT, "src", "resources", "mihomo.exe"),
                         workdir=wd, mixed_port=17897, controller_port=19097)
    with open(cleaner.pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(orphan.pid))
    cleaner._kill_orphan()
    time.sleep(1.5)
    check(not alive(orphan.pid), "上一轮遗留的 mihomo 被自动清理")
    try:
        orphan.kill()
    except Exception:
        pass

    me = os.getpid()
    with open(cleaner.pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(me))
    cleaner._kill_orphan()
    time.sleep(0.8)
    check(alive(me), "pidfile 指向无关进程时不会乱杀（本例是当前 python）")

    launcher = subprocess.Popen(["powershell", "-NoProfile", "-Command",
                                 "Start-Sleep -Seconds 30"])
    time.sleep(1.5)
    with open(cleaner.pid_path, "w", encoding="utf-8") as fh:
        fh.write(str(launcher.pid))
    cleaner._kill_orphan()
    time.sleep(1.0)
    check(alive(launcher.pid), "pidfile 指向无关 powershell 时同样不动它")
    try:
        launcher.kill()
    except Exception:
        pass

    print("\n" + "=" * 78)
    print("PASS %d   FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   FAILED: %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
