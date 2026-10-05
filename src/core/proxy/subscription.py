# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Subscription fetching and parsing.

Accepts everything a user is likely to paste:

* a full Clash / mihomo YAML document (``proxies:`` list, groups, rules)
* a base64 blob of newline separated node URIs
* a plain newline separated node URI list
* a single node URI

Node URIs understood: ``ss``, ``ssr``(best effort), ``vmess``, ``vless``,
``trojan``, ``hysteria``/``hysteria2``/``hy2``, ``tuic``, ``socks5``,
``http``/``https``.

Everything is normalised into mihomo ``proxies:`` dictionaries so the config
generator only ever deals with one shape.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..http import HTTP

log = logging.getLogger("tsearch.subscription")

DEFAULT_SUBSCRIPTIONS: Tuple[str, ...] = ()

#: Headers that make most providers hand back a Clash document.
SUB_HEADERS = {
    "User-Agent": ("clash-verge/v2.0.3 Mihomo/1.19.32 "
                   "Clash.Meta/1.19.32 mihomo/1.19.32"),
    "Accept": "*/*",
}

#: Rotated between retries -- some providers drop connections based on the
#: TLS/UA fingerprint they see, so varying it meaningfully improves success.
USER_AGENT_POOL = (
    "clash-verge/v2.0.3 Mihomo/1.19.32 Clash.Meta/1.19.32 mihomo/1.19.32",
    "ClashforWindows/0.19.23",
    "Clash.Meta/1.19.0",
    "v2rayN/6.0",
    "mihomo/1.19.32",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _b64(data: str) -> bytes:
    s = re.sub(r"\s+", "", data or "")
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * ((-len(s)) % 4)
    return base64.b64decode(s, validate=False)


def _maybe_b64_text(text: str) -> Optional[str]:
    stripped = re.sub(r"\s+", "", text or "")
    if len(stripped) < 16:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", stripped):
        return None
    try:
        raw = _b64(stripped)
        decoded = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if "://" in decoded:
        return decoded
    return None


def _split_uri(uri: str) -> Tuple[str, str, str]:
    """-> (scheme, body, fragment-name)"""
    scheme, _, rest = uri.partition("://")
    rest, _, frag = rest.partition("#")
    return scheme.lower(), rest, urllib.parse.unquote(frag)


def _qs(body: str) -> Tuple[str, Dict[str, str]]:
    """Split ``head?k=v&k2=v2`` into ``(head, params)``."""
    if "?" not in body:
        return body, {}
    head, _, query = body.partition("?")
    params: Dict[str, str] = {}
    # NOTE: re.findall returns one tuple *per group*, so this must unpack two
    # values -- unpacking three silently broke every scheme in this module.
    for k, v in re.findall(r"([^&=]+)=?([^&]*)", query):
        key = urllib.parse.unquote(k).strip()
        if key:
            params[key] = urllib.parse.unquote(v)
    return head, params


def _hostport(s: str) -> Tuple[str, int]:
    s = s.strip().strip("/")
    # strip any trailing query/fragment leftovers
    s = s.split("?")[0].split("#")[0]
    if s.startswith("["):                      # IPv6
        host, _, port = s.partition("]")
        return host.lstrip("["), int((port.lstrip(":") or "0") or 0)
    if ":" in s:
        host, _, port = s.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return s, 0
    return s, 0


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "tls", "reality")


# ---------------------------------------------------------------------------
# URI parsers
# ---------------------------------------------------------------------------

