#!/usr/bin/env python3
"""双语字幕样式排版引擎：支持彩色 ASS 高清排版与标准 SRT 渲染。"""

import re
from typing import List, Optional
from .models import SubtitleItem, _fmt_srt_time, _fmt_ass_time, has_cjk


def render_srt(items: List[SubtitleItem]) -> str:
    """渲染标准 SRT 字幕格式。"""
    lines = []
    for idx, item in enumerate(items, 1):
        clean_text = (item.text or "").replace("\u200b[AI]\u200b", "[AI] ")
        lines.append(f"{idx}\n{_fmt_srt_time(item.start)} --> {_fmt_srt_time(item.end)}\n{clean_text}\n")
    return "\n".join(lines)


def normalize_ass_color(color: str, default: str = "&H00FFFFFF") -> str:
    """将任意输入的颜色格式（标准网页HEX #RRGGBB 或 ASS码 &H00BBGGRR）转换为标准的 ASS &HAABBGGRR 格式。"""
    if not color:
        return default
    c = str(color).strip().upper()
    if c.startswith("&H"):
        val = c[2:].rstrip("&")
        if len(val) == 6:
            return f"&H00{val}"
        elif len(val) == 8:
            return f"&H{val}"
        return c
    clean = c.lstrip("#")
    if len(clean) == 6:
        r, g, b = clean[0:2], clean[2:4], clean[4:6]
        return f"&H00{b}{g}{r}"
    elif len(clean) == 8:
        a, r, g, b = clean[0:2], clean[2:4], clean[4:6], clean[6:8]
        return f"&H{a}{b}{g}{r}"
    return default


def render_ass(
    items: List[SubtitleItem],
    video_width: int = 1920,
    video_height: int = 1080,
    font_chinese: str = "Microsoft YaHei",
    font_english: str = "Arial",
    chinese_color: str = "&H00FFFFFF",  # 支持 #FFFFFF 或 &H00FFFFFF
    english_color: str = "&H0000E5FF",  # 支持 #00E5FF 或 &H0000E5FF
    outline_color: str = "&H00000000",  # 黑色描边
    shadow_color: str = "&H80000000",   # 半透明阴影
    margin_v: int = 45,
    font_scale: float = 1.0,
) -> str:
    """渲染带独立中外样式的 ASS 高清双语字幕。

    自动根据分辨率等比缩放字号：
    1080p -> 基准 ~64pt
    4K (2160p) -> ~128pt
    """
    w = int(video_width or 1920)
    h = int(video_height or 1080)
    play_res_y = h if h in (720, 1080, 1440, 2160) else 1080
    play_res_x = w if w in (1280, 1920, 2560, 3840) else 1920

    c_zh = normalize_ass_color(chinese_color, "&H00FFFFFF")
    c_en = normalize_ass_color(english_color, "&H0000E5FF")
    c_outline = normalize_ass_color(outline_color, "&H00000000")
    c_shadow = normalize_ass_color(shadow_color, "&H80000000")

    base_font_size = max(36, int(play_res_y * 0.058 * font_scale))
    en_font_size = max(30, int(base_font_size * 0.88))  # 外文字号稍小
    outline = max(2, round(base_font_size * 0.065))
    computed_margin_v = max(30, int(play_res_y * (margin_v / 1080.0)))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_chinese},{base_font_size},{c_zh},&H00FFFFFF,{c_outline},{c_shadow},0,0,0,0,100,100,0,0,1,{outline},1,2,50,50,{computed_margin_v},1
Style: Chinese,{font_chinese},{base_font_size},{c_zh},&H00FFFFFF,{c_outline},{c_shadow},0,0,0,0,100,100,0,0,1,{outline},1,2,50,50,{computed_margin_v},1
Style: English,{font_english},{en_font_size},{c_en},&H00FFFFFF,{c_outline},{c_shadow},0,0,0,0,100,100,0,0,1,{outline},1,2,50,50,{computed_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogue_lines = []
    for item in items:
        start_ts = _fmt_ass_time(item.start)
        end_ts = _fmt_ass_time(item.end)

        lines = (item.text or "").splitlines()
        parts = []
        for line in lines:
            if not line.strip():
                continue
            ai_mark = ""
            if "\u200b[AI]\u200b" in line:
                line = line.replace("\u200b[AI]\u200b", "")
                ai_mark = "{\\c&H00FFFF&\\b1}[AI]{\\r}"

            # 转义花括号与反斜杠
            safe_text = line.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            if has_cjk(line):
                prefix = f"{{\\rChinese}}{ai_mark}"
            else:
                prefix = f"{{\\rEnglish}}{ai_mark}"
            parts.append(f"{prefix}{safe_text}")

        combined = "\\N".join(parts)
        if combined:
            dialogue_lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{combined}")

    return header + "\n".join(dialogue_lines) + "\n"
