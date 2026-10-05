"""Seeder ("资源数") enrichment.

Sources that only hand out a magnet or a ``.torrent`` link cannot report how
many peers a swarm has.  This module fills that in:

1. if only a ``.torrent`` URL is known, download it (a few kB), pull the
   info-hash and the announce list out of the bencoded metadata
2. ask the HTTP trackers for a **scrape** -- ``complete`` is the seeder count,
   which is exactly the 资源数 the UI shows
3. fall back to the BitTorrent DHT: a ``get_peers`` walk returns the peers the
   network currently knows about, which is a good proxy for swarm size

Everything here is best effort and runs on a thread pool.
"""
from __future__ import annotations

import binascii
import hashlib
import logging
import random
import socket
import struct
import threading
import time
import urllib.parse
from typing import Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

from . import gateway
from .gateway import scope
from .http import HTTP
from .links import magnet_infohash, parse_ed2k
from .models import KIND_MAGNET, KIND_TORRENT, SearchResult
from .parallel import pmap

log = logging.getLogger("tsearch.enrich")

#: extra trackers used when a torrent carries none (or only dead ones).
#: Both HTTP (scrape via ``/scrape``) and UDP (BEP-15 scrape) are supported.
FALLBACK_TRACKERS: Tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
    "http://bt1.archive.org:6969/announce",
    "http://bt2.archive.org:6969/announce",
)

#: How many trackers one row asks.  The exit counts *requests*, so asking all
#: twelve every time is what fills the queue with work nobody will use.  The
#: ones asked are the ones that have been answering (:func:`_ordered`).
MAX_HTTP = 4
MAX_UDP = 4

#: tracker -> how often it has answered, 0..1 (exponential moving average)
_TRACKER_HITS: Dict[str, float] = {}
_TRACKER_LOCK = threading.Lock()


def _note(tracker: str, answered: bool) -> None:
    """Remember whether a tracker answered, so the live ones are asked first.

    Most of :data:`FALLBACK_TRACKERS` are dead at any given moment, and a row
    that tries them in declaration order spends its whole budget on hosts that
    will never reply.  The key is per *tracker*, not per swarm: the URL differs
    for every infohash, the tracker behind it does not.
    """
    with _TRACKER_LOCK:
        prev = _TRACKER_HITS.get(tracker, 0.5)
        _TRACKER_HITS[tracker] = prev * 0.7 + (0.3 if answered else 0.0)


def _ordered(trackers: Sequence[str]) -> List[str]:
    """De-duplicated, best-first.  Stable, so an unproven tracker keeps its
    declared position until it has a track record."""
    with _TRACKER_LOCK:
        score = dict(_TRACKER_HITS)
    return sorted(dict.fromkeys(trackers),
                  key=lambda t: -score.get(t, 0.5))


def _tracker_key(url: str) -> str:
    """The tracker behind a scrape URL, without the per-swarm query string."""
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------------
# bencode (read-only, just enough for .torrent files)
# ---------------------------------------------------------------------------

class _Bencode:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def parse(self):
        c = self.data[self.pos:self.pos + 1]
        if c == b"d":
            return self._dict()
        if c == b"l":
            return self._list()
        if c == b"i":
            return self._int()
        return self._bytes()

    def _dict(self):
        self.pos += 1
        out = {}
        while self.data[self.pos:self.pos + 1] != b"e":
            k = self._bytes()
            out[k] = self.parse()
        self.pos += 1
        return out

    def _list(self):
        self.pos += 1
        out = []
        while self.data[self.pos:self.pos + 1] != b"e":
            out.append(self.parse())
        self.pos += 1
        return out

    def _int(self):
        self.pos += 1
        end = self.data.index(b"e", self.pos)
        val = int(self.data[self.pos:end])
        self.pos = end + 1
        return val

    def _bytes(self):
        end = self.data.index(b":", self.pos)
        n = int(self.data[self.pos:end])
        start = end + 1
        self.pos = start + n
        return self.data[start:self.pos]


