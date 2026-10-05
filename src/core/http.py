"""Shared HTTP plumbing: sessions, proxy routing, retries, UA rotation.

Everything that talks to the outside world goes through here so that the
embedded mihomo proxy can be switched on/off in exactly one place.

Two transports are used, in order:

1. ``requests`` -- fast, keeps connections alive, handles redirects/gzip
2. the OS ``curl`` (present on Windows 10+) -- a *completely different* TLS
   stack (schannel) which gets through the several Chinese/Cloudflare hosts
   that silently drop python's OpenSSL fingerprint

Without the second transport a large fraction of the torrent indexes are
simply unreachable, so the fallback is not optional.

Every request also queues at :mod:`src.core.gateway` before it is sent.  That
is where the concurrency limit lives: capping *sources* or *rows* never bounded
traffic, because one row fans out into a dozen tracker scrapes.
"""
from __future__ import annotations

import json as _json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

from . import gateway

log = logging.getLogger("tsearch.http")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

try:  # optional dependency, used only when installed
    from urllib3.util.retry import Retry  # type: ignore
except Exception:  # pragma: no cover
    Retry = None  # type: ignore


def _redact(url: str) -> str:
    """URL with the query string removed, for logging.

    Search keywords travel in the query string (``.../search?q=火影忍者``), and
    the program promises not to keep a record of what was searched -- a log
    line is a record.  The host and path are kept, which is all a traceback of
    "this index failed" needs.
    """
    head, sep, _tail = str(url or "").partition("?")
    return head + "?<已隐去>" if sep else head


class _TimeoutAdapter(HTTPAdapter):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)


class CurlResponse:
    """Minimal stand-in for ``requests.Response`` for the curl transport."""

    __slots__ = ("content", "status_code", "headers", "url", "_encoding")

    def __init__(self, content: bytes, status: int, headers: dict, url: str) -> None:
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self.url = url
        self._encoding: Optional[str] = None

    @property
    def encoding(self) -> Optional[str]:
        return self._encoding

    @encoding.setter
    def encoding(self, value) -> None:
        self._encoding = value

    @property
    def apparent_encoding(self) -> str:
        ct = str(self.headers.get("content-type", ""))
        if "charset=" in ct:
            return ct.split("charset=", 1)[1].split(";")[0].strip()
        return "utf-8"

    @property
    def text(self) -> str:
        enc = self._encoding or self.apparent_encoding or "utf-8"
        try:
            return self.content.decode(enc, "replace")
        except LookupError:
            return self.content.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.text)

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def close(self) -> None:
        pass


def _find_curl() -> Optional[str]:
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if exe:
        return exe
    for cand in (r"C:\Windows\System32\curl.exe",
                 r"C:\Windows\SysWOW64\curl.exe"):
        if os.path.isfile(cand):
            return cand
    return None


CURL = _find_curl()


