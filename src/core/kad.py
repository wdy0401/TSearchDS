# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""A minimal, from-scratch eD2k **Kad** client.

Implements just enough of the Kad (Kademlia-over-UDP) protocol to run
keyword searches against the live eDonkey2000 network and turn the answers
into ``ed2k://`` links -- i.e. a headless replacement for the classic
``TSearch.exe`` + ``kad.dll`` pair.

Wire format (verified against eMule ``packets.cpp`` / ``KademliaUDPListener.cpp``)
---------------------------------------------------------------------------
Every datagram is::

    [0xE4][opcode][payload...]                 # plain
    [0xE5][opcode][zlib(payload)...]           # packed (only if len > 200)
    [0xE4][opcode][payload][udpkey:4]?         # optional trailing UDP key

All multi-byte integers are **little endian**; node IDs / hashes are raw
16-byte big-endian values (so ``MD4`` digest bytes go out verbatim).

Opcodes used
------------
===========================  ====  =========================================
KADEMLIA2_BOOTSTRAP_REQ      0x01  (empty)
KADEMLIA2_BOOTSTRAP_RES      0x09  id16, tcp2, ver1, cnt2, cnt*25
KADEMLIA2_HELLO_REQ          0x11  id16, tcp2, ver1, tagcount1, tags
KADEMLIA2_HELLO_RES          0x19  same as HELLO_REQ
KADEMLIA2_REQ                0x21  type1, target16, check16
KADEMLIA2_RES                0x29  target16, cnt1, cnt*25
KADEMLIA2_SEARCH_KEY_REQ     0x33  target16, start2
KADEMLIA2_SEARCH_SOURCE_REQ  0x34  target16, start2, size8
KADEMLIA2_SEARCH_RES         0x3B  src16, target16, cnt2, cnt*(id16 + taglist)
===========================  ====  =========================================

