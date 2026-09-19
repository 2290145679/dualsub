#!/usr/bin/env python3
"""字幕处理主流程引擎：协调探测、提取、AI补译、样式排版与交付。"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    VIDEO_EXTS,
    SubtitleItem,
    TrackInfo,
    TaskStatus,
    is_chinese_lang,
    is_english_lang,
    is_japanese_lang,
    is_korean_lang,
)
from .extractor import (
    probe_embedded_subtitles,
    find_external_subtitles,
    extract_track_items,
    is_track_mixed,
    split_mixed_track,
    check_native_bilingual,
    is_bitmap_codec,
)
from .translator import AITranslator
from .merger import merge_subtitles, dedup_overlap
from .styler import render_srt, render_ass
from .muxer import mux_into_mkv


def probe_video_resolution(video_path: str | Path) -> Tuple[int, int]:
    """探测视频分辨率 (width, height)。"""
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(",")
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1920, 1080


def has_existing_dual_subtitle(video_path: str | Path, suffix: str) -> bool:
    """检查是否存在已生成的双语字幕文件。"""
    v = Path(video_path)
    candidates = {
        suffix,
        ".zh-cn&en.default.ass",
        ".zh-CN.ass",
        ".zh-CN.srt",
        ".dual.ass",
        ".dual.srt",
    }
    return any(v.with_name(v.stem + s).exists() for s in candidates)


def process_video_pipeline(
    video_path: str | Path,
    config: Dict[str, Any],
    ai_translator: Optional[AITranslator],
    cache: Dict[str, str],
) -> Tuple[str, str, List[str], Dict[str, Any]]:
    """核心处理流水线。

    返回: (TaskStatus.value, summary_message, logs, detail_dict)
    """
    logs: List[str] = []
    video = Path(video_path)

    if not video.exists():
        return TaskStatus.FAILED.value, "视频文件不存在", logs, {}
    if video.suffix.lower() not in VIDEO_EXTS:
        return TaskStatus.IGNORED.value, "非视频文件", logs, {}

    min_size_mb = int(config.get("min_file_size", 0) or 0)
    if min_size_mb > 0:
        try:
            size_mb = video.stat().st_size / (1024 * 1024)
            if size_mb < min_size_mb:
                return (
                    TaskStatus.IGNORED.value,
                    f"文件大小 ({size_mb:.1f}MB) 小于设定阈值 {min_size_mb}MB",
                    logs,
                    {},
                )
        except Exception:
            pass

    sub_suffix = config.get("subtitle_suffix", ".zh-cn&en.default.ass") or ".zh-cn&en.default.ass"
    skip_if_exists = config.get("skip_if_exists", True)
    mode = config.get("mode", "srt")  # srt (外挂) | mux (封回) | both (外挂+封回)
    order = config.get("order", "zh_first")  # zh_first | en_first

    if skip_if_exists and has_existing_dual_subtitle(video, sub_suffix):
        return TaskStatus.IGNORED.value, "已存在双语字幕，跳过处理", logs, {}

    logs.append(f"开始处理: {video.name}")

    # 1. 探测字幕源（外挂优先 + 内嵌）
    ext_tracks = find_external_subtitles(video)
    emb_tracks = probe_embedded_subtitles(video)
    all_tracks = ext_tracks + emb_tracks

    if not all_tracks:
        return TaskStatus.IGNORED.value, "未发现任何外挂或内嵌字幕轨", logs, {}

    logs.append(f"发现字幕源: 外挂 {len(ext_tracks)} 个, 内嵌 {len(emb_tracks)} 轨")

    # 分类字幕轨并智能打分排序（优先文本轨、优先对白轨、排除 SDH/CC、排斥图形位图轨）
    def score_zh(t: TrackInfo) -> int:
        score = 0
        t_low = (t.title or "").lower()
        if is_bitmap_codec(t.codec):
            score -= 500  # 图形位图字幕（PGS/VOBSUB）无法转文本
        elif (t.codec or "").lower() in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt"):
            score += 100  # 优先文本格式
        if "sdh" in t_low or "cc" in t_low:
            score -= 50
        if any(k in t_low for k in ("简", "chs", "hans", "simplified", "sg", "sc")):
            score += 30
        if t.is_external:
            score += 50  # 优先外挂有效文本字幕
        else:
            score += 10
        return score

    def score_en(t: TrackInfo) -> int:
        score = 0
        t_low = (t.title or "").lower()
        if is_bitmap_codec(t.codec):
            score -= 500  # 图形位图字幕无法转文本
        elif (t.codec or "").lower() in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt"):
            score += 100  # 优先文本格式
        if "sdh" in t_low or "cc" in t_low:
            score -= 50
        if t.is_external:
            score += 50  # 优先外挂有效文本字幕
        else:
            score += 10
        return score

    zh_tracks = sorted(
        [t for t in all_tracks if is_chinese_lang(t.lang) or "中" in (t.title or "")],
        key=score_zh,
        reverse=True,
    )
    en_tracks = sorted(
        [t for t in all_tracks if is_english_lang(t.lang) or "英" in (t.title or "")],
        key=score_en,
        reverse=True,
    )
    ja_tracks = [t for t in all_tracks if is_japanese_lang(t.lang) or "日" in (t.title or "")]
    other_tracks = [t for t in all_tracks if t not in zh_tracks and t not in en_tracks and t not in ja_tracks]

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 检查片源是否本来就自带双语字幕
        is_force = bool(config.get("force", False) or config.get("source") == "manual_regen")
        if not is_force and skip_if_exists:
            has_native, native_desc = check_native_bilingual(video, tmp_path)
            if has_native:
                msg = f"片源自带双语字幕 ({native_desc})，未重新生成"
                logs.append(msg)
                return TaskStatus.IGNORED.value, msg, logs, {"ai_status": "片源自带双语，未调用 AI"}

        zh_items: List[SubtitleItem] = []
        en_items: List[SubtitleItem] = []
        zh_desc = ""
        en_desc = ""
        ai_used = False
        ai_status = ""

        # 检查是否为单轨中英混合
        is_mixed_candidate = False
        if zh_tracks and not en_tracks:
            # 试读第一条中文轨
            ok, sample_items, msg = extract_track_items(video, zh_tracks[0], tmp_path)
            if ok and is_track_mixed(zh_tracks[0], sample_items):
                is_mixed_candidate = True
                split_zh, split_en = split_mixed_track(sample_items)
                if split_zh and split_en:
                    zh_items, en_items = split_zh, split_en
                    zh_desc = f"单轨拆分中文 ({len(split_zh)} 句)"
                    en_desc = f"单轨拆分英文 ({len(split_en)} 句)"
                    ai_status = "单轨自带双语，无需调用 AI"
                    logs.append(f"单轨双语拆分: 中文 {len(split_zh)} 句 / 英文 {len(split_en)} 句")

        if not is_mixed_candidate:
            # 优先提取已有的中文和英文（按打分顺序逐轨尝试，避免某轨为PGS等不支持格式时直接中断）
            if zh_tracks:
                for t in zh_tracks:
                    ok, items, msg = extract_track_items(video, t, tmp_path)
                    if ok and items:
                        zh_items = items
                        t_zh = t
                        zh_desc = f"{'外挂' if t_zh.is_external else '内封#' + str(t_zh.index)} {t_zh.title or '中文字幕'} ({len(zh_items)} 句)"
                        logs.append(f"中文源: {msg}")
                        break
                    else:
                        logs.append(f"中文源候选轨 #{t.index if not t.is_external else '外挂'} 无法使用: {msg}")

            if en_tracks:
                for t in en_tracks:
                    ok, items, msg = extract_track_items(video, t, tmp_path)
                    if ok and items:
                        en_items = items
                        t_en = t
                        en_desc = f"{'外挂' if t_en.is_external else '内封#' + str(t_en.index)} {t_en.title or '英文字幕'} ({len(en_items)} 句)"
                        logs.append(f"英文源: {msg}")
                        break
                    else:
                        logs.append(f"英文源候选轨 #{t.index if not t.is_external else '外挂'} 无法使用: {msg}")

        # 判定是否需要 AI 补全翻译
        ai_enabled = config.get("ai_enabled", False)

        if not zh_items and not en_items:
            # 检查是否有日文或其他语言轨
            target_source_tracks = ja_tracks or other_tracks
            if target_source_tracks and ai_enabled and ai_translator:
                for t in target_source_tracks:
                    ok, items, msg = extract_track_items(video, t, tmp_path)
                    if ok and items:
                        lang_hint = "日文" if t in ja_tracks else "外文"
                        logs.append(f"检测到纯{lang_hint}字幕 (#{t.index if not t.is_external else '外挂'})，触发 AI 补全中文...")
                        trans_zh = ai_translator.translate_subtitle_items(
                            items, cache, logs, source_lang_hint=lang_hint
                        )
                        if trans_zh:
                            zh_items = trans_zh
                            en_items = items  # 次语言保留原语言
                            ai_used = True
                            zh_desc = f"AI {lang_hint}转中文 ({len(zh_items)} 句)"
                            t_orig = t
                            en_desc = f"{'外挂' if t_orig.is_external else '内封#' + str(t_orig.index)} {t_orig.title or lang_hint} ({len(en_items)} 句)"
                            ai_status = f"✅ 检测到纯{lang_hint}字幕，已调用 AI 翻译生成中文 ({len(zh_items)} 句)"
                            break
                        else:
                            logs.append(f"AI 补全 {lang_hint} 字幕失败")
                    else:
                        logs.append(f"外文候选轨 #{t.index if not t.is_external else '外挂'} 无法使用: {msg}")

            if not zh_items and not en_items:
                all_bitmap = bool(emb_tracks and all(is_bitmap_codec(t.codec) for t in emb_tracks) and not ext_tracks)
                if all_bitmap:
                    fail_msg = "片源内嵌字幕均为 PGS/图形位图字幕，不支持直接提取文本。请为该影片下载/刮削外挂 SRT/ASS 格式字幕后再试"
                else:
                    fail_msg = "未能成功提取有效字幕文本 (片源字幕轨无法解析或为空)"
                logs.append(f"❌ {fail_msg}")
                return TaskStatus.IGNORED.value, fail_msg, logs, {"ai_status": "无法提取文本字幕"}

        elif en_items and not zh_items:
            # 只有英文，缺少中文
            if ai_enabled and ai_translator:
                logs.append("片源缺少中文字幕，触发 AI 补全中文...")
                trans_zh = ai_translator.translate_subtitle_items(
                    en_items, cache, logs, source_lang_hint="英文"
                )
                if trans_zh:
                    zh_items = trans_zh
                    ai_used = True
                    zh_desc = f"AI 补全中文 ({len(zh_items)} 句)"
                    ai_status = f"✅ 片源缺少中文字幕，已调用 AI 补全翻译 ({len(zh_items)} 句)"
                else:
                    return TaskStatus.FAILED.value, "AI 补全中文字幕失败", logs, {"ai_status": "❌ AI 补全翻译失败"}
            else:
                return TaskStatus.IGNORED.value, "仅含英文字幕，AI 补全未开启，跳过", logs, {"ai_status": "仅含英文 (AI未开启)"}

        elif zh_items and not en_items:
            # 只有中文，缺少英文
            if ai_enabled and ai_translator and config.get("ai_translate_zh_to_en", False):
                logs.append("片源缺少英文字幕，触发 AI 补全英文...")
                orig_target = ai_translator.target_lang
                ai_translator.target_lang = "en"
                trans_en = ai_translator.translate_subtitle_items(
                    zh_items, cache, logs, source_lang_hint="中文"
                )
                ai_translator.target_lang = orig_target
                if trans_en:
                    en_items = trans_en
                    ai_used = True
                    en_desc = f"AI 补全英文 ({len(en_items)} 句)"
                    ai_status = f"✅ 片源缺少英文字幕，已调用 AI 补全翻译 ({len(en_items)} 句)"
                else:
                    ai_status = "片源缺少英文 (AI翻译失败)"
            else:
                if not config.get("allow_single_zh", True):
                    return TaskStatus.IGNORED.value, "仅含中文字幕，无需双语合并，跳过", logs, {"ai_status": "仅含中文 (未开启中译英)"}
                ai_status = "仅含中文字幕 (未开启中译英)"

        else:
            # 中英文均已齐备
            if not ai_status:
                if ai_enabled:
                    ai_status = "⚡ 片源已自带中英双轨，无需补全 (未消耗 AI 额度)"
                else:
                    ai_status = "⏸️ 片源自带中英双轨 (AI未开启)"

        # 3.5 时间轴微调偏移
        try:
            time_offset = float(config.get("time_offset", 0.0) or 0.0)
        except (TypeError, ValueError):
            time_offset = 0.0

        if time_offset != 0.0:
            logs.append(f"应用时间轴偏移: {time_offset:+.2f} 秒 (正数延后，负数提前)")
            for it in zh_items:
                it.start = max(0.0, round(it.start + time_offset, 3))
                it.end = max(0.0, round(it.end + time_offset, 3))
            for it in en_items:
                it.start = max(0.0, round(it.start + time_offset, 3))
                it.end = max(0.0, round(it.end + time_offset, 3))

        # 4. 执行双语合并与对齐
        logs.append(f"正在进行双语对齐 (排版顺序: {'中文在上' if order == 'zh_first' else '英文在上'})...")
        merged, stats = merge_subtitles(zh_items, en_items, order=order, max_lines=2)
        merged = dedup_overlap(merged)

        if not merged:
            return TaskStatus.FAILED.value, "字幕合并结果为空", logs, {}

        logs.append(
            f"对齐完成: 共 {stats['total']} 句 (双语配对 {stats['paired']} 句, 仅中 {stats['primary_only']} 句, 仅外 {stats['secondary_only']} 句)"
        )

        # 5. 渲染生成字幕文件
        out_sub_path = video.with_name(video.stem + sub_suffix)
        is_ass = out_sub_path.suffix.lower() in (".ass", ".ssa")

        if is_ass:
            vw, vh = probe_video_resolution(video)
            logs.append(f"视频分辨率: {vw}x{vh}，自动适配 ASS 字号与边距")
            ass_content = render_ass(
                merged,
                video_width=vw,
                video_height=vh,
                font_chinese=config.get("font_chinese", "Microsoft YaHei"),
                font_english=config.get("font_english", "Arial"),
                chinese_color=config.get("chinese_color", "&H00FFFFFF"),
                english_color=config.get("english_color", "&H0000E5FF"),
                margin_v=int(config.get("margin_v", 45) or 45),
            )
            out_sub_path.write_text(ass_content, encoding="utf-8-sig")
        else:
            srt_content = render_srt(merged)
            out_sub_path.write_text(srt_content, encoding="utf-8")

        logs.append(f"成功生成外挂字幕: {out_sub_path.name}")

        # 6. 封回 MKV（可选）
        if mode in ("mux", "both"):
            backup = config.get("backup", True)
            overwrite = config.get("overwrite", False)
            mux_ok, mux_msg, mux_logs = mux_into_mkv(
                video, out_sub_path, backup=backup, overwrite=overwrite
            )
            logs.extend(mux_logs)
            if not mux_ok:
                logs.append(f"封回 MKV 异常: {mux_msg}")

        detail = {
            "ai_used": ai_used,
            "ai_status": ai_status,
            "zh_source": zh_desc or "无",
            "en_source": en_desc or "无",
            "total": stats.get("total", 0),
            "paired": stats.get("paired", 0),
            "primary_only": stats.get("primary_only", 0),
            "secondary_only": stats.get("secondary_only", 0),
            "sub_file": out_sub_path.name,
        }
        summary_msg = f"双语字幕生成成功 ({stats['total']} 句) | AI: {ai_status}"
        return TaskStatus.COMPLETED.value, summary_msg, logs, detail
