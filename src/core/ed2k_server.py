"""eD2k **server** search ("hub" search) -- the path TSearch.exe used.

`kad.py` talks to the Kademlia network, which is UDP and needs a reachable
node.  This module speaks the classic eDonkey2000 **client<->server** TCP
protocol instead, which still has ~100k users across a handful of live
servers and works from behind NAT (you simply get a LowID).

Why this file exists at all: reverse-engineering the local ``TSearch.exe``
showed its bundled ``kad.dll`` is a stripped eMule kernel carrying both
``emule_hub_queryer`` and ``emule_server_listener``, i.e. it searched *hubs*
(servers) as well as Kad.  The server path is the one that still works.

Wire formats (verified against eMule's ``packets.cpp`` / ``SafeFile.cpp`` and
cross-checked against the Rust reference server,
``github.com/andrey23127/ed2k-server``):

**TCP framing** -- note the field order, it is easy to get wrong::

    [0xE3][uint32 length][opcode][payload]     length = len(payload) + 1
    [0xD4][uint32 length][opcode][zlib(payload)]

**Tags** (the "newtags" form)::

    type       = real_type | 0x80        (0x80 => the name is one raw byte)
    name       = <byte>                  when 0x80 set
               | uint16 len + bytes      otherwise
    value      = uint16 len + bytes      real_type 0x02 (STRING)
               | raw bytes               real_type 0x11..0x20, len = type - 0x10
               | uint32/uint16/uint8/uint64 LE
               | 16 raw bytes            real_type 0x01 (HASH16)

**Login** ``OP_LOGINREQUEST`` (0x01) = ``<hash16><id4><port2><tagcount4><tags>``

**Search** ``OP_SEARCHREQUEST`` (0x16) = a binary expression tree::

    0x01 <uint16 len><utf8>            string term
    0x00 <op: 00=AND 01=OR 02=NOT> <l> <r>

**Results** ``OP_SEARCHRESULT`` (0x33)::

    uint32 count
    count * ( <hash16><source_id4><source_port2><u32 tagcount><tags> )
    uint8 has_more
"""
from __future__ import annotations

import ipaddress
import logging
import os
import random
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .parallel import pmap

log = logging.getLogger("tsearch.ed2k")

# -- protocol constants -----------------------------------------------------
PROTO_EDONKEY = 0xE3
PROTO_PACKED = 0xD4

OP_LOGINREQUEST = 0x01
OP_GETSERVERLIST = 0x14
OP_SEARCHREQUEST = 0x16
OP_QUERY_MORE_RESULT = 0x21
OP_REJECT = 0x05
OP_SERVERLIST = 0x32
OP_SEARCHRESULT = 0x33
OP_SERVERSTATUS = 0x34
OP_SERVERMESSAGE = 0x38
OP_IDCHANGE = 0x40
OP_SERVERIDENT = 0x41
OP_FOUNDSOURCES = 0x42

# client tags (login)
CT_NAME = 0x01
CT_VERSION = 0x11
CT_SERVER_FLAGS = 0x20
CT_EMULE_VERSION = 0xFB

SRVCAP_ZLIB = 0x0001
SRVCAP_NEWTAGS = 0x0008
SRVCAP_UNICODE = 0x0010
SRVCAP_LARGEFILES = 0x0100

# file tags
FT_FILENAME = 0x01
FT_FILESIZE = 0x02
FT_FILETYPE = 0x03
FT_FILEFORMAT = 0x04
FT_FILESIZE_HI = 0x3A
FT_SOURCES = 0x15
FT_COMPLETE_SOURCES = 0x30

# tag value types
T_HASH = 0x01
T_STRING = 0x02
T_UINT32 = 0x03
T_FLOAT = 0x04
T_BOOL = 0x05
T_BLOB = 0x07
T_UINT16 = 0x08
T_UINT8 = 0x09
T_UINT64 = 0x0B
T_STR1_BASE = 0x10
T_NEWTAG = 0x80

SERVER_MET_URLS = (
    "http://upd.emule-security.org/server.met",
    "http://peerates.net/servers.met",
)


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------