The search target for a keyword is ``MD4(utf8(lowercased_word))`` -- eMule
(``CSearchManager::GetWords`` / ``KadGetKeywordHash``) lower-cases the keyword,
splits it on separators, drops words shorter than 3 UTF-8 bytes and uses the
**first** remaining word as the Kad target.
"""
from __future__ import annotations

import binascii
import hashlib
import logging
import os
import random
import select
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("tsearch.kad")

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

KAD_HEADER = 0xE4
KAD_PACKED = 0xE5
KAD_VERSION = 0x09

OP_BOOTSTRAP_REQ = 0x01
OP_BOOTSTRAP_RES = 0x09
OP_HELLO_REQ = 0x11
OP_HELLO_RES = 0x19
OP_HELLO_RES_ACK = 0x22
OP_REQ = 0x21
OP_RES = 0x29
OP_SEARCH_KEY_REQ = 0x33
OP_SEARCH_SOURCE_REQ = 0x34
OP_SEARCH_NOTES_REQ = 0x35
OP_SEARCH_RES = 0x3B
OP_PING = 0x60
OP_PONG = 0x61

KAD_FIND_VALUE = 0x02
KAD_STORE = 0x04
KAD_FIND_NODE = 0x0B

# tag types
TAGTYPE_HASH = 0x01
TAGTYPE_STRING = 0x02
TAGTYPE_UINT32 = 0x03
TAGTYPE_FLOAT32 = 0x04
TAGTYPE_BLOB = 0x07
TAGTYPE_UINT16 = 0x08
TAGTYPE_UINT8 = 0x09
TAGTYPE_BSOB = 0x0A
TAGTYPE_UINT64 = 0x0B

# well known single-byte tag names
TAG_FILENAME = b"\x01"
TAG_FILESIZE = b"\x02"
TAG_FILETYPE = b"\x03"
TAG_FILEFORMAT = b"\x04"
TAG_DESCRIPTION = b"\x0B"
TAG_SOURCES = b"\x15"
TAG_PUBLISHINFO = b"\x33"
TAG_SOURCEUPORT = b"\xFC"
TAG_SOURCEPORT = b"\xFD"
TAG_SOURCEIP = b"\xFE"
TAG_SOURCETYPE = b"\xFF"
TAG_KADMISCOPTIONS = b"\xF2"

MAX_PACKET = 8192

# keyword separators used by eMule's g_aszInvKadKeywordChars
_SEPARATORS = set(" \t\r\n.,;:!?()[]{}<>\"'`~@#$%^&*-_+=/\\|")
_SEPARATORS.update("，。、；：！？（）【】《》“”‘’·—…")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def md4(data: bytes) -> bytes:
    """MD4 -- in hashlib for most builds; pure-python fallback otherwise."""
    try:
        h = hashlib.new("md4")
        h.update(data)
        return h.digest()
    except Exception:
        return _md4_py(data)


def _md4_py(data: bytes) -> bytes:  # pragma: no cover - fallback only
    import struct as _s

    def _lrot(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    msg = bytearray(data)
    ml = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += _s.pack("<Q", ml & 0xFFFFFFFFFFFFFFFF)

    a, b, c, d = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476
    for off in range(0, len(msg), 64):
        X = _s.unpack("<16I", bytes(msg[off:off + 64]))
        A, B, C, D = a, b, c, d
        for i in range(16):
            k = i
            s = (3, 7, 11, 19)[i % 4]
            if i % 4 == 0:
                A = _lrot((A + ((B & C) | (~B & D)) + X[k]) & 0xFFFFFFFF, s)
            elif i % 4 == 1:
                D = _lrot((D + ((A & B) | (~A & C)) + X[k]) & 0xFFFFFFFF, s)
            elif i % 4 == 2:
                C = _lrot((C + ((D & A) | (~D & B)) + X[k]) & 0xFFFFFFFF, s)
            else:
                B = _lrot((B + ((C & D) | (~C & A)) + X[k]) & 0xFFFFFFFF, s)
        for i in range(16):
            k = (i % 4) * 4 + i // 4
            s = (3, 5, 9, 13)[i % 4]
            if i % 4 == 0:
                A = _lrot((A + ((B & C) | (B & D) | (C & D)) + X[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif i % 4 == 1:
                D = _lrot((D + ((A & B) | (A & C) | (B & C)) + X[k] + 0x5A827999) & 0xFFFFFFFF, s)
            elif i % 4 == 2:
                C = _lrot((C + ((D & A) | (D & B) | (A & B)) + X[k] + 0x5A827999) & 0xFFFFFFFF, s)
            else:
                B = _lrot((B + ((C & D) | (C & A) | (D & A)) + X[k] + 0x5A827999) & 0xFFFFFFFF, s)
        for i in range(16):
            k = (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)[i]
            s = (3, 9, 11, 15)[i % 4]
            if i % 4 == 0:
                A = _lrot((A + (B ^ C ^ D) + X[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif i % 4 == 1:
                D = _lrot((D + (A ^ B ^ C) + X[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            elif i % 4 == 2:
                C = _lrot((C + (D ^ A ^ B) + X[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
            else:
                B = _lrot((B + (C ^ D ^ A) + X[k] + 0x6ED9EBA1) & 0xFFFFFFFF, s)
        a = (a + A) & 0xFFFFFFFF
        b = (b + B) & 0xFFFFFFFF
        c = (c + C) & 0xFFFFFFFF
        d = (d + D) & 0xFFFFFFFF
    return _s.pack("<4I", a, b, c, d)


def keyword_words(text: str) -> List[str]:
    """Reproduce eMule's ``CSearchManager::GetWords`` for one keyword."""
    words: List[str] = []
    cur: List[str] = []
    for ch in text:
        if ch in _SEPARATORS:
            if cur:
                words.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        words.append("".join(cur))
    out = []
    seen = set()
    for w in words:
        w = w.lower()
        if len(w.encode("utf-8", "surrogateescape")) < 3:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def keyword_target(word: str) -> bytes:
    return md4(word.lower().encode("utf-8", "surrogateescape"))


def id_to_hex(b: bytes) -> str:
    return binascii.hexlify(b).decode()


def xor_distance(a: bytes, b: bytes) -> int:
    return int.from_bytes(bytes(x ^ y for x, y in zip(a, b)), "big")


# ---------------------------------------------------------------------------
# tag (de)serialisation
# ---------------------------------------------------------------------------

class _Reader:
    __slots__ = ("buf", "pos", "bad")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos
        self.bad = False

    def left(self) -> int:
        return len(self.buf) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            self.bad = True
            raise ValueError("truncated")
        v = self.buf[self.pos:self.pos + n]
        self.pos += n
        return v

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]


