#!/usr/bin/env python3
"""DualSub Web - 影视库双语字幕合并 Web 工具 (纯标准库版, 零第三方依赖)

在 NAS 上运行:  python3 app.py [端口]
浏览器访问:    http://<NAS_IP>:6543

功能:
  - 浏览媒体目录 (可输入任意路径)
  - 扫描 MKV/MP4 内嵌字幕轨, 自动识别中/英
  - 选择文件批量生成 <名字>.dual.srt 双语字幕
  - 实时进度 + 结果统计

仅依赖 Python 3 标准库 (http.server), 无需 pip/flask/docker。
"""
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading
import uuid
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from merge_dual import parse_srt, merge_dual, render_srt

# ---------------- 日志 ----------------
LOG_FILE = os.environ.get("DSUBS_LOG", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dualsub.log"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("dualsub")


def log(msg):
    logger.info(msg)

# ---------------- 配置 ----------------
DEFAULT_PORT = 6543
# 媒体根目录: 默认 / , 可在界面里输入任意路径浏览
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/")
if not MEDIA_ROOT.endswith("/"):
    MEDIA_ROOT += "/"

VIDEO_EXTS = {".mkv", ".mp4", ".ts", ".avi", ".wmv", ".m2ts", ".mov", ".flv",
              ".webm", ".m4v", ".rmvb", ".rm", ".3gp", ".mpg", ".mpeg", ".vob"}

# ---------------- 设置存储 ----------------
SETTINGS_FILE = os.environ.get("DSUBS_SETTINGS", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.json"))
DEFAULT_SETTINGS = {
    "default_dir": "",          # 默认/上次打开的目录 (相对 MEDIA_ROOT)
    "mode": "srt",              # 处理模式: srt | mux | both
    "backup": True,             # 合并回视频时是否保留原文件备份 (.bak)
    "order": "en_first",        # 双语顺序: en_first(英文在上) | zh_first(中文在上)
    "watch_dir": "",            # 自动监视目录 (绝对路径, 空=关闭)
    "watch_interval": 30,       # 监视扫描间隔 (分钟)
    "overwrite": True,          # 合并回视频: True=覆盖原文件 | False=生成.dual.mkv新文件
}
SETTINGS_LOCK = threading.Lock()


def load_settings():
    """读取设置, 不存在时返回默认值."""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
    except Exception as e:
        log(f"[settings] 读取失败: {e}")
    return dict(DEFAULT_SETTINGS)


def save_settings(new_vals):
    """保存设置 (部分更新), 返回更新后的完整设置."""
    with SETTINGS_LOCK:
        cur = load_settings()
        cur.update(new_vals)
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"[settings] 保存失败: {e}")
            return {"error": str(e)}
    log(f"[settings] 已保存: {json.dumps(cur, ensure_ascii=False)}")
    return cur

# 任务存储: {task_id: {...}}
TASKS = {}
TASKS_LOCK = threading.Lock()
FILE_LOCKS = defaultdict(threading.Lock)

# ---------------- 工具函数 ----------------
def is_chinese(lang):
    return (lang or "").lower() in {
        "chi", "zho", "zh", "chs", "cht", "zh-hans", "zh-hant",
        "zh-cn", "zh-tw", "cmn", "yue", "chinese",
    }


def is_english(lang):
    return (lang or "").lower() in {"eng", "en", "en-us", "en-gb", "english"}


# 字幕探测缓存: {path: (mtime, size, result)}
# 同一文件只要 mtime/大小没变, 就不再重复跑 ffprobe
PROBE_CACHE = {}
PROBE_CACHE_LOCK = threading.Lock()
PROBE_CACHE_MAX = 2000   # 最多缓存 2000 个文件的探测结果


def probe_subtitles(path):
    """ffprobe 探测字幕轨, 返回 {'tracks': [{index, lang, title}]} 或 {'error': msg}

    带缓存: 文件未变化时直接返回上次结果, 避免重复起 ffprobe 进程。
    """
    try:
        st = os.stat(path)
        key = (st.st_mtime, st.st_size)
    except OSError:
        return {"error": f"无法访问文件: {path}"}

    with PROBE_CACHE_LOCK:
        cached = PROBE_CACHE.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]

    # 未命中缓存 -> 跑 ffprobe
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index:stream_tags=language,title",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {"error": "未找到 ffprobe, 请确认系统已安装 ffmpeg"}
    except Exception as e:
        return {"error": str(e)}
    tracks = []
    for line in out.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(",")
        if parts and parts[0].isdigit():
            idx = int(parts[0])
            lang = parts[1] if len(parts) > 1 else ""
            title = parts[2] if len(parts) > 2 else ""
            tracks.append({"index": idx, "lang": lang, "title": title})
    result = {"tracks": tracks}

    with PROBE_CACHE_LOCK:
        # 控制缓存大小
        if len(PROBE_CACHE) >= PROBE_CACHE_MAX:
            PROBE_CACHE.clear()
        PROBE_CACHE[path] = (key, result)
    return result