def _tag_byte_name(name_id: int, real_type: int, value) -> bytes:
    """Encode a tag whose name is a single byte ID (compact form)."""
    out = bytearray()
    if real_type == T_STRING:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        n = len(raw)
        if 1 <= n <= 16:
            out.append(T_NEWTAG | (T_STR1_BASE + n))
            out.append(name_id)
            out += raw
            return bytes(out)
        out.append(T_NEWTAG | T_STRING)
        out.append(name_id)
        out += struct.pack("<H", n) + raw
        return bytes(out)
    out.append(T_NEWTAG | real_type)
    out.append(name_id)
    if real_type == T_UINT32:
        out += struct.pack("<I", int(value) & 0xFFFFFFFF)
    elif real_type == T_UINT16:
        out += struct.pack("<H", int(value) & 0xFFFF)
    elif real_type == T_UINT8:
        out += struct.pack("<B", int(value) & 0xFF)
    elif real_type == T_UINT64:
        out += struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    return bytes(out)


def read_tag(buf: bytes, pos: int):
    """Read one tag.  Returns ``(name, value, new_pos)``."""
    type_byte = buf[pos]; pos += 1
    short_name = bool(type_byte & T_NEWTAG)
    real_type = type_byte & ~T_NEWTAG

    if short_name:
        name = buf[pos]; pos += 1
    else:
        nlen = struct.unpack_from("<H", buf, pos)[0]; pos += 2
        raw = buf[pos:pos + nlen]; pos += nlen
        name = raw[0] if nlen == 1 else raw.decode("utf-8", "replace")

    if T_STR1_BASE < real_type <= T_STR1_BASE + 16:
        n = real_type - T_STR1_BASE
        value = buf[pos:pos + n].decode("utf-8", "replace"); pos += n
    elif real_type == T_STRING:
        n = struct.unpack_from("<H", buf, pos)[0]; pos += 2
        value = buf[pos:pos + n].decode("utf-8", "replace"); pos += n
    elif real_type == T_UINT32:
        value = struct.unpack_from("<I", buf, pos)[0]; pos += 4
    elif real_type == T_UINT16:
        value = struct.unpack_from("<H", buf, pos)[0]; pos += 2
    elif real_type == T_UINT8:
        value = buf[pos]; pos += 1
    elif real_type == T_UINT64:
        value = struct.unpack_from("<Q", buf, pos)[0]; pos += 8
    elif real_type == T_FLOAT:
        value = struct.unpack_from("<f", buf, pos)[0]; pos += 4
    elif real_type == T_BOOL:
        value = buf[pos] != 0; pos += 1
    elif real_type == T_HASH:
        value = buf[pos:pos + 16]; pos += 16
    elif real_type == T_BLOB:
        n = struct.unpack_from("<I", buf, pos)[0]; pos += 4
        value = buf[pos:pos + n]; pos += n
    else:
        raise ValueError("unknown tag type 0x%02x" % type_byte)
    return name, value, pos


def read_tag_list(buf: bytes, pos: int) -> Tuple[Dict[Any, Any], int]:
    count = struct.unpack_from("<I", buf, pos)[0]; pos += 4
    tags: Dict[Any, Any] = {}
    for _ in range(count):
        name, value, pos = read_tag(buf, pos)
        tags.setdefault(name, value)
    return tags, pos


# ---------------------------------------------------------------------------
# packets
# ---------------------------------------------------------------------------

