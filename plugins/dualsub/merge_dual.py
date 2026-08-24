#!/usr/bin/env python3
"""双语字幕合并核心模块。

把中英两轨字幕合成为一个"上下双行双语"SRT。
纯标准库实现, 无第三方依赖, 适合 MoviePilot 插件直接导入。
"""
import re

# 单个时间戳: 00:00:00,000
_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
# 整行时间轴: 起始 --> 结束
_LINE_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _to_seconds(h, m, s, ms):
    return h * 3600 + m * 60 + s + ms / 1000.0


def _fmt(sec):
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


class SubtitleItem:
    __slots__ = ("start", "end", "text")

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def parse_srt(content):
    """解析 SRT 字符串为 SubtitleItem 列表。content 可以是 str 或 bytes。"""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    # 去掉 BOM
    if content.startswith("\ufeff"):
        content = content[1:]
    blocks = re.split(r"\n\s*\n", content.strip())
    items = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].isdigit():
            lines = lines[1:]
        if not lines:
            continue
        time_idx = None
        m = None
        for idx, ln in enumerate(lines):
            m = _LINE_RE.search(ln)
            if m:
                time_idx = idx
                break
        if time_idx is None:
            continue
        start = _to_seconds(int(m[1]), int(m[2]), int(m[3]), int(m[4]))
        end = _to_seconds(int(m[5]), int(m[6]), int(m[7]), int(m[8]))
        text = "\n".join(lines[time_idx + 1:])
        items.append(SubtitleItem(start, end, text))
    return items


def merge_dual(zh_items, en_items, gap=0.1, order="en_first"):
    """合并中英双语。返回 (merged_items, stats)。

    order: "en_first" 英文在上 / "zh_first" 中文在上
    """
    zh_items.sort(key=lambda x: x.start)
    en_items.sort(key=lambda x: x.start)
    merged = []
    used_zh = set()
    # 统计
    paired = 0
    en_only = 0
    zh_only = 0

    def _dual(en_text, zh_text):
        if order == "zh_first":
            return f"{zh_text}\n{en_text}"
        return f"{en_text}\n{zh_text}"

    for en in en_items:
        best = None
        best_key = -1.0
        for i, zh in enumerate(zh_items):
            if i in used_zh:
                continue
            overlap = min(en.end, zh.end) - max(en.start, zh.start)
            if overlap > best_key:
                best = i
                best_key = overlap
        # 接受 重叠 或 相邻(<gap)
        if best is not None and best_key >= -gap:
            used_zh.add(best)
            zh = zh_items[best]
            start = min(en.start, zh.start)
            end = max(en.end, zh.end)
            merged.append(SubtitleItem(start, end, _dual(en.text, zh.text)))
            paired += 1
        else:
            merged.append(en)
            en_only += 1

    for i, zh in enumerate(zh_items):
        if i not in used_zh:
            merged.append(zh)
            zh_only += 1

    merged.sort(key=lambda x: x.start)
    stats = {"paired": paired, "en_only": en_only, "zh_only": zh_only, "total": len(merged)}
    return merged, stats


def render_srt(items):
    lines = []
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}\n{_fmt(item.start)} --> {_fmt(item.end)}\n{item.text}\n")
    return "\n".join(lines)