def read_tag(r: _Reader) -> Tuple[bytes, int, object]:
    ttype = r.u8()
    nlen = r.u16()
    name = r.take(nlen)
    if ttype == TAGTYPE_HASH:
        val = r.take(16)
    elif ttype == TAGTYPE_STRING:
        slen = r.u16()
        raw = r.take(slen)
        try:
            val = raw.decode("utf-8")
        except UnicodeDecodeError:
            val = raw.decode("latin-1", "replace")
    elif ttype == TAGTYPE_UINT64:
        val = r.u64()
    elif ttype == TAGTYPE_UINT32:
        val = r.u32()
    elif ttype == TAGTYPE_UINT16:
        val = r.u16()
    elif ttype == TAGTYPE_UINT8:
        val = r.u8()
    elif ttype == TAGTYPE_FLOAT32:
        val = r.f32()
    elif ttype == TAGTYPE_BSOB:
        blen = r.u8()
        val = r.take(blen)
    else:
        raise ValueError("unsupported tag type 0x%02x" % ttype)
    return name, ttype, val


def read_tag_list(r: _Reader) -> Dict[bytes, object]:
    count = r.u8()
    tags: Dict[bytes, object] = {}
    for _ in range(count):
        name, _t, val = read_tag(r)
        tags.setdefault(name, val)
    return tags


def write_tag(name: bytes, ttype: int, value) -> bytes:
    out = bytearray()
    out.append(ttype)
    out += struct.pack("<H", len(name))
    out += name
    if ttype == TAGTYPE_STRING:
        raw = value.encode("utf-8", "surrogateescape") if isinstance(value, str) else value
        out += struct.pack("<H", len(raw))
        out += raw
    elif ttype == TAGTYPE_UINT32:
        out += struct.pack("<I", int(value) & 0xFFFFFFFF)
    elif ttype == TAGTYPE_UINT16:
        out += struct.pack("<H", int(value) & 0xFFFF)
    elif ttype == TAGTYPE_UINT8:
        out += struct.pack("<B", int(value) & 0xFF)
    elif ttype == TAGTYPE_UINT64:
        out += struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    elif ttype == TAGTYPE_HASH:
        out += value
    elif ttype == TAGTYPE_BSOB:
        out.append(len(value))
        out += value
    else:
        raise ValueError("cannot write tag type 0x%02x" % ttype)
    return bytes(out)


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------

@dataclass
class KadContact:
    id: bytes
    ip: str
    udp_port: int
    tcp_port: int = 0
    version: int = KAD_VERSION
    last_seen: float = field(default_factory=time.monotonic)

    @property
    def addr(self) -> Tuple[str, int]:
        return (self.ip, self.udp_port)

    def __hash__(self) -> int:
        return hash((self.id, self.ip, self.udp_port))

    def __eq__(self, other) -> bool:
        return (isinstance(other, KadContact) and self.id == other.id
                and self.ip == other.ip and self.udp_port == other.udp_port)


def parse_contact_blob(buf: bytes) -> List[KadContact]:
    """``cnt * 25`` bytes: id16 ip4 udp2 tcp2 ver1."""
    out: List[KadContact] = []
    n = len(buf) // 25
    for i in range(n):
        chunk = buf[i * 25:(i + 1) * 25]
        cid = chunk[:16]
        ip = socket.inet_ntoa(chunk[16:20])
        udp_port = struct.unpack("<H", chunk[20:22])[0]
        tcp_port = struct.unpack("<H", chunk[22:24])[0]
        ver = chunk[24]
        if not _good_ip(ip) or udp_port == 0:
            continue
        if cid == b"\x00" * 16:
            continue
        out.append(KadContact(cid, ip, udp_port, tcp_port, ver))
    return out


def _good_ip(ip: str) -> bool:
    try:
        parts = [int(x) for x in ip.split(".")]
    except Exception:
        return False
    if len(parts) != 4:
        return False
    a, b = parts[0], parts[1]
    if a == 0 or a >= 224:
        return False
    if a == 10 or a == 127:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    if a == 100 and 64 <= b <= 127:
        return False
    return True