def frame(opcode: int, payload: bytes = b"") -> bytes:
    return (bytes([PROTO_EDONKEY]) + struct.pack("<I", len(payload) + 1)
            + bytes([opcode]) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("server closed the connection")
        buf += chunk
    return buf


def read_frame(sock: socket.socket) -> Tuple[int, bytes]:
    head = _recv_exact(sock, 6)
    prot = head[0]
    length = struct.unpack("<I", head[1:5])[0]
    opcode = head[5]
    body = _recv_exact(sock, length - 1) if length > 1 else b""
    if prot == PROTO_PACKED:
        body = zlib.decompress(body)
    elif prot != PROTO_EDONKEY:
        raise ValueError("bad protocol marker 0x%02x" % prot)
    return opcode, body


def search_tree(keyword: str) -> bytes:
    """``0x01 <uint16 len> <utf8>`` -- a single string term."""
    q = keyword.encode("utf-8")
    return b"\x01" + struct.pack("<H", len(q)) + q


def login_payload(nick: str = "TSearchDS", port: int = 0) -> bytes:
    payload = (bytes(random.getrandbits(8) for _ in range(16))
               + struct.pack("<I", 0)
               + struct.pack("<H", port))
    tags = (_tag_byte_name(CT_NAME, T_STRING, nick)
            + _tag_byte_name(CT_VERSION, T_STRING, "0.70a")
            # no SRVCAP_ZLIB: keep every reply uncompressed
            + _tag_byte_name(CT_SERVER_FLAGS, T_UINT32,
                             SRVCAP_NEWTAGS | SRVCAP_UNICODE | SRVCAP_LARGEFILES)
            + _tag_byte_name(CT_EMULE_VERSION, T_UINT32, 0x011880))
    return payload + struct.pack("<I", 4) + tags


# ---------------------------------------------------------------------------
# server list
# ---------------------------------------------------------------------------

@dataclass
class Ed2kServer:
    ip: str
    port: int
    name: str = ""
    users: int = 0
    files: int = 0
    last_ok: float = 0.0
    last_try: float = 0.0

    @property
    def key(self) -> Tuple[str, int]:
        return (self.ip, self.port)

    def __hash__(self) -> int:
        return hash(self.key)


def parse_server_met(data: bytes) -> List[Ed2kServer]:
    if not data or data[0] != 0xE0:
        return []
    pos = 1
    count = struct.unpack_from("<I", data, pos)[0]; pos += 4
    out: List[Ed2kServer] = []
    for _ in range(count):
        # server.met stores the address in NETWORK byte order; unpacking it
        # little-endian silently byte-swaps every server (85.17.116.222
        # becomes 222.116.17.85) and nothing ever answers
        ip = struct.unpack_from(">I", data, pos)[0]; pos += 4
        port = struct.unpack_from("<H", data, pos)[0]; pos += 2
        tagcount = struct.unpack_from("<I", data, pos)[0]; pos += 4
        name, users, files = "", 0, 0
        for _ in range(tagcount):
            ttype = data[pos]; pos += 1
            nlen = struct.unpack_from("<H", data, pos)[0]; pos += 2
            tname = data[pos:pos + nlen]; pos += nlen
            if ttype == T_STRING:
                vlen = struct.unpack_from("<H", data, pos)[0]; pos += 2
                val = data[pos:pos + vlen].decode("latin-1"); pos += vlen
            elif ttype == T_UINT32:
                val = struct.unpack_from("<I", data, pos)[0]; pos += 4
            else:
                break
            if tname == b"\x01":
                name = str(val)
            elif tname == b"users":
                users = int(val)
            elif tname == b"files":
                files = int(val)
        try:
            ip_s = str(ipaddress.IPv4Address(ip))
        except Exception:
            continue
        if not ip_s or port == 0:
            continue
        out.append(Ed2kServer(ip_s, port, name, users, files))
    return out


def servers_cache_path() -> str:
    from .proxy.core import data_dir
    return os.path.join(data_dir(), "ed2k_servers.json")


def load_servers(force: bool = False, max_age: float = 6 * 3600.0) -> List[Ed2kServer]:
    """Fetch + parse the published server lists, with an on-disk cache."""
    import json
    path = servers_cache_path()
    if not force and os.path.isfile(path):
        try:
            if time.time() - os.path.getmtime(path) < max_age:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                out = [Ed2kServer(**s) for s in raw.get("servers", [])]
                if out:
                    return out
        except (OSError, ValueError, TypeError):
            pass

    from .http import HTTP
    found: Dict[Tuple[str, int], Ed2kServer] = {}
    for url in SERVER_MET_URLS:
        data = HTTP.get_bytes(url, timeout=20, max_bytes=4 << 20)
        for s in parse_server_met(data or b""):
            found.setdefault(s.key, s)
    servers = list(found.values())
    if servers:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"fetched": time.time(),
                           "servers": [s.__dict__ for s in servers]}, fh,
                          ensure_ascii=False)
        except OSError:
            pass
    return servers


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