def extract_subtitle(video_path, track_index, dest):
    """提取字幕轨到 SRT 文件。返回 (ok, err)"""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(video_path), "-map", f"0:{track_index}",
             "-f", "srt", str(dest)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
            return False, r.stderr.strip()[:300] or "提取结果为空"
        return True, ""
    except Exception as e:
        return False, str(e)


def generate_dual(video_path, zh_idx, en_idx, out_path, order="en_first"):
    """提取中英轨并合并为双语 SRT。返回 (ok, detail, logs)

    order: "en_first" 英文在上 / "zh_first" 中文在上
    """
    logs = []
    zh_srt = out_path.with_suffix(".zh.tmp.srt")
    en_srt = out_path.with_suffix(".en.tmp.srt")
    try:
        logs.append(f"提取中文字幕轨 #{zh_idx} ...")
        ok, err = extract_subtitle(video_path, zh_idx, zh_srt)
        if not ok:
            return False, err, logs
        logs.append(f"提取英文字幕轨 #{en_idx} ...")
        ok, err = extract_subtitle(video_path, en_idx, en_srt)
        if not ok:
            return False, err, logs
        logs.append(f"合并双语字幕 ({'英文在上' if order=='en_first' else '中文在上'}) ...")
        zh_items = parse_srt(zh_srt.read_bytes())
        en_items = parse_srt(en_srt.read_bytes())
        merged, stats = merge_dual(zh_items, en_items, order=order)
        out_path.write_text(render_srt(merged), encoding="utf-8")
        logs.append(f"完成: 生成 {out_path.name} ({stats['total']} 条)")
        return True, stats, logs
    except Exception as e:
        return False, str(e), logs
    finally:
        for tmp in (zh_srt, en_srt):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def mux_into_video(video_path, srt_path, backup=True, overwrite=True):
    """把双语 srt 作为新字幕轨封回 MKV (视频/音频流 copy, 不重编码).

    overwrite=True : 直接覆盖原文件 (先写临时文件, 成功后原子替换; backup=True 时原文件备份为 .bak)
    overwrite=False: 生成新文件 <name>.dual.mkv, 保留原文件
    返回: (ok, out_path_or_err, logs)
    """
    logs = []
    video = Path(video_path)
    try:
        if overwrite:
            # 覆盖模式: 输出到临时文件, 成功后替换原文件
            tmp_out = video.with_name(video.stem + ".dual.tmp.mkv")
            final_out = video
        else:
            # 新文件模式
            tmp_out = video.with_name(video.stem + ".dual.mkv")
            if tmp_out.exists():
                import time as _t
                tmp_out = video.with_name(video.stem + f".dual.{int(_t.time())}.mkv")
            final_out = tmp_out

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-i", str(srt_path),
            "-map", "0:v", "-map", "0:a", "-map", "1:0",
            "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
            "-metadata:s:s:0", "title=中英双语",
            "-metadata:s:s:0", "language=chi",
            "-disposition:s:0", "default",
            str(tmp_out),
        ]
        logs.append(f"封回双语字幕 ...")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not tmp_out.exists():
            return False, r.stderr.strip()[-300:] or "封回失败", logs

        if overwrite:
            # 原子替换: 先备份原文件, 再替换
            if backup:
                bak = video.with_name(video.name + ".bak")
                # 若已有 .bak 则加时间戳
                if bak.exists():
                    import time as _t
                    bak = video.with_name(video.name + f".bak.{int(_t.time())}")
                os.replace(str(video), str(bak))
                logs.append(f"原文件已备份为 {bak.name}")
            os.replace(str(tmp_out), str(final_out))
            logs.append(f"完成: 已覆盖原文件 {final_out.name} (视频/音频未重编码)")
        else:
            logs.append(f"完成: {final_out.name} (视频/音频未重编码)")

        return True, str(final_out), logs
    except Exception as e:
        # 清理临时文件
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return False, str(e), logs


