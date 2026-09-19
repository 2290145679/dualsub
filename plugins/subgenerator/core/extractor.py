#!/usr/bin/env python3
"""字幕探测与提取模块：支持同目录外挂字幕与视频内嵌字幕轨。"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import (
    SUB_EXTS,
    SubtitleItem,
    TrackInfo,
    _TS_RE,
    _LINE_RE,
    _to_seconds,
    has_cjk,
    has_japanese,
    is_chinese_lang,
    is_english_lang,
    is_japanese_lang,
    is_korean_lang,
    strip_ass_tags,
)

BITMAP_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle",
    "pgs",
    "dvd_subtitle",
    "dvdsub",
    "vobsub",
    "xsub",
}


def is_bitmap_codec(codec: str) -> bool:
    """判断字幕流编码是否为图形/位图字幕 (如 PGS、VOBSUB)，该类字幕无法直接用 ffmpeg 转文本。"""
    if not codec:
        return False
    return codec.strip().lower() in BITMAP_SUBTITLE_CODECS



def parse_srt(content: str | bytes) -> List[SubtitleItem]:
    """解析 SRT 字符串或字节为 SubtitleItem 列表。"""
    if isinstance(content, bytes):
        for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "big5", "utf-16"):
            try:
                content = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = content.decode("utf-8", errors="replace")

    if content.startswith("\ufeff"):
        content = content[1:]

    blocks = re.split(r"\n\s*\n", content.strip().replace("\r\n", "\n"))
    items = []
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].isdigit():
            lines = lines[1:]
        if not lines:
            continue

        m = _LINE_RE.search(lines[0])
        if not m:
            continue
        s_h, s_m, s_s, s_ms = (
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4).ljust(3, "0")[:3]),
        )
        e_h, e_m, e_s, e_ms = (
            int(m.group(5)),
            int(m.group(6)),
            int(m.group(7)),
            int(m.group(8).ljust(3, "0")[:3]),
        )
        start = _to_seconds(s_h, s_m, s_s, s_ms)
        end = _to_seconds(e_h, e_m, e_s, e_ms)

        text_lines = [strip_ass_tags(tl) for tl in lines[1:]]
        text = "\n".join([tl for tl in text_lines if tl.strip()]).strip()
        if text:
            items.append(SubtitleItem(start, end, text))
    return items


def parse_ass_dialogues(content: str | bytes) -> List[SubtitleItem]:
    """简易提取 ASS/SSA [Events] 对白文本与时间轴。"""
    if isinstance(content, bytes):
        for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "big5", "utf-16"):
            try:
                content = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            content = content.decode("utf-8", errors="replace")

    items = []
    lines = content.replace("\r\n", "\n").splitlines()
    in_events = False
    format_indices = {}

    for line in lines:
        line_clean = line.strip()
        if line_clean.lower() == "[events]":
            in_events = True
            continue
        if in_events and line_clean.startswith("[") and line_clean.endswith("]"):
            in_events = False
            continue
        if in_events and line_clean.lower().startswith("format:"):
            parts = [p.strip().lower() for p in line_clean[7:].split(",")]
            format_indices = {k: i for i, k in enumerate(parts)}
            continue
        if in_events and (line_clean.startswith("Dialogue:") or line_clean.startswith("Comment:")):
            prefix = "dialogue:" if line_clean.startswith("Dialogue:") else "comment:"
            raw_data = line_clean[len(prefix):].strip()
            num_fields = len(format_indices) if format_indices else 10
            parts = raw_data.split(",", num_fields - 1)
            if len(parts) < num_fields:
                continue

            start_str = parts[format_indices.get("start", 1)].strip()
            end_str = parts[format_indices.get("end", 2)].strip()
            text_str = parts[-1].strip().replace(r"\N", "\n").replace(r"\n", "\n")
            text_clean = strip_ass_tags(text_str).strip()
            if not text_clean:
                continue

            # ASS timestamp: H:MM:SS.cs
            try:
                def ass_ts_to_sec(ts: str) -> float:
                    m = re.match(r"(\d+):(\d{2}):(\d{2})[.,](\d+)", ts)
                    if m:
                        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        cs = int(m.group(4).ljust(2, "0")[:2])
                        return h * 3600 + mn * 60 + s + cs / 100.0
                    return 0.0

                start = ass_ts_to_sec(start_str)
                end = ass_ts_to_sec(end_str)
                if end > start:
                    items.append(SubtitleItem(start, end, text_clean))
            except Exception:
                continue

    items.sort(key=lambda x: x.start)
    return items


def load_subtitle_file(file_path: Path) -> List[SubtitleItem]:
    """读取并解析外挂字幕文件 (.srt / .ass / .ssa / .vtt)。"""
    try:
        raw = file_path.read_bytes()
        suffix = file_path.suffix.lower()
        if suffix in (".ass", ".ssa"):
            return parse_ass_dialogues(raw)
        return parse_srt(raw)
    except Exception:
        return []


def probe_embedded_subtitles(video_path: str | Path) -> List[TrackInfo]:
    """用 ffprobe 探测内嵌字幕轨。"""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index,codec_name:stream_tags=language,title",
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout)
        streams = data.get("streams", [])
        tracks = []
        for s in streams:
            idx = s.get("index")
            if idx is None:
                continue
            tags = s.get("tags") or {}
            lang = tags.get("language") or tags.get("lang") or ""
            title = tags.get("title") or ""
            codec = s.get("codec_name") or ""
            tracks.append(
                TrackInfo(
                    index=int(idx),
                    lang=lang,
                    title=title,
                    codec=codec,
                    is_external=False,
                )
            )
        return tracks
    except Exception:
        return []


def find_external_subtitles(video_path: str | Path) -> List[TrackInfo]:
    """查找视频同目录下的匹配外挂字幕文件。"""
    video = Path(video_path)
    if not video.parent.exists():
        return []

    results = []
    prefix = video.stem.lower()
    for f in video.parent.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUB_EXTS:
            continue
        fname = f.stem.lower()
        # 排除自生成的双语字幕
        if any(tag in fname for tag in (".dual", ".zh-cn&en", "双语", ".zh_en")):
            continue
        # 必须匹配视频前缀
        if not fname.startswith(prefix):
            continue

        extra = fname[len(prefix):].strip("._-")
        detected_lang = ""
        if is_chinese_lang(extra):
            detected_lang = "chi"
        elif is_english_lang(extra):
            detected_lang = "eng"
        elif is_japanese_lang(extra):
            detected_lang = "jpn"
        elif is_korean_lang(extra):
            detected_lang = "kor"
        else:
            # 读取前 1000 字节检测字符特征
            try:
                sample_items = load_subtitle_file(f)[:5]
                sample_text = " ".join(it.text for it in sample_items)
                if has_japanese(sample_text):
                    detected_lang = "jpn"
                elif has_cjk(sample_text):
                    detected_lang = "chi"
                else:
                    detected_lang = "eng"
            except Exception:
                detected_lang = "unk"

        results.append(
            TrackInfo(
                index=-1,
                lang=detected_lang,
                title=f.name,
                is_external=True,
                external_path=f,
            )
        )
    return results


def extract_stream_to_srt(video_path: str | Path, stream_index: int, dest_srt: Path) -> Tuple[bool, str]:
    """使用 ffmpeg 提取指定内嵌字幕流到临时 SRT 文件。"""
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-map",
                f"0:{stream_index}",
                "-f",
                "srt",
                str(dest_srt),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if r.returncode != 0 or not dest_srt.exists() or dest_srt.stat().st_size == 0:
            return False, (r.stderr.strip()[:300] or "提取结果为空或非文本字幕轨")
        return True, ""
    except Exception as e:
        return False, str(e)


def extract_track_items(
    video_path: str | Path, track: TrackInfo, temp_dir: Path
) -> Tuple[bool, List[SubtitleItem], str]:
    """获取某条 Track 的 SubtitleItem 列表（无论是外挂还是内嵌）。"""
    if track.is_external and track.external_path:
        items = load_subtitle_file(track.external_path)
        if items:
            return True, items, f"读取外挂字幕 {track.external_path.name} (共 {len(items)} 条)"
        return False, [], f"外挂字幕 {track.external_path.name} 为空或损坏"

    # 内嵌字幕流
    if is_bitmap_codec(track.codec):
        return (
            False,
            [],
            f"内嵌字幕轨 #{track.index} 为图形位图字幕 ({track.codec})，非文本格式，无法直接转为文本",
        )

    tmp_file = temp_dir / f"extract_{track.index}_{os.getpid()}.srt"
    try:
        ok, err = extract_stream_to_srt(video_path, track.index, tmp_file)
        if not ok:
            return False, [], f"内嵌字幕轨 #{track.index} 提取失败: {err}"
        items = parse_srt(tmp_file.read_bytes())
        return True, items, f"提取内嵌字幕轨 #{track.index} (共 {len(items)} 条)"
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass


def is_track_mixed(track: TrackInfo, sample_items: List[SubtitleItem]) -> bool:
    """判断单轨字幕是否为中英双语混合。"""
    title_lower = (track.title or "").lower()
    keys = ("双语", "中英", "chs&eng", "cht&eng", "dual", "bilingual", "中字英音")
    if any(k in title_lower for k in keys):
        return True
    if sample_items:
        # 检测是否每句都既有 CJK 又有英文
        mixed_count = 0
        for it in sample_items[:10]:
            lines = [ln.strip() for ln in it.text.splitlines() if ln.strip()]
            has_zh = any(has_cjk(l) for l in lines)
            has_en = any(not has_cjk(l) and bool(re.search(r"[a-zA-Z]", l)) for l in lines)
            if has_zh and has_en:
                mixed_count += 1
        if mixed_count >= 3:
            return True
    return False


def split_mixed_track(items: List[SubtitleItem]) -> Tuple[List[SubtitleItem], List[SubtitleItem]]:
    """从单轨中英混合字幕中拆出独立的中文和英文字幕序列。"""
    zh_items = []
    en_items = []
    for it in items:
        lines = [strip_ass_tags(ln).strip() for ln in it.text.splitlines() if ln.strip()]
        zh_lines = [ln for ln in lines if has_cjk(ln)]
        en_lines = [ln for ln in lines if not has_cjk(ln) and re.search(r"[a-zA-Z]", ln)]
        if zh_lines:
            zh_items.append(SubtitleItem(it.start, it.end, "\n".join(zh_lines)))
        if en_lines:
            en_items.append(SubtitleItem(it.start, it.end, "\n".join(en_lines)))
    return zh_items, en_items


def check_native_bilingual(
    video_path: str | Path, temp_dir: Optional[Path] = None
) -> Tuple[bool, str]:
    """检测影片是否本来就带有双语字幕（内嵌双语轨或外挂双语文件）。"""
    video = Path(video_path)

    bilingual_keys = (
        "双语",
        "中英",
        "chs&eng",
        "cht&eng",
        "chs.eng",
        "cht.eng",
        "chs_eng",
        "cht_eng",
        "dual",
        "bilingual",
        "简英",
        "繁英",
        "中字英音",
        "中英双字",
        "中英双语",
    )

    # 1. 检查同目录下外挂字幕
    if video.parent.exists():
        prefix = video.stem.lower()
        for f in video.parent.iterdir():
            if not f.is_file() or f.suffix.lower() not in SUB_EXTS:
                continue
            fname = f.stem.lower()
            # 排除插件自生成的默认双语字幕
            if ".zh-cn&en.default" in fname:
                continue
            if fname.startswith(prefix):
                extra = fname[len(prefix):]
                if any(k in extra for k in bilingual_keys):
                    return True, f"已存在外挂双语字幕 ({f.name})"
                # 采样外挂字幕内容是否为双语
                try:
                    items = load_subtitle_file(f)[:20]
                    if items and is_track_mixed(TrackInfo(index=-1, lang="", title=f.name), items):
                        return True, f"外挂字幕 ({f.name}) 经采样为中英双语混合"
                except Exception:
                    pass

    # 2. 检查内嵌字幕轨标题
    emb_tracks = probe_embedded_subtitles(video)
    for t in emb_tracks:
        title_lower = (t.title or "").lower()
        if any(k in title_lower for k in bilingual_keys):
            return True, f"内嵌字幕轨 #{t.index} 包含双语标识 (标题: {t.title})"

    # 3. 采样检测内嵌中文轨是否为双语混合内容
    if emb_tracks:
        zh_candidates = [
            t for t in emb_tracks
            if is_chinese_lang(t.lang) or "中" in (t.title or "")
        ]
        if zh_candidates:
            def _probe_tracks(target_dir: Path):
                for t in zh_candidates[:3]:
                    ok, items, _ = extract_track_items(video, t, target_dir)
                    if ok and items and is_track_mixed(t, items[:20]):
                        return True, f"内嵌字幕轨 #{t.index} ({t.title or t.lang}) 经采样为双语混合"
                return False, ""

            if temp_dir:
                detected, desc = _probe_tracks(temp_dir)
                if detected:
                    return True, desc
            else:
                try:
                    with tempfile.TemporaryDirectory() as tmp_d:
                        detected, desc = _probe_tracks(Path(tmp_d))
                        if detected:
                            return True, desc
                except Exception:
                    pass

    return False, ""