class Http:
    """Process-wide HTTP facade.

    ``proxy`` is a mapping such as ``{"http": "http://127.0.0.1:7890",
    "https": "http://127.0.0.1:7890"}`` or ``None`` for direct access.
    """

    _lock = threading.RLock()
    _local = threading.local()

    def __init__(self) -> None:
        self._proxy: Optional[Dict[str, str]] = None
        self._sessions: Dict[str, requests.Session] = {}
        self._direct_fallback = True
        self._stats = {"ok": 0, "fail": 0, "proxy_used": 0, "direct_used": 0}

    # -- proxy ---------------------------------------------------------
    @property
    def proxy(self) -> Optional[Dict[str, str]]:
        return self._proxy

    def set_proxy(self, proxy: Optional[Dict[str, str]]) -> None:
        with self._lock:
            if proxy != self._proxy:
                log.info("http proxy -> %s", proxy)
            self._proxy = proxy
            self._sessions.clear()

    def set_direct_fallback(self, enabled: bool) -> None:
        self._direct_fallback = bool(enabled)

    # -- sessions ------------------------------------------------------
    def _session(self, use_proxy: bool) -> requests.Session:
        key = "p" if use_proxy else "d"
        sess = self._sessions.get(key)
        if sess is not None:
            return sess
        sess = requests.Session()
        sess.trust_env = False
        sess.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        if Retry is not None:
            # Deliberately no *read* retry: a stalled read doubles the wall
            # clock for every slow index, and the curl fallback in :meth:`get`
            # already provides the second chance with a different TLS stack.
            r = Retry(total=1, connect=1, read=0, redirect=0,
                      status=1, backoff_factor=0.3,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "HEAD", "POST"]))
            sess.mount("http://", _TimeoutAdapter(max_retries=r, pool_maxsize=24))
            sess.mount("https://", _TimeoutAdapter(max_retries=r, pool_maxsize=24))
        else:  # pragma: no cover
            sess.mount("http://", _TimeoutAdapter(pool_maxsize=24))
            sess.mount("https://", _TimeoutAdapter(pool_maxsize=24))
        if use_proxy and self._proxy:
            sess.proxies.update(self._proxy)
        self._sessions[key] = sess
        return sess

    def session(self, use_proxy: Optional[bool] = None) -> requests.Session:
        if use_proxy is None:
            use_proxy = bool(self._proxy)
        return self._session(bool(use_proxy))

    # -- request -------------------------------------------------------
    def curl_get(self, url: str, *, timeout: float = 12.0,
                 headers: Optional[dict] = None,
                 params: Optional[dict] = None,
                 use_proxy: bool = False,
                 max_bytes: int = 8 << 20) -> Optional[CurlResponse]:
        """Fetch with the OS curl (schannel TLS).  Returns ``None`` on failure."""
        if not CURL:
            return None
        if params:
            import urllib.parse
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        hdrs = dict(headers or {})
        hdrs.setdefault("User-Agent", random.choice(USER_AGENTS))
        hdrs.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        fd, tmp = tempfile.mkstemp(prefix="tsds", suffix=".bin")
        os.close(fd)
        args = [CURL, "-sS", "-L", "--compressed",
                "--max-time", str(int(max(4, timeout))),
                "-o", tmp, "-w", "%{http_code}|%{content_type}",
                "--max-filesize", str(int(max_bytes))]
        for k, v in hdrs.items():
            args += ["-H", "%s: %s" % (k, v)]
        if use_proxy and self._proxy:
            args += ["-x", self._proxy.get("http") or self._proxy.get("https") or ""]
        args.append(url)
        try:
            proc = subprocess.run(
                args, capture_output=True, timeout=timeout + 12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (proc.stdout or b"").decode("utf-8", "replace").strip()
            code_s, _, ctype = out.partition("|")
            try:
                code = int(code_s)
            except ValueError:
                code = 0
            if code == 0:
                return None
            try:
                with open(tmp, "rb") as fh:
                    body = fh.read(max_bytes)
            except OSError:
                body = b""
            with self._lock:
                self._stats["ok"] += 1
                self._stats["proxy_used" if use_proxy else "direct_used"] += 1
            return CurlResponse(body, code, {"content-type": ctype}, url)
        except (OSError, subprocess.SubprocessError):
            with self._lock:
                self._stats["fail"] += 1
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def get(self, url: str, *, timeout: float = 12.0, headers: Optional[dict] = None,
            params: Optional[dict] = None, allow_direct_fallback: bool = True,
            use_curl_fallback: bool = True,
            budget: Optional[float] = None, **kw) -> Optional[requests.Response]:
        """GET with transparent transport fallbacks.

        Order: proxy via requests -> direct via requests -> proxy via curl ->
        direct via curl.  Returns ``None`` instead of raising, because every
        caller is a best-effort metadata scraper inside a thread pool.

        ``budget`` caps the *whole* chain, not just one attempt.  Callers pass
        ``timeout`` expecting that to be the cost of the call, but four
        transports plus urllib3's connect retry turned one unreachable index
        into ~50s -- and with two searches in flight that long tail is exactly
        what made the second one look stuck.  Default is ``timeout * 1.6``,
        so a dead source costs about 20s and the healthy ones are untouched.
        """
        attempts = []
        if self._proxy:
            attempts.append(True)
        attempts.append(False)
        disc = self._proxy is not None and allow_direct_fallback and self._direct_fallback
        if not disc:
            attempts = attempts[:1]

        deadline = time.monotonic() + (budget if budget is not None
                                       else max(timeout * 1.6, timeout + 2.0))
        last_err: Optional[Exception] = None
        for use_proxy in attempts:
            left = deadline - time.monotonic()
            if left <= 0.5:
                break
            # Queue for the exit here, not before the loop: queueing is part of
            # what the caller is paying for, so it comes out of the same budget.
            with gateway.slot(min(timeout, left), url) as h:
                if h is None:
                    break        # exit jammed, or the phase is already over
                left = deadline - time.monotonic()   # re-read: queueing cost
                if left <= 0.5:
                    break
                try:
                    hdrs = dict(headers or {})
                    hdrs.setdefault("User-Agent", random.choice(USER_AGENTS))
                    r = self._session(use_proxy).get(
                        url, timeout=min(timeout, left), headers=hdrs,
                        params=params, **kw)
                    if r.status_code >= 400:
                        last_err = RuntimeError("HTTP %s" % r.status_code)
                        with self._lock:
                            self._stats["fail"] += 1
                        if r.status_code in (403, 429, 451, 502, 503, 504) and \
                                (disc or use_curl_fallback):
                            continue
                        return r
                    with self._lock:
                        self._stats["ok"] += 1
                        self._stats["proxy_used" if use_proxy else "direct_used"] += 1
                    return r
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    with self._lock:
                        self._stats["fail"] += 1
                    continue

        if use_curl_fallback and CURL and not kw.get("stream"):
            for use_proxy in attempts:
                left = deadline - time.monotonic()
                if left <= 0.5:
                    break
                # curl --max-time has a 4s floor, so do not start it at all
                # unless what is left can actually pay for it
                if left < 4.0:
                    break
                with gateway.slot(min(timeout, left), url) as h:
                    if h is None:
                        break
                    left = deadline - time.monotonic()
                    if left < 4.0:
                        break
                    cr = self.curl_get(url, timeout=min(timeout, left),
                                       headers=headers,
                                       params=params, use_proxy=use_proxy)
                    if cr is not None:
                        if cr.status_code >= 400:
                            last_err = RuntimeError("HTTP %s" % cr.status_code)
                            continue
                        return cr  # type: ignore[return-value]

        if last_err:
            log.debug("GET %s failed: %s", _redact(url), last_err)
        return None

    def get_text(self, url: str, **kw) -> str:
        r = self.get(url, **kw)
        if r is None:
            return ""
        try:
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception:
            try:
                return r.content.decode("utf-8", "replace")
            except Exception:
                return ""

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        if r is None:
            return None
        try:
            return r.json()
        except Exception:
            try:
                import json
                return json.loads(r.content.decode("utf-8", "replace"))
            except Exception:
                return None

    def get_bytes(self, url: str, *, max_bytes: int = 4 << 20, **kw) -> bytes:
        r = self.get(url, stream=True, **kw)
        if r is None:
            return b""
        try:
            chunks = []
            total = 0
            for chunk in r.iter_content(65536):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            return b"".join(chunks)
        except Exception:
            return b""
        finally:
            try:
                r.close()
            except Exception:
                pass

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)


HTTP = Http()


def proxy_dict(port: int, host: str = "127.0.0.1") -> Dict[str, str]:
    url = "http://%s:%d" % (host, port)
    return {"http": url, "https": url}


def wait_for_port(host: str, port: int, timeout: float = 20.0,
                  interval: float = 0.15) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return True
        except OSError:
            time.sleep(interval)
    return False
