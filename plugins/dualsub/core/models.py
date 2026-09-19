#!/usr/bin/env python3
"""字幕生成核心数据模型与工具方法。"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, List

# 常见视频扩展名
VIDEO_EXTS = {
    ".mkv", ".mp4", ".ts", ".avi", ".wmv", ".m2ts", ".mov", ".flv",
    ".webm", ".m4v", ".rmvb", ".rm", ".3gp", ".mpg", ".mpeg", ".vob"
}

# 常见外挂字幕扩展名
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

# 单个时间戳正则: 00:00:00,000 或 00:00:00.000
_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_LINE_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)

# CJK 字符判定正则
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
    r"\uff00-\uffef\u2600-\u27bf\U0001f300-\U0001faff]"
)

# 日文平假名/片假名正则
_JA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")

# ASS/SSA 覆盖标签 {\anN}, {\bN} 等
_TAG_RE = re.compile(r"\{[^}]*\}")

# 对话前缀: - 或 ♪ 等
_DIALOG_RE = re.compile(r"^\s*[-‐–—\u2010-\u2015*•·]")


def _to_seconds(h: int, m: int, s: int, ms: int) -> float:
    return h * 3600 + m * 60 + s + ms / 1000.0


def _fmt_srt_time(sec: float) -> str:
    if sec < 0:
        sec = 0
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec)
    if ms >= 1000:
        ms -= 1000
        s += 1
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_ass_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    total_cs = int(round(sec * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def has_cjk(text: str) -> bool:
    """文本是否包含中日韩字符"""
    return bool(_CJK_RE.search(text or ""))


def has_japanese(text: str) -> bool:
    """文本是否包含日文假名"""
    return bool(_JA_RE.search(text or ""))


import html

# HTML 样式标签 (<i>, </i>, <b>, </b>, <font ...>, <u>, etc.)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z]+(?:\s+[^>]*)?>")


def clean_subtitle_text(text: str) -> str:
    """彻底去除字幕中的 ASS/SSA 特效标签 {\\an8}、HTML 样式标签 (<i>, <b>, <font> 等)，并解码 HTML 字符实体。"""
    if not text:
        return ""
    # 去除 ASS 特效标签 {\...}
    t = _TAG_RE.sub("", text)
    # 去除 HTML 标签 <i>, </i>, <b>, <font ...> 等
    t = _HTML_TAG_RE.sub("", t)
    # 解码 HTML 字符实体如 &amp;, &nbsp;, &lt;, &gt;, &#39;
    t = html.unescape(t)
    return t


def strip_ass_tags(text: str) -> str:
    """去除 ASS/SSA 覆盖标记及 HTML 标签，保留纯净文本"""
    return clean_subtitle_text(text)


def is_chinese_lang(lang: str) -> bool:
    l = (lang or "").lower()
    return l in {
        "chi", "zho", "zh", "chs", "cht", "zh-hans", "zh-hant",
        "zh-cn", "zh-tw", "zh-hk", "zh-sg", "cmn", "yue", "chinese",
        "sc", "tc", "gb", "big5", "mandarin"
    }


def is_english_lang(lang: str) -> bool:
    l = (lang or "").lower()
    return l in {"eng", "en", "en-us", "en-gb", "english"}


def is_japanese_lang(lang: str) -> bool:
    l = (lang or "").lower()
    return l in {"jpn", "ja", "japanese", "jp"}


def is_korean_lang(lang: str) -> bool:
    l = (lang or "").lower()
    return l in {"kor", "ko", "korean", "kr"}


class SubtitleItem:
    __slots__ = ("start", "end", "text")

    def __init__(self, start: float, end: float, text: str):
        self.start = float(start)
        self.end = float(end)
        self.text = str(text or "")

    def __repr__(self):
        return f"SubtitleItem({self.start:.2f}->{self.end:.2f}, {self.text[:20]!r})"


@dataclass
class TrackInfo:
    index: int
    lang: str
    title: str
    codec: str = ""
    is_external: bool = False
    external_path: Optional[Path] = None


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    IGNORED = "ignored"
    FAILED = "failed"


class TaskSource(Enum):
    MANUAL = "manual"
    EVENT = "event"


@dataclass
class TaskItem:
    task_id: str
    video_file: str
    source: str
    add_time: str
    status: str = TaskStatus.PENDING.value
    complete_time: Optional[str] = None
    message: str = ""