@dataclass
class Ed2kHit:
    file_hash: str
    name: str
    size: int = 0
    sources: int = 0
    complete_sources: int = 0
    filetype: Any = 0

    @property
    def ed2k(self) -> str:
        safe = self.name.replace("|", "_").replace("/", "_")
        return "ed2k://|file|%s|%d|%s|/" % (safe, self.size, self.file_hash.upper())


class Ed2kServerClient:
    """One login + search + result collection against a single server."""

    def __init__(self, nick: str = "TSearchDS", timeout: float = 12.0) -> None:
        self.nick = nick
        self.timeout = timeout
        self.server_status: Tuple[int, int] = (0, 0)

    def search(self, server: Ed2kServer, keyword: str,
               max_results: int = 300, budget: float = 25.0,
               stop_event: Optional[threading.Event] = None) -> List[Ed2kHit]:
        hits: Dict[str, Ed2kHit] = {}
        deadline = time.monotonic() + budget
        sock = None
        try:
            sock = socket.create_connection(server.key, timeout=min(10.0, budget))
            sock.settimeout(max(3.0, min(self.timeout, budget)))
            sock.sendall(frame(OP_LOGINREQUEST, login_payload(self.nick)))

            logged_in = False
            asked = False
            more_sent = False
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    opcode, body = read_frame(sock)
                except socket.timeout:
                    break
                except (EOFError, ValueError, OSError):
                    break

                if opcode == OP_IDCHANGE:
                    logged_in = True
                    sock.sendall(frame(OP_SEARCHREQUEST, search_tree(keyword)))
                    asked = True
                elif opcode == OP_SERVERSTATUS and len(body) >= 8:
                    self.server_status = struct.unpack_from("<II", body, 0)
                elif opcode == OP_SEARCHRESULT:
                    count, has_more = self._parse_results(body, hits)
                    if has_more and not more_sent:
                        more_sent = True
                        sock.sendall(frame(OP_QUERY_MORE_RESULT))
                    else:
                        break
                    if len(hits) >= max_results:
                        break
                elif opcode == OP_REJECT:
                    log.debug("server %s rejected us", server.key)
                    break
                elif opcode == OP_SERVERMESSAGE:
                    continue
                elif opcode in (OP_SERVERLIST, OP_SERVERIDENT, OP_FOUNDSOURCES):
                    continue
                else:
                    continue

            if logged_in:
                server.last_ok = time.monotonic()
            try:
                sock.sendall(frame(0x18))       # OP_DISCONNECT
            except OSError:
                pass
        except (OSError, ValueError) as exc:
            log.debug("ed2k server %s failed: %s", server.key, exc)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        return list(hits.values())

    @staticmethod
    def _parse_results(body: bytes, hits: Dict[str, Ed2kHit]) -> Tuple[int, bool]:
        count = struct.unpack_from("<I", body, 0)[0]
        pos = 4
        for _ in range(count):
            if pos + 22 > len(body):
                return count, False
            fhash = body[pos:pos + 16].hex(); pos += 16
            _src_id, _src_port = struct.unpack_from("<IH", body, pos); pos += 6
            tags, pos = read_tag_list(body, pos)
            name = tags.get(FT_FILENAME, "")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if not name:
                continue
            size = int(tags.get(FT_FILESIZE, 0) or 0)
            hi = int(tags.get(FT_FILESIZE_HI, 0) or 0)
            if hi:
                size |= hi << 32
            hits[fhash] = Ed2kHit(
                file_hash=fhash,
                name=name,
                size=size,
                sources=int(tags.get(FT_SOURCES, 0) or 0),
                complete_sources=int(tags.get(FT_COMPLETE_SOURCES, 0) or 0),
                filetype=tags.get(FT_FILETYPE, 0),
            )
        has_more = bool(body[pos]) if pos < len(body) else False
        return count, has_more


# ---------------------------------------------------------------------------
# pooled searcher
# ---------------------------------------------------------------------------