def torrent_info_hash(data: bytes) -> Tuple[str, List[str], str, int]:
    """-> (infohash_hex, trackers, name, total_size)."""
    try:
        meta = _Bencode(data).parse()
    except Exception:
        return "", [], "", 0
    if not isinstance(meta, dict):
        return "", [], "", 0
    info = meta.get(b"info")
    if not isinstance(info, dict):
        return "", [], "", 0
    # re-encode exactly the info dict to hash it
    raw = _encode(info)
    ih = hashlib.sha1(raw).hexdigest()
    name = ""
    if isinstance(info.get(b"name"), bytes):
        try:
            name = info[b"name"].decode("utf-8")
        except UnicodeDecodeError:
            name = info[b"name"].decode("latin-1", "replace")
    size = 0
    if isinstance(info.get(b"length"), int):
        size = info[b"length"]
    elif isinstance(info.get(b"files"), list):
        for f in info[b"files"]:
            if isinstance(f, dict) and isinstance(f.get(b"length"), int):
                size += f[b"length"]
    trackers: List[str] = []
    ann = meta.get(b"announce")
    if isinstance(ann, bytes):
        trackers.append(ann.decode("latin-1", "replace"))
    for tier in (meta.get(b"announce-list") or []):
        if isinstance(tier, list):
            for t in tier:
                if isinstance(t, bytes):
                    trackers.append(t.decode("latin-1", "replace"))
        elif isinstance(tier, bytes):
            trackers.append(tier.decode("latin-1", "replace"))
    seen = set()
    uniq = []
    for t in trackers:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return ih, uniq, name, size


def _encode(obj) -> bytes:
    if isinstance(obj, dict):
        out = b"d"
        for k in sorted(obj.keys()):
            out += _encode(k) + _encode(obj[k])
        return out + b"e"
    if isinstance(obj, list):
        return b"l" + b"".join(_encode(x) for x in obj) + b"e"
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:" % len(obj) + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return b"%d:" % len(b) + b
    return b""


# ---------------------------------------------------------------------------
# tracker scrape
# ---------------------------------------------------------------------------

def _scrape_urls(trackers: Sequence[str], infohash: bytes) -> List[str]:
    out = []
    for tr in trackers:
        tr = (tr or "").strip()
        if not tr.startswith("http"):
            continue
        if "/announce" in tr:
            base = tr.replace("/announce", "/scrape")
        elif tr.endswith("/announce/"):
            base = tr[:-1].replace("/announce", "/scrape")
        else:
            continue
        out.append(base + ("&" if "?" in base else "?") +
                   "info_hash=" + urllib.parse.quote(infohash, safe=""))
    return out


def _udp_scrape_one(tracker: str, infohash: bytes,
                    timeout: float = 3.0) -> Optional[Tuple[int, int]]:
    """BEP-15 UDP tracker scrape -> (seeders, leechers).

    Connect request  : magic 0x41727101980 | action 0 | txid
    Connect response : action 0 | txid | connection_id(8)
    Scrape request   : connection_id(8) | action 2 | txid | info_hash(20)
    UDP trackers open their own socket and never touch :mod:`src.core.http`,
    which is exactly why they used to be invisible to every concurrency limit
    in this program -- measured at ~55% of all outbound requests during the
    资源数 phase.  They queue at the same gate as everything else, keyed by
    tracker host so the per-host limit catches them too.
    """
    import struct as _s
    from urllib.parse import urlparse

    try:
        u = urlparse(tracker)
        host = u.hostname
        port = u.port or 6969
    except Exception:
        return None
    if not host:
        return None

    with gateway.slot(timeout, tracker) as h:
        if h is None:
            return None        # exit jammed, or this phase is already over
        try:
            addrs = socket.getaddrinfo(host, port, socket.AF_INET,
                                       socket.SOCK_DGRAM)
            if not addrs:
                return None
            target = addrs[0][4]
        except OSError:
            return None

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            tx = random.getrandbits(32)
            sock.sendto(_s.pack(">QII", 0x41727101980, 0, tx), target)
            data, _ = sock.recvfrom(2048)
            if len(data) < 16:
                return None
            action, rtx = _s.unpack(">II", data[:8])
            if action != 0 or rtx != tx:
                return None
            conn_id = _s.unpack(">Q", data[8:16])[0]

            tx2 = random.getrandbits(32)
            sock.sendto(_s.pack(">QII", conn_id, 2, tx2) + infohash, target)
            data, _ = sock.recvfrom(2048)
            if len(data) < 20:
                return None
            action, rtx = _s.unpack(">II", data[:8])
            if action != 2 or rtx != tx2:
                return None
            seeders, _completed, leechers = _s.unpack(">III", data[8:20])
            return int(seeders), int(leechers)
        except (OSError, _s.error, ValueError):
            return None
        finally:
            sock.close()


