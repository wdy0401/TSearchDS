"""Web-index sources for eD2k links.

Reality check (verified 2026-10): the classic Chinese eD2k web indexes
(verycd, ed2000, ed2kers, chnp2p, ...) are all dead, paywalled or behind a
WAF, so the *only* dependable eD2k source today is the Kad network itself
(see :mod:`kad_source`).

Rather than hard-code dead hosts this module ships a small, data-driven
scraper: drop a ``ed2k_sources.json`` into the app data directory and the app
will query those pages too.  Each entry looks like::

    [
      {
        "id": "mysite",
        "label": "我的电驴站",
        "url": "https://example.com/search?q={query}",
        "link_re": "ed2k://\\\\|file\\\\|[^\"'<\\\\s]+\\\\|/",
        "name_re": "<h3[^>]*>(.*?)</h3>",
        "seeds_re": "(\\\\d+)\\\\s*(?:个)?(?:来源|源)",
        "encoding": "utf-8",
        "enabled": true
      }
    ]

``link_re`` is mandatory; ``name_re`` / ``seeds_re`` are optional.  When a
page lists several results, each ``link_re`` match is treated as one row and
the nearest following ``name_re`` match supplies the name.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

from ..http import HTTP
from ..links import parse_ed2k
from ..models import KIND_ED2K, SearchResult
from .base import Source, register

log = logging.getLogger("tsearch.ed2k")

CONFIG_NAME = "ed2k_sources.json"


def _config_path() -> str:
    from ..proxy.core import data_dir
    return os.path.join(data_dir(), CONFIG_NAME)


def load_index_defs() -> List[Dict[str, Any]]:
    path = _config_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("url") and item.get("link_re"):
            out.append(item)
    return out


class _WebEd2kSource(Source):
    kinds = (KIND_ED2K,)
    weight = 40

    def __init__(self, spec: Dict[str, Any]) -> None:
        super().__init__()
        self.spec = spec
        self.id = str(spec.get("id") or "ed2k_web")
        self.label = str(spec.get("label") or self.id)
        self.default_enabled = bool(spec.get("enabled", True))
        self.enabled = self.default_enabled
        try:
            self._link_re = re.compile(spec["link_re"], re.I | re.S)
        except re.error:
            self._link_re = None
        self._name_re = None
        if spec.get("name_re"):
            try:
                self._name_re = re.compile(spec["name_re"], re.I | re.S)
            except re.error:
                self._name_re = None
        self._seeds_re = None
        if spec.get("seeds_re"):
            try:
                self._seeds_re = re.compile(spec["seeds_re"], re.I | re.S)
            except re.error:
                self._seeds_re = None
        self.encoding = str(spec.get("encoding") or "utf-8")

    def search(self, query: str, limit: int = 60) -> Iterable[SearchResult]:
        out: List[SearchResult] = []
        if not self._link_re or not query.strip():
            return out
        url = str(self.spec["url"]).replace(
            "{query}", urllib.parse.quote(query.strip()))
        r = HTTP.get(url, timeout=self.request_timeout)
        if r is None or r.status_code >= 400:
            return out
        try:
            text = r.content.decode(self.encoding, "replace")
        except Exception:
            text = r.text or ""
        for m in self._link_re.finditer(text):
            link = html.unescape(m.group(0)).strip().strip('"\'')
            if not link.lower().startswith("ed2k://"):
                # allow a regex that captured a surrounding quote
                i = link.lower().find("ed2k://")
                if i < 0:
                    continue
                link = link[i:]
            parsed = parse_ed2k(link)
            if not parsed:
                continue
            name, size, fhash = parsed
            if self._name_re:
                nm = self._name_re.search(text, m.end())
                if not nm and self._name_re.search(text):
                    nm = self._name_re.search(text)
                if nm:
                    cand = re.sub(r"<[^>]+>", "", nm.group(1))
                    cand = html.unescape(cand).strip()
                    if cand:
                        name = cand
            seeds = 0
            if self._seeds_re:
                sm = self._seeds_re.search(text, max(0, m.end() - 200))
                if sm:
                    try:
                        seeds = int(re.sub(r"[^\d]", "", sm.group(1)) or 0)
                    except (ValueError, IndexError):
                        seeds = 0
            out.append(SearchResult(
                name=name, link=link, seeds=seeds, source=self.id,
                kind=KIND_ED2K, size=size, ed2k_hash=fhash,
                seeds_verified=bool(seeds),
                # deliberately no "page": this parser only knows the *search*
                # URL, and opening that would put the keyword into the browser's
                # history.  The link itself is the shareable thing here.
                extra={}))
            if len(out) >= limit:
                break
        return out


_DYNAMIC: List[_WebEd2kSource] = []
_DYNAMIC_LOADED = False


def refresh_dynamic_sources() -> List[_WebEd2kSource]:
    """(Re)build the user-configured eD2k web sources."""
    global _DYNAMIC_LOADED
    _DYNAMIC.clear()
    for spec in load_index_defs():
        try:
            _DYNAMIC.append(_WebEd2kSource(spec))
        except Exception as exc:  # noqa: BLE001
            log.warning("bad ed2k source spec %s: %s", spec.get("id"), exc)
    _DYNAMIC_LOADED = True
    return _DYNAMIC


def dynamic_sources() -> List[_WebEd2kSource]:
    if not _DYNAMIC_LOADED:
        refresh_dynamic_sources()
    return list(_DYNAMIC)


def write_default_config() -> str:
    """Drop an empty, documented config file next to the app data."""
    path = _config_path()
    if os.path.isfile(path):
        return path
    sample = [
        {
            "id": "example",
            "label": "示例电驴索引（请改成你找到的站点）",
            "url": "https://example.com/search?q={query}",
            "link_re": r"ed2k://\|file\|[^\"'<\s]+\|/",
            "name_re": r"<h3[^>]*>(.*?)</h3>",
            "seeds_re": r"(\d+)\s*(?:个)?(?:来源|源|sources)",
            "encoding": "utf-8",
            "enabled": False,
        }
    ]
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sample, fh, ensure_ascii=False, indent=2)
        return path
    except OSError:
        return ""
