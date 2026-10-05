"""File-size and file-type helpers for the results table.

Two jobs:

* turn a byte count into something readable (``15.40 GB``) and recover a size
  from the filename when the index did not provide one — release names very
  often carry it (``...[15.4 GiB]``, ``Movie.2024.1080p.1.4GB.mkv``)
* classify a result as 视频 / 音频 / 图片 / 文档 / 程序 / 压缩包 / 光盘镜像,
  either from the integer ``FT_FILETYPE`` an eD2k server sends, or from the
  filename extension, or -- when the name carries no extension at all -- from
  the release tags (``1080p``, ``x265``, ``WebRip``, ``FLAC`` ...)

The extension tables mirror the eD2k categories (eserver 17.6+ / eMule's
``EED2KFileType``), so a result classified locally and one classified by the
server agree.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------

TYPE_UNKNOWN = "未知"
TYPE_VIDEO = "视频"
TYPE_AUDIO = "音频"
TYPE_IMAGE = "图片"
TYPE_DOCUMENT = "文档"
TYPE_PROGRAM = "程序"
TYPE_ARCHIVE = "压缩包"
TYPE_CDIMAGE = "光盘镜像"
TYPE_COLLECTION = "电驴收藏"
TYPE_OTHER = "其它"

#: display order used when sorting the 文件类型 column
TYPE_ORDER = [
    TYPE_VIDEO, TYPE_AUDIO, TYPE_IMAGE, TYPE_DOCUMENT,
    TYPE_PROGRAM, TYPE_ARCHIVE, TYPE_CDIMAGE, TYPE_COLLECTION,
    TYPE_OTHER, TYPE_UNKNOWN,
]
_TYPE_RANK = {t: i for i, t in enumerate(TYPE_ORDER)}

VIDEO_EXT = {
    "avi", "mpg", "mpeg", "mp4", "mkv", "wmv", "mov", "flv", "ogm", "m4v",
    "rm", "rmvb", "vob", "asf", "divx", "xvid", "3gp", "ts", "m2ts", "webm",
    "mpe", "ifo", "f4v", "mts", "dat", "rmv",
}
AUDIO_EXT = {
    "mp3", "mp2", "m4a", "wav", "wma", "ogg", "flac", "aac", "ac3", "aif",
    "aiff", "ape", "mpc", "mid", "midi", "ra", "wv", "opus", "dsf", "dts",
    "mka", "tta",
}
IMAGE_EXT = {
    "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "psd", "ico",
    "svg", "raw", "cr2", "nef", "heic", "avif",
}
DOCUMENT_EXT = {
    "doc", "docx", "pdf", "txt", "rtf", "odt", "xls", "xlsx", "ppt", "pptx",
    "epub", "mobi", "djvu", "chm", "tex", "ods", "odp", "csv", "md", "azw3",
    "cbz", "cbr",
}
PROGRAM_EXT = {
    "exe", "msi", "bat", "com", "dll", "deb", "rpm", "dmg", "apk", "jar",
    "app", "bin", "run", "appimage", "ipa", "sys",
}
ARCHIVE_EXT = {
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "ace", "arj", "cab", "lzh",
    "z", "tgz", "zst", "lz4", "lz", "br", "tar.gz",
}
CDIMAGE_EXT = {"iso", "nrg", "cue", "img", "mdf", "ccd", "cdi", "mds", "vcd", "bin"}
COLLECTION_EXT = {"emulecollection"}

#: extension -> category.  "bin" appears in two tables; PROGRAM is checked
#: first here, matching ed2k-server's classifier so both agree.
_EXT_MAP = {}
for _cat, _set in (
    (TYPE_AUDIO, AUDIO_EXT),
    (TYPE_VIDEO, VIDEO_EXT),
    (TYPE_IMAGE, IMAGE_EXT),
    (TYPE_PROGRAM, PROGRAM_EXT),
    (TYPE_DOCUMENT, DOCUMENT_EXT),
    (TYPE_ARCHIVE, ARCHIVE_EXT),
    (TYPE_CDIMAGE, CDIMAGE_EXT),
    (TYPE_COLLECTION, COLLECTION_EXT),
):
    for _e in _set:
        _EXT_MAP.setdefault(_e, _cat)

#: FT_FILETYPE numeric ids sent by eD2k servers (eserver 17.6+ / eMule)
ED2K_FILETYPE_IDS = {
    0: TYPE_UNKNOWN,          # ANY == "no opinion"
    1: TYPE_AUDIO,
    2: TYPE_VIDEO,
    3: TYPE_IMAGE,
    4: TYPE_PROGRAM,
    5: TYPE_DOCUMENT,
    6: TYPE_ARCHIVE,
    7: TYPE_CDIMAGE,
    8: TYPE_COLLECTION,
}
_ED2K_STR = {
    "audio": TYPE_AUDIO, "video": TYPE_VIDEO, "image": TYPE_IMAGE,
    "pro": TYPE_PROGRAM, "program": TYPE_PROGRAM, "doc": TYPE_DOCUMENT,
    "document": TYPE_DOCUMENT, "arc": TYPE_ARCHIVE, "archive": TYPE_ARCHIVE,
    "iso": TYPE_CDIMAGE, "emulecollection": TYPE_COLLECTION,
}

#: Release-name tags that give the type away when the name has no extension at
#: all.  Anime and movie packs are routinely titled
#: ``[字幕组] 进击的巨人 最终季 Part.2 [1080P][WebRip]`` -- no container to look
#: at, but the tags say "video" beyond doubt.  Without this every such row
#: showed up as 未知, which made the 文件类型 column useless for exactly the
#: searches it was added for.
_RELEASE_TAGS = (
    (TYPE_VIDEO, re.compile(
        r"(?<![a-z0-9])("
        r"2160p|1440p|1080[pi]|720p|576p|480p|uhd|4k|8k|hdr10\+?|hdr|"
        r"x264|x265|h\.?264|h\.?265|hevc|avc|av1|10bit|8bit|hi10p|"
        r"web-?dl|web-?rip|blu-?ray|bdrip|brrip|bd-?box|remux|hdtv|"
        r"dvdrip|hdrip|mini-?bd|full-?bd|"
        r"batch|合集|全集|complete\s+(?:series|season)|tv\s*rip"
        r")(?![a-z0-9])", re.I)),
    # checked second: a "1080p FLAC" release is a video, a bare "FLAC 24bit"
    # album is not
    (TYPE_AUDIO, re.compile(
        r"(?<![a-z0-9])("
        r"flac|alac|ape|wav|aac|mp3|320k(?:bps)?|24bit|hi-?res|hires|dsd|"
        r"ost|soundtrack|专辑|无损"
        r")(?![a-z0-9])", re.I)),
)

#: A real extension sits at the very end of the name, optionally followed by
#: closing brackets/spaces ("...[1080p].mkv", "ubuntu.iso]").
_EXT_AT_END = re.compile(r"\.([A-Za-z0-9]{1,6})(?=[\s\]\)】]*$)")
#: ...or a known extension appears as its own token somewhere in the name
#: ("Movie.mkv [1080p]", "Show.1080p.avi-Group").
_EXT_TOKEN = re.compile(r"\.([A-Za-z0-9]{1,6})(?![A-Za-z0-9])")


def extension_of(name: str) -> str:
    """Best-effort extension of a release name, lower-cased, without the dot.

    Anchoring matters.  ``splitext`` happily reports the extension of
    ``...[最终季 Part.2][86][1080P][WebRip]`` as ``2][86][1080p][webrip]``, i.e.
    a whole page of anime releases came out as 其它 because of a dot in
    ``Part.2``.  Here the end of the name is checked first, and the
    scan-anywhere fallback only accepts an extension we actually know.
    """
    text = (name or "").strip()
    if not text:
        return ""
    ends = _EXT_AT_END.findall(text)
    if ends:
        return ends[-1].lower()
    for cand in reversed(_EXT_TOKEN.findall(text)):
        if cand.lower() in _EXT_MAP:
            return cand.lower()
    return ""


def release_tag(name: str) -> str:
    """Category implied by release tags alone, or ``""``."""
    if not name:
        return ""
    for cat, pattern in _RELEASE_TAGS:
        if pattern.search(name):
            return cat
    return ""


def classify(name: str, hint=None) -> str:
    """Return a display category for a result.

    ``hint`` may be the integer ``FT_FILETYPE`` from an eD2k server, or the
    older string form ("Video"/"Audio"/...).  Otherwise a known file extension
    decides, then the release tags (1080p / x265 / FLAC / ...), and only then
    does an unrecognised extension mean 其它.
    """
    if hint not in (None, "", 0, "0"):
        if isinstance(hint, bool):
            pass
        elif isinstance(hint, int):
            cat = ED2K_FILETYPE_IDS.get(hint)
            if cat and cat != TYPE_UNKNOWN:
                return cat
        elif isinstance(hint, str):
            cat = _ED2K_STR.get(hint.strip().lower())
            if cat:
                return cat

    ext = extension_of(name)
    if ext and ext in _EXT_MAP:
        return _EXT_MAP[ext]
    tagged = release_tag(name)
    if tagged:
        return tagged
    if ext:
        return TYPE_OTHER
    return TYPE_UNKNOWN


def type_rank(cat: str) -> int:
    return _TYPE_RANK.get(cat, len(TYPE_ORDER))


# ---------------------------------------------------------------------------
# sizes
# ---------------------------------------------------------------------------

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def human_size(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(_UNITS) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return "%d %s" % (int(f), _UNITS[i])
    return "%.2f %s" % (f, _UNITS[i])


_SIZE_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}

#: A number with an explicit size unit, anywhere in the name.  Requiring the
#: unit is what keeps "1080p", "x264" and "5.1" from being read as sizes.
#:
#: The lookbehind only rejects a preceding *digit*, not a dot: release names
#: put sizes straight after a dot-separated tag (``...1080p.1.4GB.mkv``), and
#: rejecting a leading dot lost exactly those.  Because the pattern starts at
#: the first digit, ``1.4GB`` is consumed whole -- ``4GB`` is never matched on
#: its own.
_SIZE_IN_NAME = re.compile(
    r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*"
    r"(kib|mib|gib|tib|kb|mb|gb|tb)\b",
    re.I,
)


def size_from_name(name: str) -> int:
    """Recover a byte size embedded in a filename, or 0.

    The *last* match wins: release names put the total size at the end
    (``...[1080p][15.4 GiB].mkv``), and an early number is more likely to be
    an episode index or a year.
    """
    if not name:
        return 0
    best = 0
    for m in _SIZE_IN_NAME.finditer(name):
        try:
            val = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        unit = m.group(2).lower()
        mult = _SIZE_UNITS.get(unit)
        if not mult:
            continue
        size = int(val * mult)
        # sanity: 1 KiB .. 8 TiB.  Anything outside is noise, not a file size.
        if 1024 <= size <= 8 * 1024 ** 4:
            best = size
    return best
