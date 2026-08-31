#!/usr/bin/env python3
"""双语字幕合并核心模块。

把中英两轨字幕合成为"上下双行双语"SRT。
也支持单轨中英混合字幕: 先按行分离中英, 再合并为标准双行。
纯标准库实现, 无第三方依赖, 适合 MoviePilot 插件直接导入。
"""
import re

# 单个时间戳: 00:00:00,000
_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
# 整行时间轴: 起始 --> 结束
_LINE_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)

# CJK 字符判定 (中日韩统一表意文字 + 兼容表意文字 + 扩展)
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
    r"\uff00-\uffef\u2600-\u27bf\U0001f300-\U0001faff]"
)
# ASS/SSA 覆盖标记 {\anN}, {\bN} 等
_TAG_RE = re.compile(r"\{[^}]*\}")
# 对话前缀: - 或 ♪ 等
_DIALOG_RE = re.compile(r"^\s*[-‐–—\u2010-\u2015*•·]")


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


def _has_cjk(text):
    """文本是否含中日韩字符"""
    return bool(_CJK_RE.search(text))


def _strip_tags(text):
    """去除 ASS/SSA 覆盖标记 {\\an8} 等, 保留纯文本"""
    return _TAG_RE.sub("", text)


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


def compact_text(text, max_lines=2):
    """把多行文本压缩到最多 max_lines 行, 避免挡画面。

    策略:
    - 去除 ASS 覆盖标记 {\\anN}
    - 按是否含 CJK 字符把行分成英文组 / 中文组
    - 每组用空格连接成一行 (保留对话前缀 - 的首行前缀)
    - 英文组在上, 中文组在下 (或反之由调用方决定)
    - max_lines=2: 英文一行 + 中文一行
    """
    if not text:
        return text
    # 去除 ASS 标记
    raw_lines = [_strip_tags(ln).rstrip() for ln in text.splitlines()]
    raw_lines = [ln for ln in raw_lines if ln.strip()]
    if not raw_lines:
        return ""

    # 分组: 中文行 / 英文行
    zh_lines = []
    en_lines = []
    for ln in raw_lines:
        if _has_cjk(ln):
            zh_lines.append(ln)
        else:
            en_lines.append(ln)

    def _join(lines):
        """把同语言的多行合成一行, 保留首行对话前缀"""
        if not lines:
            return ""
        if len(lines) == 1:
            return lines[0]
        # 保留首行前缀 (-), 其余行去掉前缀后用空格连接
        first = lines[0]
        rest = [_DIALOG_RE.sub("", ln).strip() for ln in lines[1:]]
        first_no = _DIALOG_RE.sub("", first).strip()
        # 如果首行有对话前缀, 保留它
        if _DIALOG_RE.search(first):
            return f"- {first_no} " + " ".join(rest)
        return first_no + " " + " ".join(rest)

    out_en = _join(en_lines)
    out_zh = _join(zh_lines)

    # 按 max_lines 组合: 英文在上 / 中文在下
    parts = []
    if out_en:
        parts.append(out_en)
    if out_zh:
        parts.append(out_zh)
    # 如果只有一种语言且超过 max_lines, 截断
    if len(parts) > max_lines:
        parts = parts[:max_lines]
    return "\n".join(parts)


def split_mixed(items):
    """从单轨中英混合字幕里分离中文行和英文行。

    每条字幕可能含多行, 交替排列中英文。
    返回 (zh_items, en_items), 保留原始时间轴。
    用于把"一条 chi 轨但内容是中英混合"的情况拆成双轨供 merge_dual 配对。
    """
    zh_items = []
    en_items = []
    for it in items:
        lines = [_strip_tags(ln).rstrip() for ln in it.text.splitlines()]
        lines = [ln for ln in lines if ln.strip()]
        zh_lines = [ln for ln in lines if _has_cjk(ln)]
        en_lines = [ln for ln in lines if not _has_cjk(ln)]
        if zh_lines:
            zh_items.append(SubtitleItem(it.start, it.end, "\n".join(zh_lines)))
        if en_lines:
            en_items.append(SubtitleItem(it.start, it.end, "\n".join(en_lines)))
    return zh_items, en_items