def http_scrape(infohash_hex: str, trackers: Sequence[str],
                timeout: float = 6.0,
                budget: float = 9.0) -> Optional[Tuple[int, int]]:
    """Query HTTP *and* UDP trackers for a swarm's seeder/leecher counts.

    All candidates are tried **in parallel** under a single wall clock budget:
    doing this sequentially against a dozen mostly-dead trackers is what makes
    an otherwise instant lookup feel broken.
    """
    try:
        ih = binascii.unhexlify(infohash_hex)
    except (binascii.Error, ValueError):
        return None
    if len(ih) != 20:
        return None

    # Ask a handful, not all twelve.
    #
    # Firing every candidate at once means the requests that get onto the wire
    # first are decided by luck.  With a cap on the exit, the ones left in the
    # queue still cost something: they displace requests belonging to *other*
    # rows that have not got an answer yet.  So ask fewer, chosen by which
    # trackers have actually answered recently (:func:`_ordered`), and stop as
    # soon as one of them reports seeders.
    #
    # Splitting them into waves of three was tried first and measured *worse*
    # (three searches at once: 24% of rows filled, against 62% for one big
    # wave): a small wave queues behind other rows and burns the row's own
    # budget before it is ever served.
    ordered = _ordered(trackers)
    http_urls = _scrape_urls(ordered, ih)[:MAX_HTTP]
    udp_urls = [t for t in ordered
                if (t or "").lower().startswith("udp://")][:MAX_UDP]
    if not http_urls and not udp_urls:
        return None

    def _one_http(url: str):
        raw = HTTP.get_bytes(url, timeout=timeout, max_bytes=1 << 18)
        _note(_tracker_key(url), bool(raw))
        if not raw:
            return None
        try:
            obj = _Bencode(raw).parse()
        except Exception:
            return None
        entry = (obj.get(b"files") or {}).get(ih) or {}
        complete = int(entry.get(b"complete", 0) or 0)
        incomplete = int(entry.get(b"incomplete", 0) or 0)
        return (complete, incomplete) if (complete or incomplete) else None

    def _one_udp(url: str):
        got = _udp_scrape_one(url, ih, timeout=min(3.0, timeout))
        _note(url, got is not None)
        if got and (got[0] or got[1]):
            return got
        return None

    jobs = []
    for u in http_urls:
        jobs.append((_one_http, u))
    for u in udp_urls:
        jobs.append((_one_udp, u))
    if not jobs:
        return None

    best: Optional[Tuple[int, int]] = None
    workers = max(1, min(12, len(jobs)))

    def _seen(_job, got, _err) -> None:
        nonlocal best
        if got and (best is None or (got[0] + got[1]) > (best[0] + best[1])):
            best = got

    def _run_job(job):
        fn, url = job
        return fn(url)

    # One wave, cut short as soon as a tracker reports seeders.
    #
    # Splitting it into waves of three was tried first and measured *worse*:
    # three searches at once filled 24% of rows against 62% for one wave.  A
    # small wave queues behind the other rows and burns its own budget before
    # it is ever served.  Fewer candidates chosen by what has answered lately
    # (above) is what actually reduces the load.
    pmap(_run_job, jobs, workers=max(1, min(len(jobs), len(jobs))),
         timeout=max(1.0, budget),
         on_result=_seen, stop_when=lambda got: bool(got and got[0] > 0))
    return best


# ---------------------------------------------------------------------------
# minimal BitTorrent DHT client (get_peers only)
# ---------------------------------------------------------------------------

DHT_ROUTERS: Tuple[Tuple[str, int], ...] = (
    ("dht.transmissionbt.com", 6881),
    ("router.bittorrent.com", 6881),
    ("dht.libtorrent.org", 25401),
    ("router.utorrent.com", 6881),
    ("dht.aelitis.com", 6881),
)


