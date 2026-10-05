"""文件大小 / 文件类型 两列的回归测试（不需要联网）。

跑法： python tests\\test_filetypes.py

这里的用例都来自真实搜索结果 —— 尤其是 nyaa/dmhy 的动画发布名，
它们没有扩展名，只有 [1080P][WebRip][x265] 之类的标签。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.core.filetypes import (TYPE_ARCHIVE, TYPE_AUDIO, TYPE_CDIMAGE,  # noqa: E402
                                TYPE_DOCUMENT, TYPE_OTHER, TYPE_PROGRAM,
                                TYPE_UNKNOWN, TYPE_VIDEO, classify,
                                human_size, release_tag, size_from_name)
from src.core.models import SearchResult  # noqa: E402

fails = []


def check(got, want, label):
    ok = got == want
    print("  [%s] %-58s %r" % ("PASS" if ok else "FAIL", label, got))
    if not ok:
        fails.append("%s: got %r want %r" % (label, got, want))


print("== 文件类型：真实发布名 ==")
# the ones that used to come out as 未知 or 其它
check(classify("[桜都字幕组] 进击的巨人 The Final Season Part 2 / Shingeki no Kyojin"),
      TYPE_UNKNOWN, "动画名（无标签无扩展名）-> 未知")
check(classify("[酷漫404][进击的巨人 最终季 Part.2][86][1080P][WebRip][简日双语]"),
      TYPE_VIDEO, "Part.2 + 1080P + WebRip -> 视频（不是 其它）")
check(classify("[DBD-Raws][火影忍者 疾风传/Naruto Shippuuden][386-412TV][BDRip][1080P][AVC]"),
      TYPE_VIDEO, "BDRip/1080P/AVC -> 视频")
check(classify("火影忍者疾风传.第480话.简繁字幕.Naruto.Shippuden.1080p.Starz"),
      TYPE_VIDEO, "1080p -> 视频")
check(classify("进击的巨人 最终季 合集 [BDRip 2160p HEVC]"), TYPE_VIDEO, "2160p HEVC -> 视频")
check(classify("[Nekomoe kissaten][Bocchi the Rock!][01-12][1080p][JPTC]"),
      TYPE_VIDEO, "1080p（中文括号标签）-> 视频")
check(classify("专辑 - 久石让 24bit FLAC"), TYPE_AUDIO, "FLAC 24bit -> 音频")
check(classify("Some Album (2024) [FLAC]"), TYPE_AUDIO, "FLAC -> 音频")

print("\n== 文件类型：扩展名优先，且不再被 Part.2 骗到 ==")
check(classify("ubuntu-24.04.4-desktop-amd64.iso"), TYPE_CDIMAGE, ".iso -> 光盘镜像")
check(classify("Some.Movie.2024.1080p.BluRay.x265.mkv"), TYPE_VIDEO, ".mkv -> 视频")
check(classify("Movie.mkv [1080p]"), TYPE_VIDEO, "扩展名后面还有标签")
check(classify("setup.exe"), TYPE_PROGRAM, ".exe -> 程序")
check(classify("book.pdf"), TYPE_DOCUMENT, ".pdf -> 文档")
check(classify("pack.7z"), TYPE_ARCHIVE, ".7z -> 压缩包")
check(classify("Some.Release.Part.2][86][1080P][WebRip]"), TYPE_VIDEO, "Part.2] 不当扩展名")
check(classify("mystery.bin"), TYPE_PROGRAM, ".bin -> 程序（和 ed2k 分类一致）")
check(classify("weird.qqq"), TYPE_OTHER, "不认识的扩展名 -> 其它")
check(classify("no-extension-at-all"), TYPE_UNKNOWN, "什么都没有 -> 未知")

print("\n== 文件类型：eD2k 服务器给的 hint 优先 ==")
check(classify("whatever.avi", 2), TYPE_VIDEO, "FT_FILETYPE=2 -> 视频")
check(classify("whatever", "Audio"), TYPE_AUDIO, "字符串 hint 'Audio' -> 音频")
check(classify("whatever.avi", 0), TYPE_VIDEO, "hint=0 (ANY) 时看扩展名")

print("\n== release_tag ==")
check(release_tag("Show S01 1080p WEB-DL x264"), TYPE_VIDEO, "1080p/WEB-DL/x264")
check(release_tag("Album [FLAC]"), TYPE_AUDIO, "FLAC")
check(release_tag("ubuntu-24.04.iso"), "", "没有标签")

print("\n== 从文件名解析大小 ==")
check(size_from_name("ubuntu-24.04.4-desktop-amd64.iso"), 0, "没有大小")
check(size_from_name("Show [1080p][15.4 GiB].mkv"), int(15.4 * 1024 ** 3), "15.4 GiB")
check(size_from_name("Movie.2024.1080p.1.4GB.mkv"), int(1.4 * 1000 ** 3), "1080p.1.4GB（点分隔标签后面）")
check(size_from_name("Show.1080p.x265.1.4GB-Group"), int(1.4 * 1000 ** 3), "x265 后面的大小")
check(size_from_name("Show 1080p x264"), 0, "1080p / x264 不是大小")
check(size_from_name("Show 5.1声道 12集"), 0, "5.1 / 12 不是大小")
check(size_from_name("A 900MB B 1.5GB"), int(1.5 * 1000 ** 3), "取最后一个（总大小）")

print("\n== human_size ==")
check(human_size(0), "", "0 -> 空")
check(human_size(1536), "1.50 KB", "1536 B")
check(human_size(1024 ** 3), "1.00 GB", "1 GiB")

print("\n== SearchResult 把两列接上 ==")
r = SearchResult(name="Show [1080p][4.4 GB].mkv", link="magnet:?xt=urn:btih:" + "a" * 40)
check(r.type_display, TYPE_VIDEO, "type_display")
check(bool(r.size_bytes), True, "size_bytes 从名字里解析出来")
check(r.size_display.endswith("?"), True, "猜出来的大小带问号：%s" % r.size_display)
r.set_size(4 * 1024 ** 3)
check(r.size_display, "4.00 GB", "set_size 之后是权威值（没有问号）")

print("\n%s" % ("ALL OK" if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
sys.exit(1 if fails else 0)
