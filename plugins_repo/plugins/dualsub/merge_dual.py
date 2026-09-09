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


# 常用中文句末标点, 用于把"一条中文覆盖多条英文"的长句拆开
_ZH_SPLIT_RE = re.compile(r"([。！？!?；;…])")
# 中英文空格 (半角/全角), 也是可拆边界
_ZH_SPACE_RE = re.compile(r"([ 　]+)")
# 英文句末标点 (用于判定可拆的英文边界)
_EN_SPLIT_RE = re.compile(r"([.!?…])\s+")


def _overlap(a_start, a_end, b_start, b_end):
    """两条字幕的时间重叠量, 可为负(表示间隔, 间隔越近负值越大)"""
    return min(a_end, b_end) - max(a_start, b_start)


def _split_zh_by_count(text, n, en_items):
    """把一条中文按 n 条英文的时长比例拆成 n 段。

    优先按标点边界拆; 找不到足够标点时按字符数比例均分。
    返回 list[str], 长度 == n。
    """
    if n <= 1:
        return [text]
    clean = _strip_tags(text).strip()
    # 先按空格切成候选片段 (这句中文里的多个句子常以空格分隔)
    space_parts = _ZH_SPACE_RE.split(clean)
    space_segments = [s.strip() for s in space_parts if s and s.strip()]
    # 再在每个空格片段内部按标点细分
    segments = []
    for seg in space_segments:
        parts = _ZH_SPLIT_RE.split(seg)
        buf = ""
        for p in parts:
            if not p:
                continue
            buf += p
            if _ZH_SPLIT_RE.fullmatch(p):
                segments.append(buf)
                buf = ""
        if buf:
            segments.append(buf)
    # 去掉纯标点/空段
    segments = [s for s in segments if s and _has_cjk(s)]
    if len(segments) >= n:
        # 边界足够: 前 n-1 段各占一段, 最后一段合并剩余
        head = segments[: n - 1]
        tail = "".join(segments[n - 1:])
        return head + [tail]
    # 边界不够: 按英文字幕时长比例切字符 (尽量避免切断, 靠兜底保证非空)
    total_dur = max(0.001, sum(max(0.0, e.end - e.start) for e in en_items))
    result = []
    pos = 0
    for i, e in enumerate(en_items):
        if i == n - 1:
            result.append(clean[pos:] or clean)
            break
        ratio = max(0.0, e.end - e.start) / total_dur
        cut = pos + max(1, int(len(clean) * ratio))
        cut = min(cut, len(clean) - (n - 1 - i))  # 保证后续至少各 1 字符
        result.append(clean[pos:cut])
        pos = cut
    # 兜底: 若产生空段则退化为整段
    if any(not s.strip() for s in result):
        return [clean]
    return result


