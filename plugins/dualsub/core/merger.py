#!/usr/bin/env python3
"""字幕对齐与合并引擎：实现精确时间轴重叠配对、长句智能拆分与重叠消歧。"""

import re
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict

from .models import (
    SubtitleItem,
    _DIALOG_RE,
    has_cjk,
    strip_ass_tags,
)

_ZH_SPLIT_RE = re.compile(r"([。！？!?；;…])")
_ZH_SPACE_RE = re.compile(r"([ 　]+)")


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """计算两条字幕的时间重叠量（秒），重叠为正数，间隔为负数。"""
    return min(a_end, b_end) - max(a_start, b_start)


def clean_dialogue(text: str) -> str:
    """清理人物对白的前导破折号，用于多句组合拼接。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = []
    for ln in lines:
        if ln.startswith("- ") or ln.startswith("-\t"):
            cleaned.append(ln[2:].strip())
        elif ln.startswith("-"):
            cleaned.append(ln[1:].strip())
        else:
            cleaned.append(ln)
    return " ".join(cleaned)


def split_dialogue_lines(text: str) -> List[str]:
    """拆分包含多个人物对白的行（如 '- 对白1\n- 对白2' 或 '对白1 - 对白2'）。"""
    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    result = []
    for line in raw_lines:
        if " - " in line and not line.startswith("-"):
            parts = line.split(" - ")
            result.append(parts[0].strip())
            for p in parts[1:]:
                result.append("- " + p.strip())
        else:
            result.append(line)
    return result


def compact_bilingual_text(
    first_text: str, second_text: str, max_lines: int = 2
) -> str:
    """整合成双语排版，每种语言各占一行，并规整对话首行前缀。"""
    def clean_lines(text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return ""
        if len(lines) == 1:
            return lines[0]
        first = lines[0]
        rest = [_DIALOG_RE.sub("", ln).strip() for ln in lines[1:]]
        first_clean = _DIALOG_RE.sub("", first).strip()
        if _DIALOG_RE.search(first):
            return f"- {first_clean} " + " ".join(rest)
        return first_clean + " " + " ".join(rest)

    p1 = clean_lines(first_text)
    p2 = clean_lines(second_text)
    parts = [p for p in (p1, p2) if p]
    if max_lines > 0 and len(parts) > max_lines:
        parts = parts[:max_lines]
    return "\n".join(parts)


def merge_subtitles(
    primary_items: List[SubtitleItem],
    secondary_items: List[SubtitleItem],
    order: str = "zh_first",  # zh_first: 主在上次在下; en_first: 次在上主在下
    max_lines: int = 2,
    tolerance: float = 0.4,
) -> Tuple[List[SubtitleItem], Dict[str, int]]:
    """将两轨字幕智能合并为统一时间轴的双语字幕条目。

    采用二部图区间重叠聚类算法，支持 1-to-1、1-to-N（如一句双人对话对应两条独立英文）、
    N-to-1 与多句聚合，彻底避免句子被截断成 0.04 秒闪烁或错位提前显示。
    """
    if not primary_items and not secondary_items:
        return [], {"paired": 0, "primary_only": 0, "secondary_only": 0, "total": 0}

    # 如果只有单边
    if not primary_items:
        return [
            SubtitleItem(it.start, it.end, it.text) for it in secondary_items
        ], {"paired": 0, "primary_only": 0, "secondary_only": len(secondary_items), "total": len(secondary_items)}
    if not secondary_items:
        return [
            SubtitleItem(it.start, it.end, it.text) for it in primary_items
        ], {"paired": 0, "primary_only": len(primary_items), "secondary_only": 0, "total": len(primary_items)}

    # 1. 严格 1-to-1 相同时间轴快速通道（如 AI 翻译完全对应）
    if len(primary_items) == len(secondary_items):
        is_exact_match = True
        for p, s in zip(primary_items[:10], secondary_items[:10]):
            if abs(p.start - s.start) > 0.05 or abs(p.end - s.end) > 0.05:
                is_exact_match = False
                break
        if is_exact_match:
            merged = []
            for p, s in zip(primary_items, secondary_items):
                top = p.text if order == "zh_first" else s.text
                bottom = s.text if order == "zh_first" else p.text
                text = compact_bilingual_text(top, bottom, max_lines=max_lines)
                merged.append(SubtitleItem(p.start, p.end, text))
            return merged, {
                "paired": len(merged),
                "primary_only": 0,
                "secondary_only": 0,
                "total": len(merged),
            }

    # 2. 建立有效重叠图
    p_adj: Dict[int, List[int]] = defaultdict(list)
    s_adj: Dict[int, List[int]] = defaultdict(list)

    for pi, p in enumerate(primary_items):
        p_dur = max(0.01, p.end - p.start)
        for si, s in enumerate(secondary_items):
            if s.end < p.start - tolerance:
                continue
            if s.start > p.end + tolerance:
                break

            s_dur = max(0.01, s.end - s.start)
            ov = _overlap(p.start, p.end, s.start, s.end)
            min_dur = min(p_dur, s_dur)

            is_valid_match = False
            if ov >= 0.25:
                is_valid_match = True
            elif min_dur > 0 and (ov / min_dur) >= 0.35:
                is_valid_match = True
            elif ov > 0 and (abs(p.start - s.start) <= tolerance or abs(p.end - s.end) <= tolerance):
                is_valid_match = True

            if is_valid_match:
                p_adj[pi].append(si)
                s_adj[si].append(pi)

    # 3. 寻找连通分量 (Connected Components)
    visited_p: Set[int] = set()
    visited_s: Set[int] = set()
    components: List[Tuple[List[int], List[int]]] = []

    for pi in range(len(primary_items)):
        if pi in visited_p:
            continue
        if not p_adj[pi]:
            components.append(([pi], []))
            visited_p.add(pi)
            continue

        comp_p = set([pi])
        comp_s = set()
        queue = [('p', pi)]
        visited_p.add(pi)

        while queue:
            kind, idx = queue.pop(0)
            if kind == 'p':
                for s_neighbor in p_adj[idx]:
                    if s_neighbor not in comp_s:
                        comp_s.add(s_neighbor)
                        visited_s.add(s_neighbor)
                        queue.append(('s', s_neighbor))
            else:
                for p_neighbor in s_adj[idx]:
                    if p_neighbor not in comp_p:
                        comp_p.add(p_neighbor)
                        visited_p.add(p_neighbor)
                        queue.append(('p', p_neighbor))

        components.append((sorted(list(comp_p)), sorted(list(comp_s))))

    # 补充未访问的 secondary 条目
    for si in range(len(secondary_items)):
        if si not in visited_s:
            components.append(([], [si]))
            visited_s.add(si)

    # 4. 对每个组件进行智能融合
    merged: List[SubtitleItem] = []
    paired_cnt = 0
    p_only_cnt = 0
    s_only_cnt = 0

    for p_indices, s_indices in components:
        # 单边无匹配
        if not p_indices and s_indices:
            for si in s_indices:
                s = secondary_items[si]
                top = "" if order == "zh_first" else s.text
                bottom = s.text if order == "zh_first" else ""
                merged.append(SubtitleItem(s.start, s.end, compact_bilingual_text(top, bottom, max_lines)))
                s_only_cnt += 1
            continue

        if p_indices and not s_indices:
            for pi in p_indices:
                p = primary_items[pi]
                top = p.text if order == "zh_first" else ""
                bottom = "" if order == "zh_first" else p.text
                merged.append(SubtitleItem(p.start, p.end, compact_bilingual_text(top, bottom, max_lines)))
                p_only_cnt += 1
            continue

        c_p = [primary_items[i] for i in p_indices]
        c_s = [secondary_items[i] for i in s_indices]

        # 1-to-1 匹配
        if len(c_p) == 1 and len(c_s) == 1:
            p = c_p[0]
            s = c_s[0]
            start = min(p.start, s.start)
            end = max(p.end, s.end)
            top = p.text if order == "zh_first" else s.text
            bottom = s.text if order == "zh_first" else p.text
            merged.append(SubtitleItem(start, end, compact_bilingual_text(top, bottom, max_lines)))
            paired_cnt += 1
            continue

        # 1-to-N：一条主语言对应多条次语言（如中文合并了双人对话）
        if len(c_p) == 1 and len(c_s) > 1:
            p = c_p[0]
            p_lines = split_dialogue_lines(p.text)
            if len(p_lines) == len(c_s):
                # 两人对话按行精准对齐每句英文
                for i, s in enumerate(c_s):
                    line_start = s.start if i > 0 else min(p.start, s.start)
                    line_end = s.end if i < len(c_s) - 1 else max(p.end, s.end)
                    top = p_lines[i] if order == "zh_first" else s.text
                    bottom = s.text if order == "zh_first" else p_lines[i]
                    merged.append(SubtitleItem(line_start, line_end, compact_bilingual_text(top, bottom, max_lines)))
                paired_cnt += len(c_s)
            else:
                # 无法按行对齐，合并次语言为统一长句
                s_combined = " ".join(clean_dialogue(s.text) for s in c_s)
                start = min(p.start, c_s[0].start)
                end = max(p.end, c_s[-1].end)
                top = p.text if order == "zh_first" else s_combined
                bottom = s_combined if order == "zh_first" else p.text
                merged.append(SubtitleItem(start, end, compact_bilingual_text(top, bottom, max_lines)))
                paired_cnt += 1
            continue

        # N-to-1：多条主语言对应一条次语言
        if len(c_p) > 1 and len(c_s) == 1:
            s = c_s[0]
            s_lines = split_dialogue_lines(s.text)
            if len(s_lines) == len(c_p):
                for i, p in enumerate(c_p):
                    line_start = p.start if i > 0 else min(p.start, s.start)
                    line_end = p.end if i < len(c_p) - 1 else max(p.end, s.end)
                    top = p.text if order == "zh_first" else s_lines[i]
                    bottom = s_lines[i] if order == "zh_first" else p.text
                    merged.append(SubtitleItem(line_start, line_end, compact_bilingual_text(top, bottom, max_lines)))
                paired_cnt += len(c_p)
            else:
                p_combined = " ".join(clean_dialogue(p.text) for p in c_p)
                start = min(c_p[0].start, s.start)
                end = max(c_p[-1].end, s.end)
                top = p_combined if order == "zh_first" else s.text
                bottom = s.text if order == "zh_first" else p_combined
                merged.append(SubtitleItem(start, end, compact_bilingual_text(top, bottom, max_lines)))
                paired_cnt += 1
            continue

        # N-to-N：多条对多条
        if len(c_p) == len(c_s):
            for p, s in zip(c_p, c_s):
                start = min(p.start, s.start)
                end = max(p.end, s.end)
                top = p.text if order == "zh_first" else s.text
                bottom = s.text if order == "zh_first" else p.text
                merged.append(SubtitleItem(start, end, compact_bilingual_text(top, bottom, max_lines)))
                paired_cnt += 1
        else:
            p_combined = " ".join(clean_dialogue(p.text) for p in c_p)
            s_combined = " ".join(clean_dialogue(s.text) for s in c_s)
            start = min(c_p[0].start, c_s[0].start)
            end = max(c_p[-1].end, c_s[-1].end)
            top = p_combined if order == "zh_first" else s_combined
            bottom = s_combined if order == "zh_first" else p_combined
            merged.append(SubtitleItem(start, end, compact_bilingual_text(top, bottom, max_lines)))
            paired_cnt += 1

    merged.sort(key=lambda x: x.start)
    stats = {
        "paired": paired_cnt,
        "primary_only": p_only_cnt,
        "secondary_only": s_only_cnt,
        "total": len(merged),
    }
    return merged, stats


def dedup_overlap(items: List[SubtitleItem], gap: float = 0.001) -> List[SubtitleItem]:
    """消除相邻字幕的时间轴重叠，防止播放器因结束时间推后而把字幕往上堆叠挡画面。"""
    if not items:
        return items

    sorted_items = sorted(items, key=lambda x: x.start)
    result: List[SubtitleItem] = []

    for it in sorted_items:
        if it.end - it.start < 0.15:
            continue
        if result and it.start - result[-1].end < gap and it.text == result[-1].text:
            # 文本完全相同且紧邻 -> 合并延长
            result[-1] = SubtitleItem(result[-1].start, it.end, result[-1].text)
            continue
        result.append(it)

    for i in range(len(result) - 1):
        if result[i].end > result[i + 1].start - gap:
            new_end = result[i + 1].start - gap
            if new_end > result[i].start + 0.1:
                result[i] = SubtitleItem(result[i].start, new_end, result[i].text)

    return result
