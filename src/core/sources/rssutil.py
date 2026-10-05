# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Tiny namespace-agnostic RSS/Atom reader built on the stdlib.

The feeds we consume are frequently malformed (bare ``&``, stray control
characters, mixed encodings), so everything is parsed with a recovering
parser and every field is looked up by *local* name with a fallback chain.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Dict, Iterator, List, Optional

_TAG_RE = re.compile(r"\{[^}]*\}")
_WS_RE = re.compile(r"\s+")
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)


def localname(tag: str) -> str:
    return _TAG_RE.sub("", tag or "").lower()


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    s = value.strip()
    s = _WS_RE.sub(" ", s)
    return s


def strip_html(value: str) -> str:
    if not value:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", value)
    s = re.sub(r"(?s)<br\s*/?>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    return clean_text(s)


class Item:
    """A flattened feed entry."""

    __slots__ = ("title", "link", "enclosures", "description", "guid",
                 "pubdate", "extra")

    def __init__(self) -> None:
        self.title = ""
        self.link = ""
        self.enclosures: List[str] = []
        self.description = ""
        self.guid = ""
        self.pubdate: Optional[float] = None
        self.extra: Dict[str, str] = {}

    def get(self, *names: str) -> str:
        for n in names:
            v = self.extra.get(n.lower())
            if v:
                return v
        return ""

    def __repr__(self) -> str:  # pragma: no cover
        return "<Item %r>" % (self.title[:60],)


def _parse_xml(data: str):
    """Parse XML leniently.

    The stdlib parser has **no** ``recover`` option (passing it raises
    ``TypeError``), and these feeds are routinely malformed, so lxml is used
    as a recovering fallback when available.
    """
    payload = data.encode("utf-8", "replace")
    try:
        return ET.fromstring(payload)
    except Exception:
        # ImportError when pyexpat was not bundled, ParseError on malformed
        # input -- both should fall through to the next strategy.
        pass
    try:
        from lxml import etree as _lxml  # type: ignore
        parser = _lxml.XMLParser(recover=True, encoding="utf-8",
                                 resolve_entities=False, huge_tree=True)
        root = _lxml.fromstring(payload, parser=parser)
        return root
    except Exception:
        return None


_ITEM_RE = re.compile(r"(?is)<(item|entry)\b.*?</\1>")


def _fallback_items(data: str, max_items: int) -> List["Item"]:
    """Regex extraction used when no XML parser can make sense of the feed."""
    out: List[Item] = []
    for m in _ITEM_RE.finditer(data):
        blk = m.group(0)
        it = Item()
        tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", blk)
        if tm:
            it.title = clean_text(strip_html(tm.group(1)))
        lm = re.search(r"(?is)<link[^>]*>(.*?)</link>", blk)
        if lm:
            it.link = clean_text(lm.group(1))
        for em in re.finditer(r'(?is)<enclosure[^>]*url="([^"]+)"', blk):
            it.enclosures.append(html_unquote(em.group(1)))
        dm = re.search(r"(?is)<description[^>]*>(.*?)</description>", blk)
        if dm:
            it.description = dm.group(1)
        for key in ("seeders", "infoHash", "contentLength", "size"):
            km = re.search(r'(?is)<[a-z0-9_]*:?%s[^>]*>(.*?)</[a-z0-9_]*:?%s>' % (key, key), blk)
            if km:
                it.extra[key.lower()] = clean_text(km.group(1))
        out.append(it)
        if len(out) >= max_items:
            break
    return out


def html_unquote(value: str) -> str:
    import html as _html
    return _html.unescape(value or "")


def _tag_name(tag) -> str:
    return tag if isinstance(tag, str) else ""


def parse_feed(xml_text: str, max_items: int = 400) -> List[Item]:
    if not xml_text or not xml_text.strip():
        return []
    data = xml_text.strip()
    if data.startswith("\ufeff"):
        data = data[1:]
    # strip control characters that no XML parser will accept
    data = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", data)

    root = _parse_xml(data)
    if root is None:
        return _fallback_items(data, max_items)

    out: List[Item] = []
    for node in root.iter():
        if localname(_tag_name(node.tag)) not in ("item", "entry"):
            continue
        it = Item()
        for child in node:
            name = localname(_tag_name(child.tag))
            if not name:
                continue
            text = "".join(child.itertext()) if len(child) else (child.text or "")
            if name == "title":
                it.title = it.title or clean_text(strip_html(text)) or clean_text(text)
            elif name == "link":
                href = child.get("href") or ""
                rel = (child.get("rel") or "alternate").lower()
                cand = clean_text(text) or href
                if cand and (rel == "alternate" or not it.link):
                    it.link = it.link or cand
            elif name == "enclosure":
                url = child.get("url") or clean_text(text)
                if url:
                    it.enclosures.append(url)
            elif name in ("description", "summary", "content"):
                if not it.description:
                    it.description = text or ""
            elif name in ("guid", "id"):
                it.guid = it.guid or clean_text(text)
            elif name in ("pubdate", "published", "updated", "date"):
                if it.pubdate is None:
                    it.pubdate = _parse_date(text)
            # namespace-qualified extras (nyaa:seeders, torrent:contentLength, ...)
            it.extra.setdefault(name, clean_text(text)[:256])
            if child.attrib:
                for ak, av in child.attrib.items():
                    it.extra.setdefault(localname(_tag_name(ak)), str(av)[:256])
        if it.title or it.link or it.enclosures:
            out.append(it)
        if len(out) >= max_items:
            break
    if not out:
        return _fallback_items(data, max_items)
    return out


_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
)


def _parse_date(value: str) -> Optional[float]:
    s = clean_text(value)
    if not s:
        return None
    import datetime as _dt
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except Exception:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", s)
    if m:
        try:
            return _dt.datetime(*[int(x) for x in m.groups()]).timestamp()
        except Exception:
            return None
    return None


def to_int(value: str, default: int = 0) -> int:
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    m = re.search(r"-?\d[\d,]*", s)
    if not m:
        return default
    try:
        return int(m.group(0).replace(",", ""))
    except Exception:
        return default


_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGTP]?i?B)", re.I)
_SIZE_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}


def parse_size(value: str) -> int:
    """'1.6 GiB' -> bytes."""
    if not value:
        return 0
    m = _SIZE_RE.search(str(value))
    if not m:
        return to_int(value, 0)
    try:
        num = float(m.group(1))
    except Exception:
        return 0
    unit = m.group(2).lower()
    return int(num * _SIZE_UNITS.get(unit, 1))