class Ed2kServerPool:
    """Keeps a shortlist of servers that actually answer, and searches them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._good: List[Ed2kServer] = []
        self._probed = False
        self._probing = False
        self._last_probe = 0.0
        self._done = threading.Event()
        self.last_error = ""

    # -- discovery -----------------------------------------------------
    def _probe_one(self, server: Ed2kServer, budget: float = 12.0) -> bool:
        client = Ed2kServerClient(timeout=budget)
        try:
            sock = socket.create_connection(server.key, timeout=6)
        except OSError:
            return False
        try:
            sock.settimeout(budget)
            sock.sendall(frame(OP_LOGINREQUEST, login_payload()))
            t0 = time.monotonic()
            while time.monotonic() - t0 < budget:
                opcode, body = read_frame(sock)
                if opcode == OP_IDCHANGE:
                    server.last_ok = time.monotonic()
                    return True
                if opcode == OP_REJECT:
                    return False
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return False

    def refresh(self, force: bool = False, budget: float = 45.0) -> int:
        """Probe the published server list and remember who answered.

        If another thread (the start-up warm-up) already holds the probe, this
        *waits* for it instead of reporting "no servers" -- returning 0 while a
        probe is in flight is what made the first search of every session come
        back empty.
        """
        with self._lock:
            if self._probing:
                wait = True
            else:
                wait = False
                if self._probed and not force and \
                        time.monotonic() - self._last_probe < 1800:
                    return len(self._good)
                self._probing = True
                self._done.clear()
        if wait:
            self._done.wait(timeout=budget + 30)
            with self._lock:
                return len(self._good)
        try:
            servers = load_servers(force=force)
            if not servers:
                self.last_error = "无法获取服务器列表"
                return 0
            # prefer the busiest servers, but probe a wide slice
            servers.sort(key=lambda s: -s.users)
            candidates = servers[:60]
            random.shuffle(candidates[:12])
            good: List[Ed2kServer] = []
            for srv, answered, _err in pmap(self._probe_one, candidates,
                                            workers=24,
                                            timeout=max(20.0, budget)):
                if answered:
                    good.append(srv)
            good.sort(key=lambda s: -s.users)
            with self._lock:
                self._good = good
                self._probed = True
                self._last_probe = time.monotonic()
                self.last_error = "" if good else "没有服务器接受登录"
            log.info("ed2k: %d/%d servers accepted login",
                     len(good), len(candidates))
            return len(good)
        finally:
            with self._lock:
                self._probing = False
            self._done.set()

    @property
    def good_servers(self) -> List[Ed2kServer]:
        with self._lock:
            return list(self._good)

    # -- search --------------------------------------------------------
    def search(self, keyword: str, max_results: int = 300,
               servers: int = 3, budget: float = 30.0,
               stop_event: Optional[threading.Event] = None) -> List[Ed2kHit]:
        if not self._probed:
            self.refresh()
        pool = self.good_servers
        if not pool:
            return []
        chosen = pool[:max(1, servers)]
        merged: Dict[str, Ed2kHit] = {}

        def _one(srv: Ed2kServer) -> List[Ed2kHit]:
            c = Ed2kServerClient(timeout=min(12.0, budget))
            return c.search(srv, keyword, max_results, budget, stop_event)

        for _srv, hits, _err in pmap(_one, chosen, workers=len(chosen),
                                     timeout=budget + 15, cancel=stop_event):
            for h in hits or []:
                prev = merged.get(h.file_hash)
                if prev is None or h.sources > prev.sources:
                    merged[h.file_hash] = h
        return sorted(merged.values(), key=lambda h: (-h.sources, h.name.lower()))


POOL = Ed2kServerPool()


def quick_check(host: str, port: int, timeout: float = 8.0) -> bool:
    """Standalone login probe (used by the tests / diagnostics)."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        sock.settimeout(timeout)
        sock.sendall(frame(OP_LOGINREQUEST, login_payload()))
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            opcode, _ = read_frame(sock)
            if opcode == OP_IDCHANGE:
                return True
            if opcode == OP_REJECT:
                return False
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return False