# ---------------- API 处理 ----------------
def api_tree(query):
    """列出某目录的子目录和视频文件数."""
    rel = query.get("path", [""])[0]
    root = Path(MEDIA_ROOT)
    target = (root / rel.lstrip("/")).resolve() if rel else root.resolve()
    if not str(target).startswith(str(root.resolve())):
        return {"error": "路径越界"}, 400
    if not target.is_dir():
        return {"error": f"不是目录: {target}"}, 400
    dirs = []
    files = 0
    try:
        entries = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return {"error": f"无权限访问: {target}"}, 403
    for e in entries:
        if e.name.startswith("."):
            continue
        if e.is_dir():
            vcount = 0
            try:
                vcount = sum(1 for f in e.rglob("*")
                             if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
            except PermissionError:
                pass
            dirs.append({"name": e.name, "path": str(e),
                         "rel": str(e.relative_to(root)),
                         "videos": vcount})
        elif e.is_file() and e.suffix.lower() in VIDEO_EXTS:
            files += 1
    parent = str(target.parent.relative_to(root)) if target != root else ""
    return {"root": str(root), "rel": rel, "current": str(target),
            "parent": parent, "dirs": dirs, "videos_here": files}, 200


def api_scan(query):
    """扫描指定目录, 返回视频列表+字幕轨信息.

    ?path=...&light=1  : light 模式, 只列文件不探测字幕 (超快, 用于目录浏览)
    ?path=...          : 完整模式, 探测字幕轨 (带缓存 + 并发)
    """
    rel = query.get("path", [""])[0]
    light = query.get("light", ["0"])[0] == "1"
    root = Path(MEDIA_ROOT)
    target = (root / rel.lstrip("/")).resolve() if rel else root.resolve()
    if not str(target).startswith(str(root.resolve())):
        return {"error": "路径越界"}, 400
    if not target.is_dir():
        return {"error": f"目录不存在: {target}"}, 404

    log(f"[scan] 目标目录= {target} (light={light})")

    # 列出目录真实内容 (便于诊断)
    try:
        all_entries = sorted(target.iterdir(), key=lambda x: x.name.lower())
    except PermissionError as e:
        log(f"[scan] 无权限: {e}")
        return {"error": f"无权限访问: {target}"}, 403
    log(f"[scan] 目录共 {len(all_entries)} 个条目: " +
        ", ".join(f"{e.name}({'目录' if e.is_dir() else '文件'})" for e in all_entries[:50]))

    videos = []
    # 仅扫描当前目录 (不递归子目录)
    for p in all_entries:
        if p.name.startswith("."):
            continue
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            try:
                relpath = str(p.relative_to(root))
            except ValueError:
                relpath = str(p)
            videos.append({"path": str(p), "rel": relpath})
    log(f"[scan] 识别到 {len(videos)} 个视频文件")

    # light 模式: 不探测字幕, 直接返回文件列表 (标记为"未探测")
    if light:
        results = []
        for v in videos:
            has_srt = Path(v["path"]).with_name(Path(v["path"]).stem + ".dual.srt").exists()
            results.append({
                "path": v["path"], "rel": v["rel"],
                "zh": None, "zh_lang": None,
                "en": None, "en_lang": None,
                "ready": None, "probed": False,
                "status": "done_srt" if has_srt else "none",
                "subs": [],
            })
        log(f"[scan] light模式完成: {len(results)} 个视频 (未探测字幕)")
        return {"videos": results, "count": len(results), "ready": 0,
                "light": True, "dir": str(target), "rel": rel}, 200

    # 完整模式: 并发探测字幕轨
    results = []

    def probe_one(v):
        probe = probe_subtitles(v["path"])
        zh_idx = zh_lang = None
        en_idx = en_lang = None
        has_dual_track = False   # 已有"中英双语"字幕轨 (说明已合并过)
        if "tracks" in probe:
            for t in probe["tracks"]:
                if zh_idx is None and is_chinese(t["lang"]):
                    zh_idx, zh_lang = t["index"], t["lang"]
                if en_idx is None and is_english(t["lang"]):
                    en_idx, en_lang = t["index"], t["lang"]
                # 检测是否已有双语轨 (标题或语言特征)
                title = (t.get("title") or "").lower()
                if "中英" in title or "dual" in title or "双语" in title:
                    has_dual_track = True

        # 检测同目录是否已有生成的 .dual.srt
        has_srt = Path(v["path"]).with_name(Path(v["path"]).stem + ".dual.srt").exists()

        # 已处理状态: done_srt | done_mux | done_both | none
        if has_srt and has_dual_track:
            status = "done_both"
        elif has_dual_track:
            status = "done_mux"
        elif has_srt:
            status = "done_srt"
        else:
            status = "none"

        return {
            "path": v["path"], "rel": v["rel"],
            "zh": zh_idx, "zh_lang": zh_lang,
            "en": en_idx, "en_lang": en_lang,
            "ready": zh_idx is not None and en_idx is not None and status == "none",
            "probed": True,
            "status": status,
            "subs": probe.get("tracks", []),
        }

    if videos:
        import concurrent.futures
        # 并发探测, 最多 4 个并行 (避免 ffprobe 太多撑爆 IO)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(videos))) as ex:
            results = list(ex.map(probe_one, videos))
        for r in results:
            log(f"[scan]   {r['rel']}: 中轨={r['zh']} 英轨={r['en']}")

    ready = sum(1 for r in results if r["ready"])
    log(f"[scan] 完成: 共{len(results)}个视频, {ready}个可处理")
    return {"videos": results, "count": len(results), "ready": ready,
            "dir": str(target), "rel": rel}, 200


def api_task_start(body):
    """启动处理任务. body: {files: [{path, zh, en}], mode: 'srt'|'mux'|'both'}"""
    try:
        data = json.loads(body) if isinstance(body, (bytes, str)) else body
    except Exception:
        return {"error": "无效的 JSON"}, 400
    files = data.get("files", []) if isinstance(data, dict) else []
    if not files:
        return {"error": "未选择文件"}, 400
    # 处理模式: 请求里指定, 否则用设置里的默认
    mode = data.get("mode") or load_settings().get("mode", "srt")
    if mode not in ("srt", "mux", "both"):
        mode = "srt"
    backup = load_settings().get("backup", True)
    overwrite = data.get("overwrite")
    if overwrite is None:
        overwrite = load_settings().get("overwrite", True)
    order = data.get("order") or load_settings().get("order", "en_first")
    if order not in ("en_first", "zh_first"):
        order = "en_first"
    task_id = uuid.uuid4().hex[:12]
    with TASKS_LOCK:
        TASKS[task_id] = {
            "id": task_id, "status": "running", "progress": 0,
            "total": len(files), "done": 0, "results": [],
            "current": "", "started": datetime.now().isoformat(),
            "cancel": False, "mode": mode, "order": order,
        }
    log(f"[task] 新任务 {task_id}: {len(files)} 个文件, 模式={mode}, 顺序={order}")

    def worker():
        task = TASKS[task_id]
        for i, f in enumerate(files, 1):
            if task["cancel"]:
                task["status"] = "cancelled"
                return
            path = f.get("path", "")
            zh = f.get("zh")
            en = f.get("en")
            task["current"] = os.path.basename(path)
            task["progress"] = int((i - 1) / len(files) * 100)
            result = {"path": path, "name": os.path.basename(path),
                      "status": "skip", "message": "", "logs": []}
            try:
                with FILE_LOCKS[path]:
                    video = Path(path)
                    if not video.exists():
                        result.update(status="fail", message="文件不存在")
                    elif zh is None or en is None:
                        result.update(status="skip", message="缺少中/英字幕轨")
                    else:
                        srt_out = video.with_name(video.stem + ".dual.srt")
                        # 1. 生成双语 srt
                        ok, detail, logs = generate_dual(video, zh, en, srt_out, order=order)
                        result["logs"] = list(logs)
                        if not ok:
                            result.update(status="fail", message=str(detail))
                        else:
                            msgs = [f"已生成 {srt_out.name}"]
                            if mode in ("mux", "both"):
                                # 2. 封回视频
                                ok2, out2, logs2 = mux_into_video(video, srt_out, backup=backup, overwrite=overwrite)
                                result["logs"] += logs2
                                if ok2:
                                    msgs.append(f"已封回 {Path(out2).name}")
                                    result.update(status="ok", message="；".join(msgs))
                                else:
                                    result.update(status="fail", message=f"{srt_out.name} 已生成, 但封回失败: {out2}")
                            else:
                                result.update(status="ok", message="；".join(msgs))
            except Exception as e:
                result.update(status="fail", message=str(e))
            task["results"].append(result)
            log(f"[task] [{i}/{len(files)}] {result['name']} -> {result['status']}: {result['message']}")
            task["done"] = i
            task["progress"] = int(i / len(files) * 100)
        task["status"] = "done"
        log(f"[task] {task_id} 全部完成: " +
            f"成功{sum(1 for r in task['results'] if r['status']=='ok')} " +
            f"失败{sum(1 for r in task['results'] if r['status']=='fail')} " +
            f"跳过{sum(1 for r in task['results'] if r['status']=='skip')}")

    threading.Thread(target=worker, daemon=True).start()
    return {"task_id": task_id}, 200


def api_task_status(task_id):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return {"error": "任务不存在"}, 404
    ok = sum(1 for r in task["results"] if r["status"] == "ok")
    fail = sum(1 for r in task["results"] if r["status"] == "fail")
    skip = sum(1 for r in task["results"] if r["status"] == "skip")
    return {
        "status": task["status"], "progress": task["progress"],
        "total": task["total"], "done": task["done"],
        "current": task["current"], "ok": ok, "fail": fail, "skip": skip,
        "results": task["results"],
    }, 200


def api_task_cancel(task_id):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if task and task["status"] == "running":
            task["cancel"] = True
            return {"ok": True}, 200
    return {"ok": False}, 200


def api_preview(query):
    """预览某文件字幕轨内容."""
    path = query.get("path", [""])[0]
    track = query.get("track", [""])[0]
    if not path or not track.isdigit():
        return {"error": "参数错误"}, 400
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-map", f"0:{track}", "-f", "srt", "-"],
            capture_output=True, timeout=30,
        )
        return {"preview": r.stdout.decode("utf-8", errors="replace")[:2000]}, 200
    except Exception as e:
        return {"error": str(e)}, 500


