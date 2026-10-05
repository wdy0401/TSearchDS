"""Link parsing / conversion.

Handles the three link families the app cares about:

* ``magnet:?xt=urn:btih:<hex|base32>``            -> BitTorrent
* ``ed2k://|file|<name>|<size>|<hash>|/``         -> eDonkey2000 / Kad
* ``thunder://<base64('AA' + url + 'ZZ')>``       -> Xunlei (迅雷)

A Xunlei link is *not* a network of its own -- it is a transport wrapper
around an ordinary URL (http/ftp/ed2k/magnet/...).  So every result can be
re-emitted as a ``thunder://`` link, and a ``thunder://`` link can be
unwrapped back to whatever it really points at.
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

from .models import (
    KIND_ED2K,
    KIND_HTTP,
    KIND_MAGNET,
    KIND_THUNDER,
    KIND_TORRENT,
    KIND_UNKNOWN,
)

# ---------------------------------------------------------------------------
# magnet
# ---------------------------------------------------------------------------

_BTIH_RE = re.compile(r"^urn:btih:([0-9a-zA-Z]{32,40})$", re.I)
_ED2K_URN_RE = re.compile(r"^urn:ed2k:([0-9a-fA-F]{32})$")
_BTMH_RE = re.compile(r"^urn:btmh:", re.I)


def magnet_infohash(link: str) -> str:
    """Return the lower-case hex infohash of a magnet URI ('' when absent)."""
    if not link or not link.lower().startswith("magnet:"):
        return ""
    try:
        q = parse_qs(link.split("?", 1)[1], keep_blank_values=True)
    except Exception:
        return ""
    for xt in q.get("xt", []):
        xt = unquote(xt)
        m = _BTIH_RE.match(xt)
        if not m:
            continue
        ih = m.group(1)
        if len(ih) == 32:
            try:
                return binascii.hexlify(base64.b32decode(ih.upper())).decode()
            except Exception:
                return ih.lower()
        return ih.lower()
    return ""


def magnet_display_name(link: str) -> str:
    if not link or not link.lower().startswith("magnet:"):
        return ""
    try:
        q = parse_qs(link.split("?", 1)[1], keep_blank_values=True)
    except Exception:
        return ""
    dn = q.get("dn", [""])[0]
    return unquote(dn.replace("+", " ")).strip()


def magnet_size(link: str) -> int:
    try:
        q = parse_qs(link.split("?", 1)[1], keep_blank_values=True)
    except Exception:
        return 0
    for key in ("xl", "so"):
        v = q.get(key, [""])[0]
        if v.isdigit():
            return int(v)
    return 0


def magnet_ed2k_hash(link: str) -> str:
    try:
        q = parse_qs(link.split("?", 1)[1], keep_blank_values=True)
    except Exception:
        return ""
    for xt in q.get("xt", []):
        m = _ED2K_URN_RE.match(unquote(xt))
        if m:
            return m.group(1).lower()
    return ""


DEFAULT_TRACKERS: Tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "https://tracker.tamersunion.org:443/announce",
)


def build_magnet(infohash: str, name: str = "", size: int = 0,
                 trackers: Optional[List[str]] = None,
                 ed2k_hash: str = "") -> str:
    """Build a magnet URI from an infohash (and optional metadata)."""
    parts: List[str] = []
    if ed2k_hash:
        parts.append("xt=urn:ed2k:" + ed2k_hash.lower())
    if infohash:
        parts.append("xt=urn:btih:" + infohash.lower())
    if name:
        parts.append("dn=" + quote(name, safe=""))
    if size:
        parts.append("xl=" + str(int(size)))
    for tr in (trackers if trackers is not None else DEFAULT_TRACKERS):
        if tr:
            parts.append("tr=" + quote(tr, safe=""))
    return "magnet:?" + "&".join(parts)


# ---------------------------------------------------------------------------
# ed2k
# ---------------------------------------------------------------------------

_ED2K_RE = re.compile(
    r"^ed2k://\|file\|(?P<name>.*?)\|(?P<size>\d+)\|(?P<hash>[0-9a-fA-F]{32})\|",
    re.I | re.S,
)


def parse_ed2k(link: str):
    """-> (name, size, hash) or None."""
    if not link:
        return None
    m = _ED2K_RE.match(link.strip())
    if not m:
        return None
    try:
        size = int(m.group("size"))
    except Exception:
        size = 0
    name = m.group("name")
    try:
        name = unquote(name)
    except Exception:
        pass
    return name, size, m.group("hash").lower()


def ed2k_hash_of(link: str) -> str:
    p = parse_ed2k(link)
    return p[2] if p else ""


def build_ed2k(name: str, size: int, filehash: str) -> str:
    return "ed2k://|file|%s|%d|%s|/" % (name, int(size), filehash.lower())


# ---------------------------------------------------------------------------
# thunder (迅雷)
# ---------------------------------------------------------------------------

_THUNDER_PREFIX = "thunder://"


def thunder_encode(url: str) -> str:
    """Wrap any URL as a Xunlei link."""
    if not url:
        return ""
    raw = ("AA" + url + "ZZ").encode("utf-8", "surrogateescape")
    return _THUNDER_PREFIX + base64.b64encode(raw).decode("ascii")


def thunder_decode(link: str) -> str:
    """Unwrap a Xunlei link.  Returns '' when it is not a valid thunder link."""
    if not link:
        return ""
    s = link.strip()
    if s.lower().startswith(_THUNDER_PREFIX):
        s = s[len(_THUNDER_PREFIX):]
    s = s.strip()
    # tolerate url-safe / missing padding variants found in the wild
    pad = (-len(s)) % 4
    s = s.replace("-", "+").replace("_", "/") + "=" * pad
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", "replace")
    text = text.strip("\x00").strip()
    if text.startswith("AA"):
        text = text[2:]
    if text.endswith("ZZ"):
        text = text[:-2]
    return text.strip()


# ---------------------------------------------------------------------------
# generic
# ---------------------------------------------------------------------------

def detect_kind(link: str) -> str:
    if not link:
        return KIND_UNKNOWN
    s = link.strip().lower()
    if s.startswith("magnet:"):
        return KIND_MAGNET
    if s.startswith("ed2k://"):
        return KIND_ED2K
    if s.startswith("thunder://"):
        return KIND_THUNDER
    if s.startswith("http://") or s.startswith("https://"):
        return KIND_HTTP
    if s.startswith("ftp://"):
        return KIND_HTTP
    if s.startswith("bc://") or s.startswith("qqdl://"):
        return KIND_THUNDER
    return KIND_UNKNOWN


def safe_page_url(url: str, query: str = "") -> str:
    """A URL that is safe to hand to a browser, or ``""``.

    Two rules, both about the same promise: searching must not leave the
    keyword anywhere the user did not ask for -- and a browser's history is
    exactly such a place.

    * a URL whose **path** contains the keyword is refused outright, because
      some indexes page their results as ``/torrent-list/<关键词>/`` and
      opening that is what puts the search into the browser history;
    * a URL that only carries the keyword in its query string or fragment is
      returned without them (``/q.php?q=火影忍者`` -> ``/q.php``).

    Anything that does not mention the keyword is returned unchanged, so
    ordinary detail pages (``/view/12345``, ``/torrent/name-hash``) still work.
    """
    text = str(url or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return ""
    parts = urlparse(text)
    if not parts.netloc:
        return ""
    tokens = [t for t in (query or "").lower().split() if len(t) >= 2]
    if not tokens:
        return text
    if any(t in unquote(parts.path or "").lower() for t in tokens):
        return ""
    query_clean = parts.query
    if any(t in unquote(parts.query or "").lower() for t in tokens):
        query_clean = ""
    frag_clean = parts.fragment
    if any(t in unquote(parts.fragment or "").lower() for t in tokens):
        frag_clean = ""
    if query_clean == parts.query and frag_clean == parts.fragment:
        return text
    return urlunparse((parts.scheme, parts.netloc, parts.path or "/",
                       "", query_clean, frag_clean))


# ---------------------------------------------------------------------------
# offline-download / CC ("bc://") wrappers used by Baidu / QQ旋风
# ---------------------------------------------------------------------------

def bc_encode(url: str, mark: str = "bt") -> str:
    import zlib
    payload = mark.encode() + url.encode("utf-8", "surrogateescape")
    return "bc://%s/%s" % (mark, base64.b64encode(zlib.compress(payload)).decode())


# ---------------------------------------------------------------------------
# convenience: convert a SearchResult link to any of the three families
# ---------------------------------------------------------------------------

def link_to_thunder(link: str) -> str:
    return thunder_encode(link) if link else ""


def link_to_magnet(link: str, name: str = "", size: int = 0) -> str:
    """Best-effort conversion of any link to a magnet URI."""
    kind = detect_kind(link)
    if kind == KIND_MAGNET:
        return link
    if kind == KIND_ED2K:
        p = parse_ed2k(link)
        if not p:
            return ""
        return build_magnet("", p[0] or name, p[1] or size, ed2k_hash=p[2])
    if kind in (KIND_THUNDER, KIND_HTTP):
        inner = unwrap(link)
        if inner and inner != link:
            return link_to_magnet(inner, name, size)
        return ""
    return ""


def link_to_ed2k(link: str, name: str = "", size: int = 0) -> str:
    kind = detect_kind(link)
    if kind == KIND_ED2K:
        return link
    if kind == KIND_MAGNET:
        h = magnet_ed2k_hash(link)
        if h:
            return build_ed2k(magnet_display_name(link) or name,
                              magnet_size(link) or size, h)
        return ""
    if kind in (KIND_THUNDER, KIND_HTTP):
        inner = unwrap(link)
        if inner and inner != link:
            return link_to_ed2k(inner, name, size)
    return ""


def unwrap(link: str) -> str:
    """Peel thunder:// / bc:// / qqdl:// wrappers.  Returns the inner URL."""
    if not link:
        return ""
    s = link.strip()
    low = s.lower()
    if low.startswith(_THUNDER_PREFIX):
        return thunder_decode(s)
    if low.startswith("qqdl://"):
        try:
            pad = (-len(s[7:])) % 4
            raw = base64.b64decode(s[7:].replace("-", "+").replace("_", "/") + "=" * pad)
            return raw.decode("utf-8", "replace")
        except Exception:
            return ""
    if low.startswith("bc://"):
        try:
            import zlib
            body = s.split("://", 1)[1]
            if "/" not in body:
                return ""
            _mark, b64 = body.split("/", 1)
            pad = (-len(b64)) % 4
            raw = zlib.decompress(base64.b64decode(b64 + "=" * pad))
            if len(raw) > 2:
                return raw[2:].decode("utf-8", "replace")
            return raw.decode("utf-8", "replace")
        except Exception:
            return ""
    return s