def merge_dual(zh_items, en_items, gap=0.1, order="en_first", max_lines=2):
    """合并中英双语。返回 (merged_items, stats)。

    order: "en_first" 英文在上 / "zh_first" 中文在上
    max_lines: 每条字幕最多保留的行数 (2 = 英文一行 + 中文一行), 避免挡画面

    对齐策略:
    - 先做全局单调最优对齐(按时间重叠总量最大化), 避免贪心抢配对错位。
    - 若一条中文覆盖多条英文(时间范围明显更长), 按标点/时长比例把它拆开,
      分别配给各条英文 (解决"前一句中文提前出来、后一句只剩英文"的问题)。
    - 一条英文覆盖多条中文时, 将多条中文做轻量合并。
    """
    zh_items = sorted(zh_items, key=lambda x: x.start)
    en_items = sorted(en_items, key=lambda x: x.start)
    merged = []
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

    if not zh_items:
        for en in en_items:
            merged.append(SubtitleItem(en.start, en.end, compact_text(en.text, max_lines=max_lines)))
            en_only += 1
        merged.sort(key=lambda x: x.start)
        stats = {"paired": 0, "en_only": en_only, "zh_only": 0, "total": len(merged)}
        return merged, stats
    if not en_items:
        for zh in zh_items:
            merged.append(SubtitleItem(zh.start, zh.end, compact_text(zh.text, max_lines=max_lines)))
            zh_only += 1
        merged.sort(key=lambda x: x.start)
        stats = {"paired": 0, "en_only": 0, "zh_only": zh_only, "total": len(merged)}
        return merged, stats

    # ---- 第一步: 全局单调对齐 (1 对 1) ----
    # dp[i][j] = 前 i 条英文与前 j 条中文的最大重叠总分
    n, m = len(en_items), len(zh_items)
    NEG = -1e18
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if i > 0:
                dp[i][j] = max(dp[i][j], dp[i - 1][j])  # 跳过这条英文(en_only)
            if j > 0:
                dp[i][j] = max(dp[i][j], dp[i][j - 1])  # 跳过这条中文(zh_only)
            if i > 0 and j > 0:
                score = dp[i - 1][j - 1] + _overlap(
                    en_items[i - 1].start, en_items[i - 1].end,
                    zh_items[j - 1].start, zh_items[j - 1].end,
                )
                if score > dp[i][j]:
                    dp[i][j] = score

    # 回溯得到配对列表 (en_idx, zh_idx)
    pairs = []
    used_en = set()
    used_zh = set()
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            score_pair = dp[i - 1][j - 1] + _overlap(
                en_items[i - 1].start, en_items[i - 1].end,
                zh_items[j - 1].start, zh_items[j - 1].end,
            )
            if abs(dp[i][j] - score_pair) < 1e-9:
                pairs.append((i - 1, j - 1))
                used_en.add(i - 1)
                used_zh.add(j - 1)
                i -= 1
                j -= 1
                continue
        if i > 0 and abs(dp[i][j] - dp[i - 1][j]) < 1e-9:
            i -= 1
            continue
        if j > 0 and abs(dp[i][j] - dp[i][j - 1]) < 1e-9:
            j -= 1
            continue
        if i > 0:
            i -= 1
        if j > 0:
            j -= 1
    pairs.reverse()

    # 过滤掉"几乎无重叠且间隔明显"的伪配对
    for ei, zi in pairs:
        ov = _overlap(en_items[ei].start, en_items[ei].end,
                      zh_items[zi].start, zh_items[zi].end)
        if ov < -gap:
            used_en.discard(ei)
            used_zh.discard(zi)

    valid_pairs = [(ei, zi) for (ei, zi) in pairs
                   if ei in used_en and zi in used_zh]

    # ---- 第二步: 把"一条中文覆盖多条英文"的多对一关系展开 ----
    # 先按中文分组, 统计每条中文被多少条英文引用/重叠
    en2zh = {}
    for ei, zi in valid_pairs:
        en2zh[ei] = zi

    # 找出每条中文"严格包含"的英文 (英文整段时间都落在中文时间段内)。
    # 这类英文是被这条中文"吞掉"的多个句子, 需要把中文拆开分别配对。
    zh_to_ens = {}
    for zi, zh in enumerate(zh_items):
        ens = [ei for ei, en in enumerate(en_items)
               if en.start >= zh.start - gap and en.end <= zh.end + gap]
        if ens:
            zh_to_ens[zi] = ens

    # 触发拆分: 一条中文严格包含 >=2 条英文
    zh_split = {}   # zi -> list of (en_idx, zh_fragment)
    for zi, eis in zh_to_ens.items():
        if len(eis) < 2:
            continue
        zh = zh_items[zi]
        ens = [en_items[ei] for ei in sorted(eis, key=lambda x: en_items[x].start)]
        frags = _split_zh_by_count(zh.text, len(ens), ens)
        zh_split[zi] = list(zip(sorted(eis), frags))

    # ---- 第三步: 组装输出 ----
    # 被拆分的英文集合: 每条用自己的时间轴, 只配对应中文片段
    en_covered_by_split = set()
    for zi, frags in zh_split.items():
        for ei, frag in frags:
            en_covered_by_split.add(ei)

    for ei, en in enumerate(en_items):
        zh_text = None
        use_en_timing = False
        if ei in en_covered_by_split:
            # 拆分段: 英文用自己的时间轴, 配对应中文片段
            for zi, frags in zh_split.items():
                if ei in [x for x, _ in frags]:
                    frag = next((t for x, t in frags if x == ei), None)
                    if frag is not None:
                        zh_text = frag
                        use_en_timing = True
                    break
        elif ei in en2zh:
            zi = en2zh[ei]
            zh_text = zh_items[zi].text

        if zh_text is not None:
            if use_en_timing:
                start, end = en.start, en.end
            else:
                start = min(en.start, zh_items[en2zh[ei]].start)
                end = max(en.end, zh_items[en2zh[ei]].end)
            merged.append(SubtitleItem(start, end, _dual(en.text, zh_text)))
            paired += 1
        else:
            merged.append(SubtitleItem(en.start, en.end, compact_text(en.text, max_lines=max_lines)))
            en_only += 1

    # 剩余中文: 既未被拆分、也未作为 one-to-one 配对输出
    consumed_zh = set(en2zh.values())
    consumed_zh.update(zh_split.keys())
    for zi, zh in enumerate(zh_items):
        if zi in consumed_zh:
            continue
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
        text = (item.text or "").replace("\u200b[AI]\u200b", "[AI] ")
        lines.append(f"{idx}\n{_fmt(item.start)} --> {_fmt(item.end)}\n{text}\n")
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
Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1
Style: English,Arial,{font_size},&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1
Style: Chinese,Microsoft YaHei,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for item in items:
        start = _ass_time(item.start)
        end = _ass_time(item.end)
        # 同一时间段的中英文放在同一个 Dialogue, 用 \N 换行 + \r 样式切换
        # 顺序由 item.text 的行序决定: 第一行在上, 第二行在下
        parts = []
        for text in (item.text or "").splitlines() or [""]:
            # AI 翻译标记: \u200b[AI]\u200b -> ASS 内联青色标记
            ai_mark = ""
            if "\u200b[AI]\u200b" in text:
                text = text.replace("\u200b[AI]\u200b", "")
                ai_mark = "{\\c&H00FFFF&\\b1}[AI]{\\r}"
            clean = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            if _has_cjk(text):
                prefix = f"{{\\rChinese}}{ai_mark}"
            else:
                prefix = f"{{\\rEnglish}}{ai_mark}"
            parts.append(f"{prefix}{clean}")
        combined = "\\N".join(parts)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{combined}")
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