def merge_dual(zh_items, en_items, gap=0.1, order="en_first", max_lines=2):
    """合并中英双语。返回 (merged_items, stats)。

    order: "en_first" 英文在上 / "zh_first" 中文在上
    max_lines: 每条字幕最多保留的行数 (2 = 英文一行 + 中文一行), 避免挡画面
    """
    zh_items = sorted(zh_items, key=lambda x: x.start)
    en_items = sorted(en_items, key=lambda x: x.start)
    merged = []
    used_zh = set()
    # 统计
    paired = 0
    en_only = 0
    zh_only = 0

    def _dual(en_text, zh_text):
        en_c = compact_text(en_text, max_lines=max_lines) if en_text else ""
        zh_c = compact_text(zh_text, max_lines=max_lines) if zh_text else ""
        if order == "zh_first":
            parts = [p for p in (zh_c, en_c) if p]
        else:
            parts = [p for p in (en_c, zh_c) if p]
        return "\n".join(parts)

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
            merged.append(SubtitleItem(en.start, en.end, compact_text(en.text, max_lines=max_lines)))
            en_only += 1

    for i, zh in enumerate(zh_items):
        if i not in used_zh:
            merged.append(SubtitleItem(zh.start, zh.end, compact_text(zh.text, max_lines=max_lines)))
            zh_only += 1

    merged.sort(key=lambda x: x.start)
    stats = {"paired": paired, "en_only": en_only, "zh_only": zh_only, "total": len(merged)}
    return merged, stats


def dedup_overlap(items, gap=0.001):
    """消除相邻字幕的时间轴重叠。

    合并双轨时 end=max(en.end, zh.end) 可能把结束时间往后推, 导致与下一条
    字幕重叠, 播放器把新字幕往上堆叠挡住画面。这里把每条字幕的 end 截短到
    不超过下一条的 start - gap, 保证时间轴严格递进不重叠。

    极短字幕(<0.2s)会被丢弃, 完全相同的连续条目会被合并。
    """
    if not items:
        return items
    items = sorted(items, key=lambda x: x.start)
    result = []
    for it in items:
        if it.end - it.start < 0.2:
            continue  # 丢弃过短条目
        if result and it.start - result[-1].end < gap and it.text == result[-1].text:
            # 文本相同且时间相邻 -> 合并延长
            result[-1] = SubtitleItem(result[-1].start, it.end, result[-1].text)
            continue
        result.append(it)
    # 截短重叠: 每条的 end 不超过下一条 start - gap
    for i in range(len(result) - 1):
        if result[i].end > result[i + 1].start - gap:
            new_end = result[i + 1].start - gap
            if new_end > result[i].start:  # 保证 end>start, 否则丢弃
                result[i] = SubtitleItem(result[i].start, new_end, result[i].text)
            else:
                result[i] = None
    result = [x for x in result if x is not None]
    return result


def render_srt(items):
    lines = []
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}\n{_fmt(item.start)} --> {_fmt(item.end)}\n{item.text}\n")
    return "\n".join(lines)


def render_ass(items, video_width=1920, video_height=1080):
    """渲染带中英不同颜色和黑色描边的 ASS 字幕。
    字号按视频高度等比缩放: 1080p→64, 2K→68, 4K→128。
    """
    w = int(video_width or 1920)
    h = int(video_height or 1080)
    play_res_y = h if h in (720, 1080, 1440, 2160) else 1080
    play_res_x = w if w in (1280, 1920, 2560, 3840) else 1920
    # 字号 = 高度 * 0.059, 1080→64, 1440→85, 2160→128
    font_size = max(36, int(play_res_y * 0.059))
    outline = max(2, round(font_size * 0.0625))
    margin_v = max(30, int(play_res_y * 0.042))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: English,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1
Style: Chinese,Microsoft YaHei,{font_size},&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for item in items:
        start = _ass_time(item.start)
        end = _ass_time(item.end)
        for text in (item.text or "").splitlines() or [""]:
            clean = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            style = "Chinese" if _has_cjk(text) else "English"
            lines.append(f"Dialogue: 0,{start},{end},{style},,0,0,0,,{clean}")
    return "\n".join(lines) + "\n"


def _ass_time(sec):
    """ASS 时间格式: H:MM:SS.cc"""
    sec = max(0, float(sec))
    total_cs = int(round(sec * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