def api_health(query=None):
    return {"status": "ok"}, 200


def api_logs(query=None):
    """返回日志文件内容 (用于排查问题)."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, encoding="utf-8") as f:
                lines = f.readlines()
            # 返回最近 200 行
            return {"logs": "".join(lines[-200:])}, 200
        return {"logs": "(暂无日志)"}, 200
    except Exception as e:
        return {"error": str(e)}, 500


def api_settings_get(query=None):
    """返回当前设置 (含默认目录/处理模式/备份选项)."""
    return load_settings(), 200


def api_settings_save(body):
    """保存设置. body: {default_dir?, mode?, backup?}"""
    try:
        data = json.loads(body) if isinstance(body, (bytes, str)) else body
    except Exception:
        return {"error": "无效的 JSON"}, 400
    if not isinstance(data, dict):
        return {"error": "无效的设置"}, 400
    allowed = {"default_dir", "mode", "backup", "order", "watch_dir", "watch_interval", "overwrite"}
    to_save = {k: v for k, v in data.items() if k in allowed}
    # 校验 mode
    if "mode" in to_save and to_save["mode"] not in ("srt", "mux", "both"):
        to_save["mode"] = "srt"
    result = save_settings(to_save)
    if isinstance(result, dict) and "error" in result:
        return result, 500
    return result, 200


# ---------------- HTTP Handler ----------------
ROUTES = {
    "/api/tree": ("GET", api_tree),
    "/api/scan": ("GET", api_scan),
    "/api/preview": ("GET", api_preview),
    "/api/health": ("GET", api_health),
    "/api/logs": ("GET", api_logs),
    "/api/settings": ("GET", api_settings_get),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DualSub/1.0"

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        """发送静态文件 (HTML/JS/CSS 等)."""
        try:
            data = Path(path).read_bytes()
        except Exception:
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            idx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "templates", "index.html")
            if os.path.exists(idx):
                html = Path(idx).read_text(encoding="utf-8")
                html = html.replace("{{ media_root }}", MEDIA_ROOT)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "index.html not found")
            return

        # 静态资源
        if path.startswith("/static/"):
            base = os.path.dirname(os.path.abspath(__file__))
            fpath = os.path.join(base, path.lstrip("/"))
            if os.path.isfile(fpath):
                self._send_file(fpath)
                return
            self.send_error(404)
            return

        # API
        if path in ROUTES:
            method, func = ROUTES[path]
            if method != "GET":
                self.send_error(405)
                return
            log(f"[http] GET {self.path}")
            try:
                result, status = func(query)
            except Exception as e:
                result, status = {"error": str(e)}, 500
            self._send_json(result, status)
            return

        # 任务 API (带参数)
        if path.startswith("/api/task/"):
            parts = path.split("/")
            if len(parts) >= 4:
                tid = parts[3]
                action = parts[4] if len(parts) > 4 else None
                if action == "cancel" and self.command == "POST":
                    result, status = api_task_cancel(tid)
                    self._send_json(result, status)
                    return
                result, status = api_task_status(tid)
                self._send_json(result, status)
                return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
            except Exception:
                body = b"{}"
            log(f"[http] POST /api/settings body={body.decode('utf-8', 'replace')[:300]}")
            try:
                result, status = api_settings_save(body)
            except Exception as e:
                result, status = {"error": str(e)}, 500
            self._send_json(result, status)
            return

        if path == "/api/task/start":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b"{}"
            except Exception:
                body = b"{}"
            log(f"[http] POST /api/task/start body={body.decode('utf-8', 'replace')[:500]}")
            try:
                result, status = api_task_start(body)
            except Exception as e:
                result, status = {"error": str(e)}, 500
            self._send_json(result, status)
            return

        if path.startswith("/api/task/") and path.endswith("/cancel"):
            parts = path.split("/")
            if len(parts) >= 4:
                result, status = api_task_cancel(parts[3])
                self._send_json(result, status)
                return

        self.send_error(404)

    def log_message(self, fmt, *args):
        # 精简访问日志: 只记录 5xx 错误
        try:
            msg = fmt % args
        except Exception:
            return
        if msg.strip() and " 5" in msg:
            sys.stderr.write(f"[{self.log_date_time_string()}] {msg}\n")


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    host = "0.0.0.0"
    print(f"🎬 DualSub Web 启动: http://0.0.0.0:{port}")
    print(f"   媒体根目录: {MEDIA_ROOT}")
    print(f"   按 Ctrl+C 停止")
    # 启动自动监视线程
    start_watcher()
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


# ---------------- 自动监视 ----------------
WATCHER_THREAD = None
WATCHER_STOP = threading.Event()


def _probe_file_tracks(path):
    """探测单个文件的中英轨索引. 返回 (zh_idx, en_idx) 或 (None, None)"""
    probe = probe_subtitles(path)
    zh = en = None
    if "tracks" in probe:
        for t in probe["tracks"]:
            if zh is None and is_chinese(t["lang"]):
                zh = t["index"]
            if en is None and is_english(t["lang"]):
                en = t["index"]
    return zh, en


def watcher_loop():
    """后台循环: 定期扫描 watch_dir, 自动处理新入库且含中英轨的视频."""
    log("[watcher] 自动监视线程已启动")
    while not WATCHER_STOP.is_set():
        try:
            settings = load_settings()
            watch_dir = settings.get("watch_dir", "")
            interval = max(1, int(settings.get("watch_interval", 30)))
            mode = settings.get("mode", "srt")
            order = settings.get("order", "en_first")
            backup = settings.get("backup", True)
            overwrite = settings.get("overwrite", True)

            if watch_dir and os.path.isdir(watch_dir):
                # 扫描目录下所有视频 (递归)
                found = []
                for p in sorted(Path(watch_dir).rglob("*")):
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                        # 跳过已经处理过的 (已有 .dual.srt 或 .dual.mkv)
                        if p.with_name(p.stem + ".dual.srt").exists():
                            continue
                        if p.with_name(p.stem + ".dual.mkv").exists():
                            continue
                        # 跳过 .bak 文件
                        if p.name.endswith(".bak"):
                            continue
                        found.append(p)

                if found:
                    log(f"[watcher] 发现 {len(found)} 个待处理视频: " +
                        ", ".join(os.path.basename(str(p)) for p in found[:10]))
                    for video in found:
                        if WATCHER_STOP.is_set():
                            break
                        srt_out = video.with_name(video.stem + ".dual.srt")
                        if srt_out.exists():
                            continue
                        try:
                            with FILE_LOCKS[str(video)]:
                                zh, en = _probe_file_tracks(str(video))
                                if zh is None or en is None:
                                    log(f"[watcher] 跳过(缺中英轨): {video.name}")
                                    continue
                                log(f"[watcher] 自动处理: {video.name} (中={zh} 英={en}, 模式={mode})")
                                ok, detail, logs = generate_dual(
                                    video, zh, en, srt_out, order=order)
                                if ok:
                                    log(f"[watcher] ✅ 已生成 {srt_out.name}")
                                    if mode in ("mux", "both"):
                                        ok2, out2, logs2 = mux_into_video(video, srt_out, backup=backup, overwrite=overwrite)
                                        if ok2:
                                            log(f"[watcher] ✅ 已封回 {Path(out2).name}")
                                        else:
                                            log(f"[watcher] ⚠ srt已生成但封回失败: {out2}")
                                else:
                                    log(f"[watcher] ❌ 处理失败: {video.name} - {detail}")
                        except Exception as e:
                            log(f"[watcher] 处理异常 {video.name}: {e}")
        except Exception as e:
            log(f"[watcher] 循环异常: {e}")
        # 等待间隔 (可被打断)
        WATCHER_STOP.wait(interval * 60)


def start_watcher():
    """启动自动监视线程 (幂等)."""
    global WATCHER_THREAD
    if WATCHER_THREAD is not None and WATCHER_THREAD.is_alive():
        return
    WATCHER_STOP.clear()
    WATCHER_THREAD = threading.Thread(target=watcher_loop, daemon=True, name="dualsub-watcher")
    WATCHER_THREAD.start()
    log(f"[watcher] 已启动 (当前监视目录: {load_settings().get('watch_dir') or '无'})")


if __name__ == "__main__":
    main()