def parse_ss(uri: str) -> Optional[Dict[str, Any]]:
    scheme, body, name = _split_uri(uri)
    if scheme == "ssr":
        return None
    # two dialects:
    #   ss://base64(method:pass)@host:port#name
    #   ss://base64(method:pass@host:port)#name
    if "@" in body:
        userinfo, _, hostpart = body.partition("@")
        try:
            userinfo = _b64(userinfo).decode("utf-8", "replace") if ":" not in userinfo else userinfo
        except Exception:
            pass
    else:
        try:
            decoded = _b64(body).decode("utf-8", "replace")
        except Exception:
            return None
        userinfo, _, hostpart = decoded.rpartition("@")
    if ":" not in userinfo:
        return None
    method, _, password = userinfo.partition(":")
    host, port = _hostport(hostpart)
    if not host or not port:
        return None
    return {
        "name": name or ("%s:%d" % (host, port)),
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def parse_vmess(uri: str) -> Optional[Dict[str, Any]]:
    _, body, name = _split_uri(uri)
    try:
        cfg = json.loads(_b64(body).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    host = str(cfg.get("add") or "").strip()
    try:
        port = int(cfg.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or not port:
        return None
    net = str(cfg.get("net") or "tcp").lower()
    tls = str(cfg.get("tls") or "").lower() in ("tls", "true", "1", "reality")
    out: Dict[str, Any] = {
        "name": str(cfg.get("ps") or name or ("%s:%d" % (host, port))),
        "type": "vmess",
        "server": host,
        "port": port,
        "uuid": str(cfg.get("id") or ""),
        "alterId": int(cfg.get("aid") or 0),
        "cipher": str(cfg.get("scy") or cfg.get("security") or "auto"),
        "udp": True,
    }
    if tls:
        out["tls"] = True
        sni = str(cfg.get("sni") or cfg.get("host") or "").strip()
        if sni:
            out["servername"] = sni
    if net in ("ws", "httpupgrade"):
        out["network"] = net
        opts: Dict[str, Any] = {"path": str(cfg.get("path") or "/")}
        h = str(cfg.get("host") or "").strip()
        if h:
            opts["headers"] = {"Host": h}
        out["ws-opts"] = opts
    elif net == "grpc":
        out["network"] = "grpc"
        out["grpc-opts"] = {"grpc-service-name": str(cfg.get("path") or "")}
    elif net == "h2":
        out["network"] = "h2"
        out["h2-opts"] = {"path": [str(cfg.get("path") or "/")],
                          "host": [str(cfg.get("host") or "")]}
    return out


def parse_vless(uri: str) -> Optional[Dict[str, Any]]:
    _, body, name = _split_uri(uri)
    head, params = _qs(body)
    uuid, _, hostpart = head.rpartition("@")
    host, port = _hostport(hostpart)
    if not host or not port or not uuid:
        return None
    security = (params.get("security") or "none").lower()
    out: Dict[str, Any] = {
        "name": name or ("%s:%d" % (host, port)),
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": uuid,
        "udp": True,
        "network": (params.get("type") or "tcp").lower(),
        "tls": security in ("tls", "reality", "xtls"),
        "flow": params.get("flow") or "",
        "skip-cert-verify": _truthy(params.get("allowInsecure", "0")),
    }
    if not out["flow"]:
        out.pop("flow")
    sni = params.get("sni") or params.get("peer") or ""
    if sni:
        out["servername"] = sni
    if security == "reality":
        out["reality-opts"] = {}
        if params.get("pbk"):
            out["reality-opts"]["public-key"] = params["pbk"]
        if params.get("sid"):
            out["reality-opts"]["short-id"] = params["sid"]
        if not out["reality-opts"]:
            out.pop("reality-opts")
        out["client-fingerprint"] = params.get("fp") or "chrome"
    fp = params.get("fp")
    if fp and "client-fingerprint" not in out:
        out["client-fingerprint"] = fp
    net = out["network"]
    if net in ("ws", "httpupgrade", "splithttp"):
        out["ws-opts"] = {"path": params.get("path") or "/"}
        h = params.get("host")
        if h:
            out["ws-opts"]["headers"] = {"Host": h}
        if net != "ws":
            out["network"] = net
    elif net == "grpc":
        out["grpc-opts"] = {"grpc-service-name": params.get("serviceName") or ""}
    elif net == "tcp" and not out.get("flow"):
        pass
    if not params.get("type") or params.get("type") == "tcp":
        out["network"] = "tcp"
    return out


def parse_trojan(uri: str) -> Optional[Dict[str, Any]]:
    _, body, name = _split_uri(uri)
    head, params = _qs(body)
    password, _, hostpart = head.rpartition("@")
    host, port = _hostport(hostpart)
    if not host or not port:
        return None
    out: Dict[str, Any] = {
        "name": name or ("%s:%d" % (host, port)),
        "type": "trojan",
        "server": host,
        "port": port,
        "password": urllib.parse.unquote(password),
        "udp": True,
        "skip-cert-verify": _truthy(params.get("allowInsecure", "0")),
    }
    sni = params.get("sni") or params.get("peer")
    if sni:
        out["sni"] = sni
    if params.get("type") in ("ws", "grpc"):
        net = params["type"]
        out["network"] = net
        if net == "ws":
            out["ws-opts"] = {"path": params.get("path") or "/"}
            if params.get("host"):
                out["ws-opts"]["headers"] = {"Host": params["host"]}
        else:
            out["grpc-opts"] = {"grpc-service-name": params.get("serviceName") or ""}
    return out


def parse_hysteria2(uri: str) -> Optional[Dict[str, Any]]:
    scheme, body, name = _split_uri(uri)
    head, params = _qs(body)
    auth, _, hostpart = head.rpartition("@")
    host, port = _hostport(hostpart)
    if not host or not port:
        return None
    out: Dict[str, Any] = {
        "name": name or ("%s:%d" % (host, port)),
        "type": "hysteria2",
        "server": host,
        "port": port,
        "password": urllib.parse.unquote(auth),
        "skip-cert-verify": _truthy(params.get("insecure", "0")),
    }
    if params.get("sni"):
        out["sni"] = params["sni"]
    if params.get("obfs"):
        out["obfs"] = params["obfs"]
        out["obfs-password"] = params.get("obfs-password", "")
    return out


def parse_tuic(uri: str) -> Optional[Dict[str, Any]]:
    _, body, name = _split_uri(uri)
    head, params = _qs(body)
    userinfo, _, hostpart = head.rpartition("@")
    host, port = _hostport(hostpart)
    if not host or not port:
        return None
    uuid, _, password = userinfo.partition(":")
    out: Dict[str, Any] = {
        "name": name or ("%s:%d" % (host, port)),
        "type": "tuic",
        "server": host,
        "port": port,
        "uuid": uuid,
        "password": urllib.parse.unquote(password),
        "skip-cert-verify": _truthy(params.get("allow_insecure", "0")),
        "udp-relay-mode": params.get("udp_relay_mode", "native"),
        "congestion-controller": params.get("congestion_control", "bbr"),
    }
    if params.get("sni"):
        out["sni"] = params["sni"]
    return out


def parse_plain_proxy(uri: str) -> Optional[Dict[str, Any]]:
    scheme, body, name = _split_uri(uri)
    if scheme not in ("socks5", "socks", "http", "https"):
        return None
    body, params = _qs(body)
    userinfo, _, hostpart = body.rpartition("@")
    host, port = _hostport(hostpart)
    if not host or not port:
        return None
    out: Dict[str, Any] = {
        "name": name or ("%s:%d" % (host, port)),
        "type": "socks5" if scheme.startswith("socks") else "http",
        "server": host,
        "port": port,
        "udp": True,
    }
    if userinfo and ":" in userinfo:
        u, _, p = userinfo.partition(":")
        out["username"] = urllib.parse.unquote(u)
        out["password"] = urllib.parse.unquote(p)
    if scheme == "https" or _truthy(params.get("tls", "0")):
        out["tls"] = True
    return out


_URI_PARSERS = {
    "ss": parse_ss,
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "hysteria": parse_hysteria2,
    "tuic": parse_tuic,
    "socks5": parse_plain_proxy,
    "socks": parse_plain_proxy,
    "http": parse_plain_proxy,
    "https": parse_plain_proxy,
}


def parse_node_uri(uri: str) -> Optional[Dict[str, Any]]:
    uri = (uri or "").strip()
    if "://" not in uri:
        return None
    scheme = uri.split("://", 1)[0].lower()
    fn = _URI_PARSERS.get(scheme)
    if fn is None:
        return None
    try:
        return fn(uri)
    except Exception as exc:  # noqa: BLE001
        log.debug("cannot parse node uri %s: %s", scheme, exc)
        return None


# ---------------------------------------------------------------------------
# document parsing
# ---------------------------------------------------------------------------

def parse_clash_yaml(text: str) -> Optional[Dict[str, Any]]:
    obj = None
    try:
        obj = yaml.safe_load(text)
    except Exception:
        obj = None
    if not isinstance(obj, dict):
        return None
    proxies = obj.get("proxies")
    if not isinstance(proxies, list):
        return None
    clean = []
    for p in proxies:
        if not isinstance(p, dict):
            continue
        if not p.get("server") or not p.get("port") or not p.get("type"):
            continue
        p = dict(p)
        try:
            p["port"] = int(p["port"])
        except (TypeError, ValueError):
            continue
        p.setdefault("name", "%s:%s" % (p["server"], p["port"]))
        clean.append(p)
    if not clean:
        return None
    out: Dict[str, Any] = {"proxies": clean}
    # NOTE: `tun` is deliberately NOT carried over. This app runs mihomo purely
    # as a loopback proxy for its own requests; importing a profile that had TUN
    # enabled must never be able to turn on a virtual adapter or system routes.
    # The config that actually runs is generated from `proxies` alone.
    for key in ("proxy-groups", "rules", "rule-providers", "dns",
                "mixed-port", "external-controller", "mode", "log-level",
                "allow-lan", "ipv6", "sniffer", "geodata-mode"):
        if key in obj:
            out[key] = obj[key]
    return out


def parse_subscription(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of any subscription payload."""
    if not text or not text.strip():
        return None
    body = text.strip()

    parsed = parse_clash_yaml(body)
    if parsed:
        return parsed

    decoded = _maybe_b64_text(body)
    if decoded:
        parsed = parse_clash_yaml(decoded)
        if parsed:
            return parsed
        body = decoded

    nodes: List[Dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        node = parse_node_uri(line)
        if node:
            nodes.append(node)
    if nodes:
        return {"proxies": nodes}
    return None


def parse_pasted_content(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse anything a user pastes into the import box.

    Handles, in order:

    * a complete mihomo / Clash config document (what FlClash, Clash Verge,
      mihomo itself and most clients export) -- only ``proxies`` is used, the
      rest is regenerated so auto fail-over keeps working
    * a bare ``proxies:`` YAML fragment
    * a base64 blob of node URIs
    * a plain list of ``ss:// vmess:// vless:// trojan:// hysteria2:// ...``

    Returns ``(proxies, error)``; ``error`` is ``""`` on success.
    """
    if not text or not text.strip():
        return [], "内容为空"

    raw = text.strip()
    # tolerate the leading pipe some users copy out of a markdown code block
    raw = "\n".join(
        re.sub(r"^\s*\|\s?", "", ln) for ln in raw.splitlines())

    parsed = parse_subscription(raw)
    if not parsed:
        # maybe a document whose top level is just the proxy list
        try:
            obj = yaml.safe_load(raw)
        except Exception:
            obj = None
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            parsed = {"proxies": obj}
        elif isinstance(obj, dict) and isinstance(obj.get("proxies"), list):
            parsed = {"proxies": obj["proxies"]}
    if not parsed:
        return [], "无法识别的内容格式（既不是 Clash/mihomo 配置，也不是节点链接列表）"

    proxies = dedupe_proxies(parsed.get("proxies") or [])
    if not proxies:
        return [], "内容里没有可用节点（proxies 为空或字段不完整）"
    return proxies, ""


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def _fetch_via_requests(url: str, timeout: float, use_proxy: bool,
                        ua: str) -> str:
    headers = dict(SUB_HEADERS)
    headers["User-Agent"] = ua
    r = HTTP.get(url, timeout=timeout, headers=headers,
                 allow_direct_fallback=use_proxy)
    if r is None or r.status_code >= 400:
        return ""
    try:
        return r.content.decode("utf-8", "replace")
    except Exception:
        return r.text or ""


def _fetch_via_curl(url: str, timeout: float, proxy_url: str = "") -> str:
    """Fallback transport.

    Windows ships ``curl.exe`` and its schannel TLS stack gets through
    several CDN/WAF combinations that python's OpenSSL fingerprint does not.
    """
    import shutil
    import subprocess
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        return ""
    args = [exe, "-sS", "-L", "--max-time", str(int(max(5, timeout))),
            "--compressed", "-A", USER_AGENT_POOL[0], url]
    if proxy_url:
        args[1:1] = ["-x", proxy_url]
    try:
        proc = subprocess.run(args, capture_output=True,
                              timeout=timeout + 10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return proc.stdout.decode("utf-8", "replace")


def fetch_subscription(url: str, timeout: float = 25.0, attempts: int = 3,
                       use_proxy: bool = True) -> Optional[Dict[str, Any]]:
    """Download + parse one subscription URL.  Never raises.

    Providers of this kind are extremely flaky (connection resets, 403s,
    multi-second stalls), so each attempt rotates the User-Agent and falls
    back from ``requests`` to the OS ``curl`` before giving up.
    """
    if not url:
        return None
    import random
    proxy_url = ""
    if use_proxy and HTTP.proxy:
        proxy_url = HTTP.proxy.get("http") or ""

    for attempt in range(max(1, attempts)):
        ua = USER_AGENT_POOL[attempt % len(USER_AGENT_POOL)]
        text = ""
        try:
            text = _fetch_via_requests(url, timeout, use_proxy, ua)
        except Exception:
            text = ""
        parsed = parse_subscription(text) if text else None
        if parsed:
            parsed["_source_url"] = url
            return parsed

        if attempt + 1 < attempts or not text:
            text = _fetch_via_curl(url, timeout, proxy_url)
            parsed = parse_subscription(text) if text else None
            if parsed:
                parsed["_source_url"] = url
                return parsed
        log.info("subscription attempt %d/%d failed: %s", attempt + 1,
                 attempts, "<订阅地址已隐去>")
        time.sleep(0.6 + random.random() * 0.8)
    return None


def dedupe_proxies(proxies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop duplicates / junk and make names unique."""
    out: List[Dict[str, Any]] = []
    seen_keys = set()
    used_names = set()
    for p in proxies:
        if not isinstance(p, dict):
            continue
        server = str(p.get("server") or "").strip()
        try:
            port = int(p.get("port") or 0)
        except (TypeError, ValueError):
            continue
        ptype = str(p.get("type") or "").strip().lower()
        if not server or not port or not ptype:
            continue
        if ptype in ("direct", "reject", "dns", "compatible"):
            continue
        key = (ptype, server, port, str(p.get("uuid") or p.get("password") or ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        p = dict(p)
        name = str(p.get("name") or "").strip() or ("%s:%d" % (server, port))
        name = name.replace("\u200b", "").strip()
        base = name
        i = 2
        while name in used_names:
            name = "%s #%d" % (base, i)
            i += 1
        used_names.add(name)
        p["name"] = name
        out.append(p)
    return out
