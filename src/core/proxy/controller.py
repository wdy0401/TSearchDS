"""Client for mihomo's External Controller REST API.

Endpoints used (see https://wiki.metacubex.one/en/config/controller/):

* ``GET  /version``                            -> ``{"version": ..., "meta": true}``
* ``GET  /configs``                            -> running config summary
* ``PUT  /configs?force=true`` (JSON body)     -> reload/replace the config
* ``GET  /proxies``                            -> every proxy + group and their
                                                  ``history`` (delay samples)
* ``GET  /proxies/{name}``                     -> one proxy/group
* ``GET  /proxies/{name}/delay``               -> live delay test
                                                  ``?timeout=3000&url=...``
* ``PUT  /proxies/{group}`` body ``{"name": n}`` -> change a ``select`` group

If ``secret`` is configured every call needs
``Authorization: Bearer <secret>``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("tsearch.mihomo")


class MihomoController:
    def __init__(self, host: str = "127.0.0.1", port: int = 9090,
                 secret: str = "", timeout: float = 6.0) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.timeout = timeout
        self._sess = requests.Session()
        self._sess.trust_env = False
        # a delay sweep fires one request per node concurrently
        adapter = requests.adapters.HTTPAdapter(pool_connections=4,
                                                pool_maxsize=64,
                                                max_retries=0)
        self._sess.mount("http://", adapter)
        self._sess.mount("https://", adapter)

    # -- plumbing ------------------------------------------------------
    @property
    def base(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.secret:
            h["Authorization"] = "Bearer " + self.secret
        return h

    def _req(self, method: str, path: str, **kw) -> Optional[requests.Response]:
        url = self.base + path
        kw.setdefault("timeout", self.timeout)
        try:
            return self._sess.request(method, url, headers=self._headers(), **kw)
        except requests.RequestException as exc:
            log.debug("controller %s %s failed: %s", method, path, exc)
            return None

    # -- API -----------------------------------------------------------
    def alive(self) -> bool:
        r = self._req("GET", "/version")
        return r is not None and r.status_code == 200

    def version(self) -> str:
        r = self._req("GET", "/version")
        if r is None or r.status_code != 200:
            return ""
        try:
            return str(r.json().get("version") or "")
        except Exception:
            return ""

    def configs(self) -> Dict[str, Any]:
        r = self._req("GET", "/configs")
        if r is None or r.status_code != 200:
            return {}
        try:
            return r.json() or {}
        except Exception:
            return {}

    def proxies(self) -> Dict[str, Any]:
        r = self._req("GET", "/proxies")
        if r is None or r.status_code != 200:
            return {}
        try:
            return (r.json() or {}).get("proxies") or {}
        except Exception:
            return {}

    def proxy(self, name: str) -> Dict[str, Any]:
        r = self._req("GET", "/proxies/" + _q(name))
        if r is None or r.status_code != 200:
            return {}
        try:
            return r.json() or {}
        except Exception:
            return {}

    def delay(self, name: str, timeout_ms: int = 3000,
              url: str = "http://www.gstatic.com/generate_204") -> Optional[int]:
        """Live delay test.  ``None`` means dead; otherwise milliseconds."""
        r = self._req("GET", "/proxies/%s/delay" % _q(name),
                      params={"timeout": timeout_ms, "url": url},
                      timeout=max(self.timeout, timeout_ms / 1000.0 + 2.0))
        if r is None:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json() or {}
        except Exception:
            return None
        d = data.get("delay")
        if isinstance(d, (int, float)) and d > 0:
            return int(d)
        return None

    def select(self, group: str, name: str) -> bool:
        r = self._req("PUT", "/proxies/" + _q(group), json={"name": name})
        if r is None:
            return False
        ok = r.status_code in (200, 204)
        # mihomo accepts PATCH as well on some builds
        if not ok and r.status_code in (400, 404, 405):
            r2 = self._req("PATCH", "/proxies/" + _q(group), json={"name": name})
            ok = r2 is not None and r2.status_code in (200, 204)
        if not ok:
            log.debug("select %s -> %s failed (%s)", group, name,
                      getattr(r, "status_code", "?"))
        return ok

    def reload_config(self, path: str, force: bool = True) -> bool:
        body = {"path": path}
        r = self._req("PUT", "/configs", params={"force": "true" if force else "false"},
                      json=body, timeout=max(self.timeout, 15.0))
        return r is not None and r.status_code in (200, 204)

    def close(self) -> None:
        try:
            self._sess.close()
        except Exception:
            pass


def _q(name: str) -> str:
    import urllib.parse
    return urllib.parse.quote(name, safe="")


# ---------------------------------------------------------------------------
# helpers for interpreting the /proxies payload
# ---------------------------------------------------------------------------

def group_now(proxies: Dict[str, Any], group: str) -> str:
    info = proxies.get(group) or {}
    return str(info.get("now") or "")


def group_members(proxies: Dict[str, Any], group: str) -> List[str]:
    info = proxies.get(group) or {}
    members = info.get("all") or []
    return [str(m) for m in members]


def node_names(proxies: Dict[str, Any], skip_groups: bool = True) -> List[str]:
    out = []
    for name, info in proxies.items():
        if not isinstance(info, dict):
            continue
        t = str(info.get("type") or "")
        if skip_groups and t in ("Selector", "URLTest", "Fallback",
                                 "LoadBalance", "Relay", "Compatible"):
            continue
        if t in ("Direct", "Reject", "RejectDrop", "Dns", "Pass"):
            continue
        out.append(name)
    return out


def last_delay(proxies: Dict[str, Any], name: str) -> Optional[int]:
    info = proxies.get(name) or {}
    hist = info.get("history") or []
    if not isinstance(hist, list) or not hist:
        return None
    last = hist[-1] or {}
    d = last.get("delay")
    if isinstance(d, (int, float)) and d > 0:
        return int(d)
    return None


def wait_for_controller(ctl: MihomoController, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctl.alive():
            return True
        time.sleep(0.25)
    return False