class DhtPeerCounter:
    """Counts peers for an info-hash via DHT ``get_peers``.

    A full DHT client is overkill: bootstrapping from the public routers and
    querying the closest nodes once is enough to get a useful swarm estimate.
    """

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout

    @staticmethod
    def _bdecode_nodes(raw: bytes) -> List[Tuple[bytes, Tuple[str, int]]]:
        out = []
        for i in range(0, len(raw) - 25, 26):
            nid = raw[i:i + 20]
            ip = socket.inet_ntoa(raw[i + 20:i + 24])
            port = struct.unpack(">H", raw[i + 24:i + 26])[0]
            if port:
                out.append((nid, (ip, port)))
        return out

    def count(self, infohash_hex: str) -> int:
        try:
            ih = binascii.unhexlify(infohash_hex)
        except (binascii.Error, ValueError):
            return 0
        if len(ih) != 20:
            return 0

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        my_id = random.getrandbits(160).to_bytes(20, "big")
        peers: set = set()
        nodes: List[Tuple[str, int]] = []
        pending: Dict[bytes, str] = {}

        def query(target_host, target_port, want: bytes):
            tid = bytes([random.randrange(256)]) + b"ts"
            msg = (b"d1:ad2:id20:" + my_id + b"13:info_hash20:" + want +
                   b"e1:q9:get_peers1:t2:" + tid + b"1:y1:qe")
            try:
                sock.sendto(msg, (target_host, target_port))
                pending[tid] = target_host
            except OSError:
                pass

        deadline = time.monotonic() + self.timeout
        for host, port in DHT_ROUTERS:
            try:
                nodes.append((socket.gethostbyname(host), port))
            except OSError:
                continue
        if not nodes:
            sock.close()
            return 0
        for ip, port in nodes[:4]:
            query(ip, port, ih)

        probed: set = set()
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                # keep walking whatever nodes we learned about
                fresh = [(ip, p) for (ip, p) in nodes if (ip, p) not in probed]
                if not fresh:
                    break
                for ip, p in fresh[:6]:
                    probed.add((ip, p))
                    query(ip, p, ih)
                continue
            except OSError:
                break
            tid = data[1:3]
            kind = data[2:3]
            if b"2:y1:r" not in data and b"1:r" not in data:
                continue
            if b"5:token" not in data and b"6:values" not in data and b"5:nodes" not in data:
                continue
            try:
                msg = _Bencode(data).parse()
            except Exception:
                continue
            r = msg.get(b"r") or {}
            for v in (r.get(b"values") or []):
                if isinstance(v, bytes) and len(v) == 6:
                    peers.add((socket.inet_ntoa(v[:4]),
                               struct.unpack(">H", v[4:6])[0]))
            for nid, addr2 in self._bdecode_nodes(r.get(b"nodes") or b""):
                if addr2 not in probed and len(probed) < 40:
                    probed.add(addr2)
                    query(addr2[0], addr2[1], ih)
            if len(peers) > 400:
                break
        sock.close()
        return len(peers)


_DHT = DhtPeerCounter()
_DHT_LOCK = threading.Semaphore(3)


def dht_peer_count(infohash_hex: str, timeout: float = 10.0) -> int:
    if not _DHT_LOCK.acquire(timeout=timeout + 4):
        return 0
    try:
        return _DHT.count(infohash_hex)
    except Exception:
        return 0
    finally:
        _DHT_LOCK.release()


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def resolve_torrent(url: str, timeout: float = 12.0) -> Tuple[str, List[str], str, int]:
    if not url:
        return "", [], "", 0
    data = HTTP.get_bytes(url, timeout=timeout, max_bytes=3 << 20)
    if not data or not data.startswith(b"d"):
        return "", [], "", 0
    return torrent_info_hash(data)