# ---------------------------------------------------------------------------
# nodes.dat
# ---------------------------------------------------------------------------

def load_nodes_dat(path: str) -> List[KadContact]:
    """Read an eMule ``nodes.dat`` (versions 0..3 + bootstrap editions)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    if len(data) < 8:
        return []
    pos = 0
    count, = struct.unpack_from("<I", data, pos)
    pos += 4
    version = 0
    if count == 0:
        version, = struct.unpack_from("<I", data, pos)
        pos += 4
        if version == 3:
            edition, = struct.unpack_from("<I", data, pos)
            pos += 4
            if edition != 1:
                return []
        if not (0 <= version <= 3):
            return []
        count, = struct.unpack_from("<I", data, pos)
        pos += 4
    entsize = {0: 17, 1: 25, 2: 34, 3: 34}.get(version, 25)
    if count * entsize > len(data) - pos + entsize:
        # tolerate slightly-off lengths, just read what fits
        count = max(0, (len(data) - pos) // entsize)
    out: List[KadContact] = []
    for _ in range(count):
        if pos + 16 + 4 + 2 + 2 + 1 > len(data):
            break
        cid = data[pos:pos + 16]
        pos += 16
        ip = socket.inet_ntoa(data[pos:pos + 4])
        pos += 4
        udp_port, tcp_port = struct.unpack_from("<HH", data, pos)
        pos += 4
        ver = data[pos]
        pos += 1
        if version >= 2:
            pos += 9  # CKadUDPKey(8) + verified(1)
        if not _good_ip(ip) or udp_port == 0 or cid == b"\x00" * 16:
            continue
        out.append(KadContact(cid, ip, udp_port, tcp_port, ver))
    return out


# ---------------------------------------------------------------------------
# search hits
# ---------------------------------------------------------------------------

@dataclass
class KadHit:
    file_hash: bytes
    name: str
    size: int = 0
    sources: int = 0
    publishers: int = 0
    filetype: str = ""
    fileformat: str = ""
    aich: bytes = b""

    @property
    def ed2k(self) -> str:
        return "ed2k://|file|%s|%d|%s|/" % (
            self.name.replace("|", "_").replace("/", "_"),
            self.size, binascii.hexlify(self.file_hash).decode().upper())


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class KadClient:
    """Synchronous Kad node good enough for keyword searching.

    Typical use::

        kad = KadClient()
        kad.bootstrap()
        for hit in kad.search("ubuntu"):
            print(hit.name, hit.ed2k, hit.sources)
    """

    def __init__(self, node_id: Optional[bytes] = None,
                 bind: Tuple[str, int] = ("0.0.0.0", 0),
                 nodes_dat: Optional[str] = None) -> None:
        self.id = node_id or os.urandom(16)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.sock.bind(bind)
        self.sock.setblocking(False)
        self.local_port = self.sock.getsockname()[1]
        self.contacts: Dict[bytes, KadContact] = {}
        self.stats = {"sent": 0, "recv": 0, "res": 0, "hits": 0, "errors": 0}
        self._nodes_dat = nodes_dat

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- low level -----------------------------------------------------
    def _build(self, opcode: int, payload: bytes) -> bytes:
        if len(payload) > 200:
            packed = zlib.compress(payload, 9)
            if len(packed) < len(payload):
                return bytes([KAD_PACKED, opcode]) + packed
        return bytes([KAD_HEADER, opcode]) + payload

    def send(self, opcode: int, payload: bytes, addr: Tuple[str, int]) -> None:
        try:
            self.sock.sendto(self._build(opcode, payload), addr)
            self.stats["sent"] += 1
        except OSError:
            self.stats["errors"] += 1

    def _parse(self, data: bytes) -> Optional[Tuple[int, bytes]]:
        if len(data) < 2:
            return None
        proto = data[0]
        opcode = data[1]
        body = data[2:]
        if proto == KAD_PACKED:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                return None
        elif proto != KAD_HEADER:
            return None
        # strip a trailing 4-byte UDP key when the sender is known to use one
        return opcode, body

    # -- bootstrap -----------------------------------------------------
    def add_contacts(self, contacts: Iterable[KadContact]) -> int:
        n = 0
        for c in contacts:
            if c.id in self.contacts:
                continue
            if len(self.contacts) >= 4000:
                break
            self.contacts[c.id] = c
            n += 1
        return n

    def load_bootstrap(self, extra_paths: Optional[Sequence[str]] = None) -> int:
        total = 0
        paths = []
        if self._nodes_dat:
            paths.append(self._nodes_dat)
        if extra_paths:
            paths.extend(extra_paths)
        for p in paths:
            if p and os.path.isfile(p):
                total += self.add_contacts(load_nodes_dat(p))
        return total

    def hello(self, c: KadContact, timeout: float = 1.0) -> Optional[KadContact]:
        """HELLO_REQ -> returns the remote contact when it answered."""
        payload = (c.id
                   + struct.pack("<H", 0)
                   + bytes([KAD_VERSION, 0]))
        # NOTE: the 16-byte sender id must be *ours*
        payload = (self.id
                   + struct.pack("<H", 0)
                   + bytes([KAD_VERSION, 0]))
        self.send(OP_HELLO_REQ, payload, c.addr)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self._recv(timeout=deadline - time.monotonic())
            if pkt is None:
                return None
            data, addr = pkt
            parsed = self._parse(data)
            if not parsed:
                continue
            opcode, body = parsed
            if opcode == OP_HELLO_RES and len(body) >= 19:
                rid = body[:16]
                tcp_port = struct.unpack("<H", body[16:18])[0]
                ver = body[18]
                nc = KadContact(rid, addr[0], addr[1], tcp_port, ver)
                self.contacts[rid] = nc
                return nc
            if opcode == OP_HELLO_RES_ACK:
                return c
        return None

    def _recv(self, timeout: float = 1.0) -> Optional[Tuple[bytes, Tuple[str, int]]]:
        if timeout <= 0:
            return None
        try:
            ready, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            data, addr = self.sock.recvfrom(MAX_PACKET)
        except OSError:
            return None
        self.stats["recv"] += 1
        return data, addr

    def bootstrap(self, timeout: float = 8.0) -> bool:
        """BOOTSTRAP_REQ a slice of the contact list until nodes come back."""
        if not self.contacts:
            return False
        seeds = list(self.contacts.values())
        random.shuffle(seeds)
        seeds = seeds[:120]
        got = False
        deadline = time.monotonic() + timeout
        # fire a first wave
        for c in seeds[:40]:
            self.send(OP_BOOTSTRAP_REQ, b"", c.addr)
        while time.monotonic() < deadline:
            pkt = self._recv(timeout=min(0.5, max(0.05, deadline - time.monotonic())))
            if pkt is None:
                continue
            data, addr = pkt
            parsed = self._parse(data)
            if not parsed:
                continue
            opcode, body = parsed
            if opcode == OP_BOOTSTRAP_RES and len(body) >= 21:
                try:
                    r = _Reader(body)
                    rid = r.take(16)
                    tcp_port = r.u16()
                    ver = r.u8()
                except ValueError:
                    continue
                self.contacts.setdefault(rid, KadContact(rid, addr[0], addr[1], tcp_port, ver))
                try:
                    cnt = struct.unpack_from("<H", body, 19)[0]
                    blob = body[21:21 + cnt * 25]
                    self.add_contacts(parse_contact_blob(blob))
                except Exception:
                    pass
                got = True
            elif opcode == OP_PING:
                self.send(OP_PONG, b"", addr)
        return got

    # -- kademlia lookup ----------------------------------------------
    def find_node_round(self, target: bytes, k: int = 16, alpha: int = 8,
                        wait: float = 2.2,
                        queried: Optional[Set[bytes]] = None,
                        ) -> List[KadContact]:
        """One Kademlia FIND_NODE iteration toward ``target``."""
        queried = queried if queried is not None else set()
        cands = sorted(self.contacts.values(),
                       key=lambda c: xor_distance(c.id, target))
        batch = [c for c in cands if c.id not in queried][:alpha]
        if not batch:
            return []
        for c in batch:
            queried.add(c.id)
            payload = bytes([KAD_FIND_NODE]) + target + c.id
            self.send(OP_REQ, payload, c.addr)
        deadline = time.monotonic() + wait
        found: List[KadContact] = []
        while time.monotonic() < deadline:
            pkt = self._recv(timeout=min(0.4, max(0.02, deadline - time.monotonic())))
            if pkt is None:
                continue
            data, addr = pkt
            parsed = self._parse(data)
            if not parsed:
                continue
            opcode, body = parsed
            if opcode == OP_RES and len(body) >= 17:
                cnt = body[16]
                if len(body) >= 17 + cnt * 25:
                    new = parse_contact_blob(body[17:17 + cnt * 25])
                    self.add_contacts(new)
                    found.extend(new)
            elif opcode == OP_PING:
                self.send(OP_PONG, b"", addr)
        return found

    def lookup(self, target: bytes, rounds: int = 4,
               on_progress: Optional[Callable[[int], None]] = None
               ) -> List[KadContact]:
        """Iteratively walk the Kad DHT toward ``target``."""
        queried: Set[bytes] = set()
        for i in range(rounds):
            found = self.find_node_round(target, queried=queried)
            if on_progress:
                try:
                    on_progress(len(self.contacts))
                except Exception:
                    pass
            if not found and i >= 1:
                break
        return sorted(self.contacts.values(),
                      key=lambda c: xor_distance(c.id, target))

    # -- keyword search ------------------------------------------------
    def search(self, keyword: str, *, duration: float = 14.0,
               depth_rounds: int = 4, fanout: int = 20,
               min_sources: int = 0,
               stop_event: Optional[threading.Event] = None,
               on_hit: Optional[Callable[[KadHit], None]] = None
               ) -> List[KadHit]:
        """Search the live Kad network for ``keyword``."""
        words = keyword_words(keyword)
        if not words:
            return []
        target = keyword_target(words[0])
        started = time.monotonic()
        deadline = started + duration

        # phase 1: walk toward the target
        self.lookup(target, rounds=depth_rounds)
        if stop_event is not None and stop_event.is_set():
            return []

        # phase 2: ask the closest nodes for the keyword
        hits: Dict[bytes, KadHit] = {}
        asked: Set[bytes] = set()

        def _ask(nodes: Sequence[KadContact], count: int) -> None:
            for c in nodes:
                if c.id in asked:
                    continue
                if len(asked) >= count:
                    break
                asked.add(c.id)
                self.send(OP_SEARCH_KEY_REQ, target + struct.pack("<H", 0), c.addr)
                self.stats["res"] += 1

        closest = sorted(self.contacts.values(),
                         key=lambda c: xor_distance(c.id, target))
        _ask(closest, fanout)

        last_reask = time.monotonic()
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                break
            pkt = self._recv(timeout=min(0.5, max(0.02, deadline - time.monotonic())))
            if pkt is None:
                # periodically ask any newly discovered closer nodes
                if time.monotonic() - last_reask > 3.0:
                    last_reask = time.monotonic()
                    fresh = sorted(self.contacts.values(),
                                   key=lambda c: xor_distance(c.id, target))
                    _ask(fresh, fanout * 3)
                continue
            data, addr = pkt
            parsed = self._parse(data)
            if not parsed:
                continue
            opcode, body = parsed
            if opcode == OP_SEARCH_RES:
                for hit in self._parse_search_result(body):
                    if hit.file_hash in hits:
                        continue
                    if min_sources and hit.sources < min_sources:
                        continue
                    hits[hit.file_hash] = hit
                    self.stats["hits"] += 1
                    if on_hit:
                        try:
                            on_hit(hit)
                        except Exception:
                            pass
            elif opcode == OP_RES:
                if len(body) >= 17:
                    cnt = body[16]
                    if len(body) >= 17 + cnt * 25:
                        self.add_contacts(parse_contact_blob(body[17:17 + cnt * 25]))
            elif opcode == OP_PING:
                self.send(OP_PONG, b"", addr)
            elif opcode == OP_HELLO_RES:
                if len(body) >= 19:
                    rid = body[:16]
                    tcp_port = struct.unpack("<H", body[16:18])[0]
                    self.contacts.setdefault(
                        rid, KadContact(rid, addr[0], addr[1], tcp_port, body[18]))

        return sorted(hits.values(), key=lambda h: (-h.sources, h.name.lower()))

    @staticmethod
    def _parse_search_result(body: bytes) -> List[KadHit]:
        out: List[KadHit] = []
        try:
            r = _Reader(body)
            r.take(16)           # source node id
            r.take(16)           # search target
            count = r.u16()
            for _ in range(count):
                fhash = r.take(16)
                tags = read_tag_list(r)
                name = tags.get(TAG_FILENAME, "")
                if isinstance(name, bytes):
                    try:
                        name = name.decode("utf-8")
                    except UnicodeDecodeError:
                        name = name.decode("latin-1", "replace")
                size_v = tags.get(TAG_FILESIZE, 0)
                if isinstance(size_v, bytes):
                    if len(size_v) == 8:
                        size_v = struct.unpack("<Q", size_v)[0]
                    else:
                        size_v = 0
                sources = int(tags.get(TAG_SOURCES, 0) or 0)
                publishers = 0
                pub = tags.get(TAG_PUBLISHINFO)
                if isinstance(pub, int):
                    publishers = (pub >> 16) & 0xFF
                ft = tags.get(TAG_FILETYPE, "")
                ff = tags.get(TAG_FILEFORMAT, "")
                aich = tags.get(TAG_KADMISCOPTIONS, b"")
                if not name:
                    continue
                out.append(KadHit(
                    file_hash=fhash,
                    name=str(name),
                    size=int(size_v or 0),
                    sources=sources or publishers,
                    publishers=publishers,
                    filetype=ft if isinstance(ft, str) else "",
                    fileformat=ff if isinstance(ff, str) else "",
                    aich=aich if isinstance(aich, bytes) else b"",
                ))
        except (ValueError, KeyError, struct.error):
            pass
        return out

    def count_sources(self, file_hash: bytes, nodes: Optional[Sequence[KadContact]] = None,
                      wait: float = 3.0, size: int = 0) -> int:
        """KADEMLIA2_SEARCH_SOURCE_REQ -- real 源数 for one file hash."""
        if nodes is None:
            nodes = sorted(self.contacts.values(),
                           key=lambda c: xor_distance(c.id, file_hash))[:10]
        asked = set()
        for c in nodes:
            if c.id in asked:
                continue
            asked.add(c.id)
            self.send(OP_SEARCH_SOURCE_REQ,
                      file_hash + struct.pack("<H", 0) + struct.pack("<Q", int(size)),
                      c.addr)
        seen: Set[bytes] = set()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            pkt = self._recv(timeout=min(0.4, max(0.02, deadline - time.monotonic())))
            if pkt is None:
                continue
            data, addr = pkt
            parsed = self._parse(data)
            if not parsed:
                continue
            opcode, body = parsed
            if opcode != OP_SEARCH_RES:
                continue
            try:
                r = _Reader(body)
                r.take(16)
                r.take(16)
                cnt = r.u16()
                for _ in range(cnt):
                    src_id = r.take(16)
                    read_tag_list(r)
                    seen.add(src_id)
            except (ValueError, struct.error):
                continue
        return len(seen)


# ---------------------------------------------------------------------------
# convenience threading wrapper for the GUI
# ---------------------------------------------------------------------------

class KadSearcher:
    """Owns a long-lived KadClient and runs one search at a time."""

    def __init__(self, nodes_dat: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._client: Optional[KadClient] = None
        self._nodes_dat = nodes_dat
        self._stop = threading.Event()
        self.last_error = ""
        self.ready = False

    def start(self, timeout: float = 20.0) -> bool:
        with self._lock:
            if self._client is not None:
                return self.ready
            try:
                client = KadClient(nodes_dat=self._nodes_dat)
                client.load_bootstrap()
                if client.bootstrap(timeout=timeout):
                    self.ready = True
                self._client = client
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self.ready = False
            return self.ready

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
        self.ready = False

    def cancel(self) -> None:
        self._stop.set()

    def search(self, keyword: str, duration: float = 14.0,
               on_hit: Optional[Callable[[KadHit], None]] = None) -> List[KadHit]:
        self._stop.clear()
        client = self._client
        if client is None:
            if not self.start():
                return []
            client = self._client
        if client is None:
            return []
        try:
            return client.search(keyword, duration=duration, on_hit=on_hit,
                                 stop_event=self._stop)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []

    @property
    def contact_count(self) -> int:
        return len(self._client.contacts) if self._client else 0
