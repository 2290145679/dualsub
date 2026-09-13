#!/usr/bin/env python3
"""MKV 无损封装模块：使用 ffmpeg 流复制将双语字幕封回视频容器。"""

import os
import subprocess
import time
from pathlib import Path
from typing import List, Tuple


def mux_into_mkv(
    video_path: str | Path,
    sub_path: str | Path,
    backup: bool = True,
    overwrite: bool = False,
) -> Tuple[bool, str, List[str]]:
    """将双语字幕追加到 MKV 容器中作为默认字幕轨（视频/音频不重新编码）。

    :param video_path: 原视频文件路径
    :param sub_path: 生成的外挂字幕文件路径 (.ass / .srt)
    :param backup: 覆盖原视频时是否先备份为 .bak
    :param overwrite: 是否直接覆盖原视频文件
    :return: (ok, final_path_or_err, logs)
    """
    logs: List[str] = []
    video = Path(video_path)
    sub = Path(sub_path)

    if not video.exists():
        return False, "视频文件不存在", logs
    if not sub.exists():
        return False, "字幕文件不存在", logs

    tmp_out = video.with_name(video.stem + f".mux.tmp.{os.getpid()}.mkv")
    final_out = video if overwrite else video.with_name(video.stem + ".dual.mkv")

    sub_codec = "ass" if sub.suffix.lower() in (".ass", ".ssa") else "srt"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(sub),
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-map",
        "1:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        sub_codec,
        "-metadata:s:s:0",
        "title=中英双语",
        "-metadata:s:s:0",
        "language=chi",
        "-disposition:s:0",
        "default",
        str(tmp_out),
    ]

    logs.append(f"执行 ffmpeg 无损混流封回 MKV ({sub_codec} 格式)...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
            err_msg = r.stderr.strip()[-300:] or "封回输出为空"
            return False, f"ffmpeg 封回失败: {err_msg}", logs

        if overwrite:
            if backup:
                bak = video.with_name(video.name + ".bak")
                if bak.exists():
                    bak = video.with_name(video.name + f".bak.{int(time.time())}")
                os.replace(str(video), str(bak))
                logs.append(f"原视频文件已备份为 {bak.name}")
            os.replace(str(tmp_out), str(final_out))
            logs.append(f"封装完成: 已更新原视频文件 {final_out.name} (音视频无损流复制)")
        else:
            if final_out.exists():
                final_out = video.with_name(video.stem + f".dual.{int(time.time())}.mkv")
            os.replace(str(tmp_out), str(final_out))
            logs.append(f"封装完成: 生成新视频 {final_out.name} (原视频保留)")

        return True, str(final_out), logs
    except Exception as e:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except Exception:
                pass
        return False, str(e), logs