def enrich_one(result: SearchResult, use_dht: bool = True,
               timeout: float = 10.0) -> SearchResult:
    """Fill in ``seeds`` (and infohash/size) for a single result."""
    if result.kind not in (KIND_MAGNET, "torrent"):
        return result
    started = time.monotonic()

    def left(default: float) -> float:
        remain = timeout - (time.monotonic() - started)
        return max(1.0, min(default, remain))

    ih = result.infohash or magnet_infohash(result.link) or ""
    trackers: List[str] = []
    if result.link.startswith("magnet:"):
        try:
            q = urllib.parse.parse_qs(result.link.split("?", 1)[1])
            trackers = [urllib.parse.unquote(t) for t in q.get("tr", [])]
        except Exception:
            trackers = []

    if not ih:
        turl = result.extra.get("torrent_url") or ""
        if not turl and result.link.lower().endswith(".torrent"):
            turl = result.link
        if turl:
            got_ih, got_tr, name, size = resolve_torrent(turl, timeout=left(8.0))
            if got_ih:
                ih = got_ih
                trackers = trackers or got_tr
                result.infohash = ih
                if size:
                    # authoritative: supersedes any guess taken from the name
                    result.set_size(size)
                if result.link.lower().endswith(".torrent"):
                    from .links import build_magnet
                    result.link = build_magnet(ih, result.name or name, size,
                                               trackers or None)
                    result.kind = KIND_MAGNET
    if not ih:
        result.seeds_verified = True
        return result

    # a magnet can carry the exact size in its xl= parameter
    if not result.size or result.extra.get("size_from_name"):
        from .links import magnet_size
        xl = magnet_size(result.link)
        if xl:
            result.set_size(xl)

    counts = http_scrape(ih, (trackers + list(FALLBACK_TRACKERS)),
                         timeout=left(3.5), budget=left(6.0))
    if counts and (counts[0] or counts[1]):
        result.seeds = counts[0]
        result.peers = counts[1]
        result.seeds_verified = True
        return result

    if use_dht and (time.monotonic() - started) < timeout:
        n = dht_peer_count(ih, timeout=left(8.0))
        if n:
            result.seeds = max(result.seeds, n)
            result.seeds_verified = True
            return result

    result.seeds_verified = True
    return result


def enrich(results: Sequence[SearchResult], workers: int = 8,
           use_dht: bool = True, on_done=None,
           stop_event: Optional[threading.Event] = None,
           deadline: Optional[float] = None,
           owner: Optional[Hashable] = None) -> None:
    """Enrich in place; calls ``on_done(result)`` as each one finishes.

    ``deadline`` (``time.monotonic()`` value) is a hard wall-clock stop for the
    whole pass, and it also shortens the per-row budget as time runs out.  The
    caller is a background nicety: without a deadline, 100 rows whose trackers
    never answer keep the search "running" for minutes, which is exactly what
    makes the program feel slow.

    ``owner`` is the search these rows belong to; the gate uses it to split
    capacity fairly between concurrent searches and to let a cancelled search
    return its slots.
    """
    targets = [r for r in results if not r.seeds and r.kind in (KIND_MAGNET, KIND_TORRENT)]
    if not targets:
        return

    class _StopWhen:
        """``pmap`` takes one event; this one trips on any of several."""

        def __init__(self, *events) -> None:
            self._events = [e for e in events if e is not None]

        def is_set(self) -> bool:
            return any(e.is_set() for e in self._events)

    expired = threading.Event()
    timer = None
    if deadline is not None:
        timer = threading.Timer(max(0.0, deadline - time.monotonic()),
                                expired.set)
        timer.daemon = True
        timer.start()

    def _one(r: SearchResult) -> SearchResult:
        # Enrichment is background polish: it marks its traffic low priority so
        # a second search's index fetches -- what the user is actually waiting
        # for -- can always get onto the wire.  The cap itself lives down in
        # http.py / _udp_scrape_one, where the requests actually are.
        budget = 10.0
        if deadline is not None:
            budget = max(2.0, min(budget, deadline - time.monotonic()))
        enrich_one(r, use_dht, timeout=budget)
        return r

    def _report(r: SearchResult, _value, error: Optional[BaseException]) -> None:
        if error is not None:
            r.seeds_verified = True
        if on_done:
            try:
                on_done(r)
            except Exception:  # noqa: BLE001
                pass

    try:
        # Background polish: low priority, and the deadline rides along with
        # the pool so a request nobody is waiting for is not sent at all.
        with scope(high=False, deadline=deadline or 0.0,
                   stop=_StopWhen(stop_event, expired), owner=owner):
            pmap(_one, targets, workers=max(1, min(workers, len(targets))),
                 cancel=_StopWhen(stop_event, expired), on_result=_report)
    finally:
        if timer is not None:
            timer.cancel()
