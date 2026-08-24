#!/usr/bin/env python3
"""DualSub - 影视库双语字幕合并插件 (MoviePilot V2)

监听媒体入库事件, 自动把影片内嵌的中英字幕轨合并为"上下双行"双语字幕。
- 生成 <文件名>.dual.srt 到影片同目录 (不修改原文件)
- 可选: 用 ffmpeg 把双语字幕封回 MKV (视频/音频 copy 不重编码)
- 支持手动批量处理指定目录
"""
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote, unquote

from app.core.config import settings
from app.core.event import eventmanager, Event as MPEvent
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .merge_dual import parse_srt, merge_dual, render_srt


# ---------------- 常量 ----------------
VIDEO_EXTS = {".mkv", ".mp4", ".ts", ".avi", ".wmv", ".m2ts", ".mov", ".flv",
              ".webm", ".m4v", ".rmvb", ".rm", ".3gp", ".mpg", ".mpeg", ".vob"}


def is_chinese(lang):
    return (lang or "").lower() in {
        "chi", "zho", "zh", "chs", "cht", "zh-hans", "zh-hant",
        "zh-cn", "zh-tw", "cmn", "yue", "chinese",
    }


def is_english(lang):
    return (lang or "").lower() in {"eng", "en", "en-us", "en-gb", "english"}


# ---------------- 任务模型 ----------------
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
    source: str  # TaskSource.value
    add_time: str  # isoformat
    status: str = TaskStatus.PENDING.value
    complete_time: Optional[str] = None
    message: str = ""


class DualSub(_PluginBase):
    # 插件名称
    plugin_name = "双语字幕合并"
    # 插件描述
    plugin_desc = "自动合并影片内嵌中英字幕轨为上下双行双语字幕(生成.srt或封回MKV)。支持入库自动执行, 也可在插件详情页浏览媒体库并勾选已有视频处理。"
    # 插件图标
    plugin_icon = "subtitles.png"
    # 主题色
    plugin_color = "#8d51f9"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "wuzhennana"
    # 作者主页
    author_url = "https://github.com/wuzhennana"
    # 插件配置项ID前缀
    plugin_config_prefix = "dualsub"
    # 加载顺序
    plugin_order = 18
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _listen_transfer_event = True
    _send_notify = False
    _run_now = False
    _path_list = ""
    _mode = "srt"            # srt | mux | both
    _order = "en_first"      # en_first | zh_first
    _backup = True           # 封回时是否备份原文件
    _overwrite = False       # 封回时是否覆盖原文件(默认False=生成.dual.mkv)
    _min_file_size = 0       # 触发的最小文件大小(MB), 0=不限
    _skip_if_exists = True   # 已有.dual.srt时跳过
    _scan_subdirs = True     # 手动执行时是否递归子目录
    _clear_history = False
    _browse_root = "/vol2/1000"  # 浏览页起始根目录(容器内绝对路径)
    _browse_path = ""         # 当前浏览的目录路径(运行时状态, 非持久配置)
    _page_tab = "browse"      # 浏览页当前 Tab: browse | history

    # 任务队列与消费线程(实例级, 在 init_plugin 中初始化)
    _task_queue = None
    _consumer_thread = None
    _running = False
    _stop_event = None
    _tasks: Dict[str, dict] = None
    _file_locks: Dict[str, threading.Lock] = None
    _file_locks_lock = None

    def _init_state(self):
        """初始化实例级可变状态(避免类属性在多实例间共享)"""
        if self._stop_event is None:
            self._stop_event = threading.Event()
        if self._file_locks is None:
            self._file_locks = {}
        if self._file_locks_lock is None:
            self._file_locks_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        self._init_state()
        if not config:
            return
        self._enabled = config.get("enabled", False)
        self._listen_transfer_event = config.get("listen_transfer_event", True)
        self._send_notify = config.get("send_notify", False)
        self._run_now = config.get("run_now", False)
        self._path_list = config.get("path_list", "")
        self._mode = config.get("mode", "srt")
        if self._mode not in ("srt", "mux", "both"):
            self._mode = "srt"
        self._order = config.get("order", "en_first")
        if self._order not in ("en_first", "zh_first"):
            self._order = "en_first"
        self._backup = config.get("backup", True)
        self._overwrite = config.get("overwrite", False)
        try:
            self._min_file_size = int(config.get("min_file_size", 0) or 0)
        except (TypeError, ValueError):
            self._min_file_size = 0
        self._skip_if_exists = config.get("skip_if_exists", True)
        self._scan_subdirs = config.get("scan_subdirs", True)
        self._clear_history = config.get("clear_history", False)
        self._browse_root = config.get("browse_root", "") or "/vol2/1000"
        self._browse_path = config.get("browse_path", "") or self._browse_root
        self._page_tab = config.get("page_tab", "browse")

        # 加载历史任务
        self._tasks = self.load_tasks()

        # 清理历史
        if self._clear_history:
            self.clear_tasks()
            self._clear_history = False
            self.update_config({
                "enabled": self._enabled,
                "listen_transfer_event": self._listen_transfer_event,
                "send_notify": self._send_notify,
                "run_now": False,
                "path_list": self._path_list,
                "mode": self._mode,
                "order": self._order,
                "backup": self._backup,
                "overwrite": self._overwrite,
                "min_file_size": self._min_file_size,
                "skip_if_exists": self._skip_if_exists,
                "scan_subdirs": self._scan_subdirs,
                "clear_history": False,
                "browse_root": self._browse_root,
                "browse_path": self._browse_path,
                "page_tab": self._page_tab,
            })

        # 启动消费线程
        if self._enabled:
            self.start_consumer()

        # 手动执行一次
        if self._run_now:
            self._run_now = False
            self.update_config({
                "enabled": self._enabled,
                "listen_transfer_event": self._listen_transfer_event,
                "send_notify": self._send_notify,
                "run_now": False,
                "path_list": self._path_list,
                "mode": self._mode,
                "order": self._order,
                "backup": self._backup,
                "overwrite": self._overwrite,
                "min_file_size": self._min_file_size,
                "skip_if_exists": self._skip_if_exists,
                "scan_subdirs": self._scan_subdirs,
                "clear_history": False,
                "browse_root": self._browse_root,
                "browse_path": self._browse_path,
                "page_tab": self._page_tab,
            })
            self.run_manual()

    # ---------------- 字幕探测/提取/合并/封回 ----------------
    def probe_subtitles(self, path: str) -> dict:
        """ffprobe 探测字幕轨, 返回 {'tracks': [{index, lang, title}]} 或 {'error': msg}"""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "s",
                 "-show_entries", "stream=index:stream_tags=language,title",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return {"error": "未找到 ffprobe, 请确认容器内已安装 ffmpeg"}
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
        return {"tracks": tracks}

    def probe_zh_en_tracks(self, path: str) -> Tuple[Optional[int], Optional[int]]:
        """探测单个文件的中英轨索引. 返回 (zh_idx, en_idx) 或 (None, None)"""
        probe = self.probe_subtitles(path)
        zh = en = None
        if "tracks" in probe:
            for t in probe["tracks"]:
                if zh is None and is_chinese(t["lang"]):
                    zh = t["index"]
                if en is None and is_english(t["lang"]):
                    en = t["index"]
        return zh, en

    def extract_subtitle(self, video_path: str, track_index: int, dest: Path) -> Tuple[bool, str]:
        """提取字幕轨到 SRT 文件。返回 (ok, err)"""
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(video_path), "-map", f"0:{track_index}",
                 "-f", "srt", str(dest)],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
                return False, (r.stderr.strip()[:300] or "提取结果为空")
            return True, ""
        except Exception as e:
            return False, str(e)

    def generate_dual(self, video_path: str, zh_idx: int, en_idx: int,
                      out_path: Path, order: str = "en_first") -> Tuple[bool, Any, List[str]]:
        """提取中英轨并合并为双语 SRT。返回 (ok, detail, logs)"""
        logs: List[str] = []
        zh_srt = out_path.with_suffix(".zh.tmp.srt")
        en_srt = out_path.with_suffix(".en.tmp.srt")
        try:
            logs.append(f"提取中文字幕轨 #{zh_idx} ...")
            ok, err = self.extract_subtitle(video_path, zh_idx, zh_srt)
            if not ok:
                return False, err, logs
            logs.append(f"提取英文字幕轨 #{en_idx} ...")
            ok, err = self.extract_subtitle(video_path, en_idx, en_srt)
            if not ok:
                return False, err, logs
            logs.append(f"合并双语字幕 ({'英文在上' if order == 'en_first' else '中文在上'}) ...")
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

    def mux_into_video(self, video_path: str, srt_path: str,
                       backup: bool = True, overwrite: bool = False) -> Tuple[bool, str, List[str]]:
        """把双语 srt 作为新字幕轨封回 MKV (视频/音频流 copy, 不重编码).

        overwrite=True  : 直接覆盖原文件 (先写临时文件, 成功后原子替换; backup=True 时原文件备份为 .bak)
        overwrite=False : 生成新文件 <name>.dual.mkv, 保留原文件
        返回: (ok, out_path_or_err, logs)
        """
        logs: List[str] = []
        video = Path(video_path)
        try:
            if overwrite:
                tmp_out = video.with_name(video.stem + ".dual.tmp.mkv")
                final_out = video
            else:
                tmp_out = video.with_name(video.stem + ".dual.mkv")
                if tmp_out.exists():
                    tmp_out = video.with_name(video.stem + f".dual.{int(time.time())}.mkv")
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
            logs.append("封回双语字幕 ...")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0 or not tmp_out.exists():
                return False, (r.stderr.strip()[-300:] or "封回失败"), logs

            if overwrite:
                if backup:
                    bak = video.with_name(video.name + ".bak")
                    if bak.exists():
                        bak = video.with_name(video.name + f".bak.{int(time.time())}")
                    os.replace(str(video), str(bak))
                    logs.append(f"原文件已备份为 {bak.name}")
                os.replace(str(tmp_out), str(final_out))
                logs.append(f"完成: 已覆盖原文件 {final_out.name} (视频/音频未重编码)")
            else:
                logs.append(f"完成: {final_out.name} (视频/音频未重编码)")
            return True, str(final_out), logs
        except Exception as e:
            try:
                tmp_out.unlink(missing_ok=True)  # noqa
            except Exception:
                pass
            return False, str(e), logs

    # ---------------- 处理单个视频 ----------------
    def _file_lock(self, path: str) -> threading.Lock:
        with self._file_locks_lock:
            if path not in self._file_locks:
                self._file_locks[path] = threading.Lock()
            return self._file_locks[path]

    def _should_skip(self, video_path: str) -> bool:
        """是否已有双语字幕产物, 需跳过"""
        video = Path(video_path)
        if self._skip_if_exists:
            if video.with_name(video.stem + ".dual.srt").exists():
                return True
            if video.with_name(video.stem + ".dual.mkv").exists():
                return True
        return False

    def process_video(self, video_path: str) -> Tuple[str, str]:
        """处理单个视频文件。返回 (TaskStatus.value, message)"""
        video = Path(video_path)
        if not video.exists():
            return TaskStatus.FAILED.value, "文件不存在"
        if video.suffix.lower() not in VIDEO_EXTS:
            return TaskStatus.IGNORED.value, "非视频文件"
        if self._min_file_size > 0:
            try:
                size_mb = video.stat().st_size / (1024 * 1024)
                if size_mb < self._min_file_size:
                    return TaskStatus.IGNORED.value, f"文件小于 {self._min_file_size}MB, 跳过"
            except Exception:
                pass
        if self._should_skip(video_path):
            return TaskStatus.IGNORED.value, "已存在双语字幕, 跳过"

        with self._file_lock(video_path):
            zh, en = self.probe_zh_en_tracks(video_path)
            if zh is None or en is None:
                return TaskStatus.IGNORED.value, "缺少中文或英文字幕轨"
            logger.info(f"[DualSub] 处理: {video.name} (中轨={zh} 英轨={en} 模式={self._mode})")
            srt_out = video.with_name(video.stem + ".dual.srt")
            ok, detail, logs = self.generate_dual(str(video), zh, en, srt_out, order=self._order)
            if not ok:
                return TaskStatus.FAILED.value, f"生成失败: {detail}"
            msgs = [f"已生成 {srt_out.name}"]
            if self._mode in ("mux", "both"):
                ok2, out2, logs2 = self.mux_into_video(
                    str(video), str(srt_out), backup=self._backup, overwrite=self._overwrite)
                if ok2:
                    msgs.append(f"已封回 {Path(out2).name}")
                    return TaskStatus.COMPLETED.value, "；".join(msgs)
                else:
                    return TaskStatus.FAILED.value, f"{srt_out.name} 已生成, 但封回失败: {out2}"
            return TaskStatus.COMPLETED.value, "；".join(msgs)

    # ---------------- 事件监听 ----------------
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: MPEvent):
        """监听媒体整理完成(入库)事件"""
        if not self._enabled or not self._listen_transfer_event:
            return
        try:
            item = event.event_data or {}
            item_transfer = item.get("transferinfo")
            if not item_transfer:
                return
            file_list = getattr(item_transfer, "file_list_new", None) or []
            if not file_list:
                return
            item_media = item.get("mediainfo")
            title = getattr(item_media, "title", "") if item_media else ""
            logger.info(f"[DualSub] 监听到入库事件: {title or '(未知)'} 文件数={len(file_list)}")
            for file_path in file_list:
                if os.path.splitext(file_path)[-1].lower() in VIDEO_EXTS:
                    self.add_task(file_path, TaskSource.EVENT.value)
        except Exception as e:
            logger.error(f"[DualSub] 处理入库事件异常: {e}\n{traceback.format_exc()}")

    # ---------------- 任务队列 ----------------
    def start_consumer(self):
        """启动消费线程(幂等)"""
        import queue
        if self._task_queue is None:
            self._task_queue = queue.Queue()
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._stop_event.clear()
        self._running = True
        self._consumer_thread = threading.Thread(
            target=self._consume_tasks, daemon=True, name="dualsub-consumer")
        self._consumer_thread.start()
        logger.info("[DualSub] 消费线程已启动")

    def _consume_tasks(self):
        """消费任务队列"""
        while not self._stop_event.is_set():
            try:
                task = self._task_queue.get(timeout=2)
            except Exception:
                # queue.Empty
                continue
            if task is None:
                continue
            try:
                self._mark_status(task["task_id"], TaskStatus.IN_PROGRESS.value)
                status, message = self.process_video(task["video_file"])
                self._mark_status(task["task_id"], status, message=message)
                logger.info(f"[DualSub] [{task['task_id'][:8]}] {Path(task['video_file']).name} -> {status}: {message}")
                if self._send_notify:
                    self._notify(task["video_file"], status, message)
            except Exception as e:
                logger.error(f"[DualSub] 消费任务异常: {e}\n{traceback.format_exc()}")
                self._mark_status(task["task_id"], TaskStatus.FAILED.value, message=str(e))
            finally:
                try:
                    self._task_queue.task_done()
                except Exception:
                    pass
        logger.info("[DualSub] 消费线程已退出")

    def add_task(self, video_file: str, source: str) -> bool:
        """添加任务到队列。已存在(队列中/历史已完成)则跳过"""
        if self._task_queue is None:
            import queue
            self._task_queue = queue.Queue()
        # 去重: 队列中
        with self._task_queue.mutex:
            for t in self._task_queue.queue:
                if t["video_file"] == video_file:
                    return False
        # 去重: 历史已完成 (避免重复处理)
        if self._tasks:
            for t in self._tasks.values():
                if t.get("video_file") == video_file and t.get("status") in (
                        TaskStatus.COMPLETED.value, TaskStatus.IN_PROGRESS.value, TaskStatus.PENDING.value):
                    logger.info(f"[DualSub] 任务已存在, 跳过: {video_file}")
                    return False
        import uuid
        task_id = str(uuid.uuid4())
        task = {
            "task_id": task_id,
            "video_file": video_file,
            "source": source,
            "add_time": datetime.now().isoformat(),
            "status": TaskStatus.PENDING.value,
            "complete_time": None,
            "message": "",
        }
        self._task_queue.put(task)
        if self._tasks is None:
            self._tasks = {}
        self._tasks[task_id] = task
        self.save_tasks()
        logger.info(f"[DualSub] 加入任务队列: {video_file}")
        return True

    def _mark_status(self, task_id: str, status: str, message: str = ""):
        if not self._tasks or task_id not in self._tasks:
            return
        self._tasks[task_id]["status"] = status
        self._tasks[task_id]["complete_time"] = (
            datetime.now().isoformat() if status in (
                TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.IGNORED.value) else None)
        if message:
            self._tasks[task_id]["message"] = message
        self.save_tasks()

    def _notify(self, video_file: str, status: str, message: str):
        name = os.path.basename(video_file)
        status_text = {
            TaskStatus.COMPLETED.value: "✅ 完成",
            TaskStatus.FAILED.value: "❌ 失败",
            TaskStatus.IGNORED.value: "⏭ 跳过",
        }.get(status, status)
        try:
            self.post_message(mtype=NotificationType.Plugin,
                              title="【双语字幕合并】",
                              text=f" 媒体: {name}\n 状态: {status_text}\n {message}")
        except Exception as e:
            logger.warn(f"[DualSub] 发送通知失败: {e}")

    # ---------------- 手动执行 ----------------
    def run_manual(self):
        """手动执行一次: 扫描配置的目录, 加入任务队列"""
        if not self._path_list:
            logger.warn("[DualSub] 手动执行但未配置路径")
            return
        if self._task_queue is None:
            import queue
            self._task_queue = queue.Queue()
        self.start_consumer()
        paths = [p.strip() for p in self._path_list.split("\n") if p.strip()]
        added = 0
        for path in paths:
            if not os.path.isabs(path):
                logger.warn(f"[DualSub] 路径非绝对路径, 跳过: {path}")
                continue
            if not os.path.exists(path):
                logger.warn(f"[DualSub] 路径不存在: {path}")
                continue
            if os.path.isdir(path):
                for vf in self._scan_dir(path):
                    if self.add_task(vf, TaskSource.MANUAL.value):
                        added += 1
            elif os.path.splitext(path)[-1].lower() in VIDEO_EXTS:
                if self.add_task(path, TaskSource.MANUAL.value):
                    added += 1
        logger.info(f"[DualSub] 手动执行: 扫描完成, 新增 {added} 个任务")

    def _scan_dir(self, path: str) -> List[str]:
        """扫描目录下的视频文件"""
        result = []
        try:
            root = Path(path)
            if self._scan_subdirs:
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                        result.append(str(p))
            else:
                for p in sorted(root.iterdir()):
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                        result.append(str(p))
        except Exception as e:
            logger.error(f"[DualSub] 扫描目录异常 {path}: {e}")
        return result

    # ---------------- 浏览页: 列目录 + 字幕轨探测 ----------------
    def _safe_path(self, path: str) -> Optional[str]:
        """校验并规范化浏览路径, 必须在 browse_root 之下, 禁止 .. 穿越"""
        try:
            root = Path(self._browse_root).resolve()
            target = Path(path).resolve()
            # 允许等于根目录或在根目录之下
            if target != root:
                if not str(target).startswith(str(root) + os.sep):
                    logger.warn(f"[DualSub] 路径越界, 拒绝: {path} (root={root})")
                    return None
            return str(target)
        except Exception as e:
            logger.error(f"[DualSub] 路径校验异常 {path}: {e}")
            return None

    def _list_browse(self, path: str) -> dict:
        """列出目录一层内容(目录 + 视频文件), 并对视频探测字幕轨状态.
        返回 {'path', 'parent', 'dirs', 'videos', 'error'}
        """
        result = {"path": path, "parent": None, "dirs": [], "videos": [], "error": None}
        safe = self._safe_path(path)
        if not safe:
            result["error"] = "路径无效或越界, 请检查浏览根目录配置"
            return result
        try:
            root = Path(safe)
            if not root.is_dir():
                result["error"] = f"不是目录: {safe}"
                return result
            # 父目录(不超过 browse_root)
            root_path = Path(self._browse_root).resolve()
            if root != root_path:
                result["parent"] = str(root.parent)
            entries = []
            try:
                entries = list(root.iterdir())
            except Exception as e:
                result["error"] = f"读取目录失败: {e}"
                return result
            entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
            video_count = 0
            for p in entries:
                try:
                    name = p.name
                    if name.startswith("."):
                        continue
                    if p.is_dir():
                        result["dirs"].append({"name": name, "path": str(p)})
                    elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                        video_count += 1
                        if video_count > 300:
                            continue
                        item = self._probe_video_item(p)
                        result["videos"].append(item)
                except Exception:
                    continue
        except Exception as e:
            result["error"] = f"浏览异常: {e}"
            logger.error(f"[DualSub] 浏览目录异常 {path}: {e}")
        return result

    def _probe_video_item(self, p: Path) -> dict:
        """探测单个视频文件: 大小/字幕轨/已生成状态"""
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        zh, en = self.probe_zh_en_tracks(str(p))
        has_dual = p.with_name(p.stem + ".dual.srt").exists() or \
            p.with_name(p.stem + ".dual.mkv").exists()
        status = "ready"
        if has_dual:
            status = "done"
        elif zh is None or en is None:
            status = "missing"
        return {
            "name": p.name,
            "path": str(p),
            "size": size,
            "size_text": self._fmt_size(size),
            "zh": zh is not None,
            "en": en is not None,
            "has_dual": has_dual,
            "status": status,
        }

    @staticmethod
    def _fmt_size(size_bytes: int) -> str:
        try:
            size = float(size_bytes)
        except (TypeError, ValueError):
            return "0 B"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{int(size_bytes)} B"

    # ---------------- 历史持久化 ----------------
    def load_tasks(self) -> Dict[str, dict]:
        try:
            return self.get_data("tasks") or {}
        except Exception:
            return {}

    def save_tasks(self):
        try:
            self.save_data("tasks", self._tasks or {})
        except Exception as e:
            logger.error(f"[DualSub] 保存任务失败: {e}")

    def clear_tasks(self):
        self._tasks = {}
        self.save_tasks()
        logger.info("[DualSub] 历史任务已清除")

    # ---------------- 插件接口 ----------------
    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件交互 API (前端 get_page 的 events.click.api 调用, auth=bear 用浏览器 token)"""
        return [
            {
                "path": "/browse",
                "endpoint": self.api_browse,
                "methods": ["GET"],
                "summary": "浏览目录",
                "auth": "bear",
                "description": "列出目录内容并探测视频字幕轨, path=目录绝对路径",
            },
            {
                "path": "/process",
                "endpoint": self.api_process,
                "methods": ["GET"],
                "summary": "处理单个视频",
                "auth": "bear",
                "description": "把单个视频加入处理队列, path=视频文件绝对路径",
            },
            {
                "path": "/process_dir",
                "endpoint": self.api_process_dir,
                "methods": ["GET"],
                "summary": "批量处理目录",
                "auth": "bear",
                "description": "扫描目录下所有视频加入队列, path=目录绝对路径",
            },
            {
                "path": "/set_tab",
                "endpoint": self.api_set_tab,
                "methods": ["GET"],
                "summary": "切换页面标签",
                "auth": "bear",
                "description": "切换 get_page 的 Tab, tab=browse|history",
            },
        ]

    def api_browse(self, path: str = ""):
        """前端点'进入目录'按钮调用: 更新当前浏览路径, 前端 onAction 自动重载 get_page"""
        try:
            target = unquote(path or "").strip() or self._browse_root
            self._browse_path = target
            self.update_config({**self._build_config(), "browse_path": target})
            return {"success": True, "path": target}
        except Exception as e:
            logger.error(f"[DualSub] api_browse 异常: {e}")
            return {"success": False, "message": str(e)}

    def api_process(self, path: str = ""):
        """前端点'处理'按钮调用: 把单个视频加入队列"""
        try:
            video_path = unquote(path or "").strip()
            if not video_path:
                return {"success": False, "message": "缺少 path 参数"}
            if Path(video_path).suffix.lower() not in VIDEO_EXTS:
                return {"success": False, "message": "非视频文件"}
            if not os.path.isfile(video_path):
                return {"success": False, "message": "文件不存在"}
            self.start_consumer()
            added = self.add_task(video_path, TaskSource.MANUAL.value)
            return {
                "success": True,
                "added": added,
                "message": "已加入队列" if added else "任务已存在或已处理",
            }
        except Exception as e:
            logger.error(f"[DualSub] api_process 异常: {e}")
            return {"success": False, "message": str(e)}

    def api_process_dir(self, path: str = ""):
        """前端点'批量处理目录'按钮调用: 扫描目录下所有视频入队"""
        try:
            dir_path = unquote(path or "").strip() or self._browse_path
            if not os.path.isdir(dir_path):
                return {"success": False, "message": "目录不存在"}
            self.start_consumer()
            files = self._scan_dir(dir_path)
            added = 0
            for vf in files:
                if self.add_task(vf, TaskSource.MANUAL.value):
                    added += 1
            return {
                "success": True,
                "added": added,
                "total": len(files),
                "message": f"新增 {added} 个任务, 目录共 {len(files)} 个视频",
            }
        except Exception as e:
            logger.error(f"[DualSub] api_process_dir 异常: {e}")
            return {"success": False, "message": str(e)}

    def api_set_tab(self, tab: str = "browse"):
        """切换 get_page 的 Tab"""
        try:
            tab = (tab or "browse").strip()
            if tab not in ("browse", "history"):
                tab = "browse"
            self._page_tab = tab
            self.update_config({**self._build_config(), "page_tab": tab})
            return {"success": True, "tab": tab}
        except Exception as e:
            logger.error(f"[DualSub] api_set_tab 异常: {e}")
            return {"success": False, "message": str(e)}

    def _build_config(self) -> dict:
        """构造当前配置字典(供 update_config 使用)"""
        return {
            "enabled": self._enabled,
            "listen_transfer_event": self._listen_transfer_event,
            "send_notify": self._send_notify,
            "run_now": False,
            "path_list": self._path_list,
            "mode": self._mode,
            "order": self._order,
            "backup": self._backup,
            "overwrite": self._overwrite,
            "min_file_size": self._min_file_size,
            "skip_if_exists": self._skip_if_exists,
            "scan_subdirs": self._scan_subdirs,
            "clear_history": False,
            "browse_root": self._browse_root,
            "browse_path": self._browse_path,
            "page_tab": self._page_tab,
        }


    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    # 第一行: 开关
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                            'color': 'primary'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'listen_transfer_event',
                                            'label': '入库自动执行',
                                            'hint': '监听媒体整理完成事件, 自动为新入库影片生成双语字幕'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'send_notify',
                                            'label': '发送通知'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 第二行: 手动执行
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'run_now',
                                            'label': '手动执行一次',
                                            'color': 'secondary',
                                            'hint': '勾选后保存即开始扫描下方路径'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'scan_subdirs',
                                            'label': '递归子目录',
                                            'hint': '手动执行时是否扫描子目录中的视频'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_history',
                                            'label': '清理历史记录',
                                            'color': 'error'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 浏览根目录
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'browse_root',
                                            'label': '浏览根目录',
                                            'placeholder': '容器内绝对路径, 如 /vol2/1000',
                                            'hint': '插件详情页「媒体浏览」Tab 的起始目录, 浏览范围限定在此根目录之下'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'density': 'compact',
                                            'text': '在「插件详情」页面可浏览媒体库并勾选已有视频生成双语字幕。点击目录进入下级, 点击「处理」按钮处理单个视频, 或点「处理当前目录」批量处理。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 手动执行路径
                    {
                        'component': 'VRow',
                        'props': {'v-show': 'run_now'},
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_list',
                                            'label': '媒体路径(手动执行)',
                                            'rows': 3,
                                            'placeholder': '容器内绝对路径, 每行一个。支持文件和文件夹, 如 /vol2/1000/Emby入库'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 处理模式与顺序
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '处理模式',
                                            'items': [
                                                {'title': '仅生成 .dual.srt (推荐)', 'value': 'srt'},
                                                {'title': '仅封回 MKV', 'value': 'mux'},
                                                {'title': '生成 srt + 封回 MKV', 'value': 'both'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'order',
                                            'label': '双语顺序',
                                            'items': [
                                                {'title': '英文在上 / 中文在下', 'value': 'en_first'},
                                                {'title': '中文在上 / 英文在下', 'value': 'zh_first'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_file_size',
                                            'label': '最小文件大小(MB)',
                                            'placeholder': '0=不限, 小于此值的视频跳过'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 封回选项
                    {
                        'component': 'VRow',
                        'props': {'v-show': "mode === 'mux' || mode === 'both'"},
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'overwrite',
                                            'label': '覆盖原文件',
                                            'hint': '关闭则生成 .dual.mkv 新文件, 保留原文件'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'backup',
                                            'label': '覆盖时备份原文件(.bak)',
                                            'hint': '仅覆盖模式生效'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'skip_if_exists',
                                            'label': '已有双语字幕则跳过'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 说明
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '插件依赖容器内的 ffmpeg/ffprobe。路径请填容器内绝对路径(如 /vol2/1000/Emby入库)。生成 .dual.srt 不修改原视频; 封回 MKV 时视频/音频流为 copy 不重编码。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "listen_transfer_event": True,
            "send_notify": False,
            "run_now": False,
            "path_list": "",
            "mode": "srt",
            "order": "en_first",
            "backup": True,
            "overwrite": False,
            "min_file_size": 0,
            "skip_if_exists": True,
            "scan_subdirs": True,
            "clear_history": False,
            "browse_root": "/vol2/1000",
            "browse_path": "",
            "page_tab": "browse",
        }

    def get_page(self) -> List[dict]:
        """插件详情页: 浏览媒体库 + 处理历史(双 Tab)"""
        tab = self._page_tab or "browse"
        browse_page = self._build_browse_tab()
        history_page = self._build_history_tab()
        return [
            {
                "component": "div",
                "props": {"class": "mb-3"},
                "content": [
                    {
                        "component": "VBtnToggle",
                        "props": {
                            "modelValue": tab,
                            "color": "primary",
                            "group": True,
                            "variant": "outlined",
                            "divided": True,
                        },
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "value": "browse",
                                    "size": "small",
                                    "variant": "text",
                                    "prepend-icon": "mdi-folder-search-outline",
                                },
                                "text": "媒体浏览",
                                "events": {
                                    "click": {
                                        "api": "plugin/DualSub/set_tab?tab=browse",
                                        "method": "get",
                                    }
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "value": "history",
                                    "size": "small",
                                    "variant": "text",
                                    "prepend-icon": "mdi-history",
                                },
                                "text": "处理历史",
                                "events": {
                                    "click": {
                                        "api": "plugin/DualSub/set_tab?tab=history",
                                        "method": "get",
                                    }
                                },
                            },
                        ],
                    }
                ],
            },
            browse_page if tab == "browse" else {"component": "div", "content": []},
            history_page if tab == "history" else {"component": "div", "content": []},
        ]

    def _build_browse_tab(self) -> dict:
        """构建媒体浏览 Tab"""
        browse = self._list_browse(self._browse_path or self._browse_root)
        if browse.get("error"):
            return {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": f"浏览失败: {browse['error']}。请在插件配置里设置正确的「浏览根目录」(容器内绝对路径)",
                },
            }
        current = browse.get("path", self._browse_path)
        parent = browse.get("parent")
        dirs = browse.get("dirs", [])
        videos = browse.get("videos", [])

        # 面包屑/路径条 + 操作按钮
        header_content = []
        header_content.append({
            "component": "VCol",
            "props": {"cols": 12, "md": 7, "class": "d-flex align-center"},
            "content": [
                {
                    "component": "VIcon",
                    "props": {"icon": "mdi-folder-outline", "color": "amber-darken-2", "class": "me-2"},
                },
                {
                    "component": "span",
                    "props": {"class": "text-body-1 text-truncate", "style": "max-width: 100%"},
                    "text": current,
                },
            ],
        })
        header_buttons = []
        if parent:
            header_buttons.append({
                "component": "VBtn",
                "props": {
                    "color": "secondary", "variant": "tonal", "size": "small",
                    "prepend-icon": "mdi-arrow-up", "class": "me-2",
                },
                "text": "上级",
                "events": {
                    "click": {"api": f"plugin/DualSub/browse?path={quote(parent)}", "method": "get"}
                },
            })
        if videos:
            header_buttons.append({
                "component": "VBtn",
                "props": {
                    "color": "primary", "variant": "flat", "size": "small",
                    "prepend-icon": "mdi-playlist-play", "class": "me-2",
                },
                "text": f"处理当前目录({len(videos)})",
                "events": {
                    "click": {"api": f"plugin/DualSub/process_dir?path={quote(current)}", "method": "get"}
                },
            })
        if header_buttons:
            header_content.append({
                "component": "VCol",
                "props": {"cols": 12, "md": 5, "class": "d-flex align-center justify-end flex-wrap"},
                "content": header_buttons,
            })

        rows = []
        # 目录行
        for d in dirs:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "ps-4"},
                     "content": [{"component": "VIcon", "props": {"icon": "mdi-folder", "color": "amber-darken-2"}}]},
                    {"component": "td", "props": {"class": "text-start"},
                     "content": [{"component": "span", "props": {"class": "font-weight-medium"}, "text": d["name"]}]},
                    {"component": "td", "props": {"class": "text-center text-muted"}, "text": "—"},
                    {"component": "td", "props": {"class": "text-center text-muted"}, "text": "—"},
                    {"component": "td", "props": {"class": "text-center text-muted"}, "text": "—"},
                    {"component": "td", "props": {"class": "text-end pe-4"},
                     "content": [{
                         "component": "VBtn",
                         "props": {"color": "info", "variant": "text", "size": "small",
                                   "prepend-icon": "mdi-folder-open-outline"},
                         "text": "进入",
                         "events": {
                             "click": {"api": f"plugin/DualSub/browse?path={quote(d['path'])}", "method": "get"}
                         },
                     }]},
                ],
            })
        # 视频行
        for v in videos:
            zh_tag = "🀄中" if v["zh"] else "—"
            en_tag = "EN英" if v["en"] else "—"
            if v["status"] == "done":
                state = {"text": "✅ 已生成", "color": "success"}
                action_btn = {
                    "component": "VBtn",
                    "props": {"variant": "text", "size": "small", "disabled": True,
                              "prepend-icon": "mdi-check-circle-outline", "color": "success"},
                    "text": "已完成",
                }
            elif v["status"] == "missing":
                state = {"text": "缺中/英轨", "color": "error"}
                action_btn = {
                    "component": "VBtn",
                    "props": {"variant": "text", "size": "small", "disabled": True,
                              "prepend-icon": "mdi-alert-circle-outline", "color": "grey"},
                    "text": "跳过",
                }
            else:
                state = {"text": "可处理", "color": "primary"}
                action_btn = {
                    "component": "VBtn",
                    "props": {"color": "primary", "variant": "flat", "size": "small",
                              "prepend-icon": "mdi-subtitles-outline"},
                    "text": "处理",
                    "events": {
                        "click": {"api": f"plugin/DualSub/process?path={quote(v['path'])}", "method": "get"}
                    },
                }
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "ps-4"},
                     "content": [{"component": "VIcon", "props": {"icon": "mdi-file-video-outline", "color": "indigo"}}]},
                    {"component": "td", "props": {"class": "text-start"},
                     "content": [
                         {"component": "div", "props": {"class": "text-body-2 text-truncate", "style": "max-width: 360px"}, "text": v["name"]},
                         {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": v["size_text"]},
                     ]},
                    {"component": "td", "props": {"class": "text-center"},
                     "content": [{"component": "span", "props": {"class": f"text-{state['color']}"}, "text": zh_tag}]},
                    {"component": "td", "props": {"class": "text-center"},
                     "content": [{"component": "span", "props": {"class": f"text-{state['color']}"}, "text": en_tag}]},
                    {"component": "td", "props": {"class": "text-center"},
                     "content": [{"component": "span", "props": {"class": f"font-weight-medium text-{state['color']}"}, "text": state["text"]}]},
                    {"component": "td", "props": {"class": "text-end pe-4"}, "content": [action_btn]},
                ],
            })
        if not dirs and not videos:
            rows.append({
                "component": "tr",
                "content": [{"component": "td", "props": {"class": "text-center text-muted pa-4", "colspan": 6}, "text": "此目录为空"}],
            })

        return {
            "component": "div",
            "content": [
                {
                    "component": "VRow",
                    "props": {"class": "mb-2", "align": "center"},
                    "content": header_content,
                },
                {
                    "component": "VTable",
                    "props": {"hover": True, "density": "compact"},
                    "content": [
                        {
                            "component": "thead",
                            "content": [
                                {"component": "th", "props": {"class": "ps-4", "style": "width: 40px"}},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "名称"},
                                {"component": "th", "props": {"class": "text-center"}, "text": "中轨"},
                                {"component": "th", "props": {"class": "text-center"}, "text": "英轨"},
                                {"component": "th", "props": {"class": "text-center"}, "text": "状态"},
                                {"component": "th", "props": {"class": "text-end pe-4"}, "text": "操作"},
                            ],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                },
            ],
        }

    def _build_history_tab(self) -> dict:
        """构建处理历史 Tab (保留原任务表格)"""
        tasks: Dict[str, dict] = self._tasks or {}
        sorted_tasks = sorted(
            tasks.items(),
            key=lambda x: x[1].get("add_time", ""),
            reverse=True
        )
        sorted_tasks = sorted_tasks[:200]

        status_class = {
            TaskStatus.PENDING.value: "text-info",
            TaskStatus.IN_PROGRESS.value: "text-warning",
            TaskStatus.COMPLETED.value: "text-success",
            TaskStatus.IGNORED.value: "text-muted",
            TaskStatus.FAILED.value: "text-error",
        }
        status_text = {
            TaskStatus.PENDING.value: "等待中",
            TaskStatus.IN_PROGRESS.value: "处理中",
            TaskStatus.COMPLETED.value: "已完成",
            TaskStatus.IGNORED.value: "已忽略",
            TaskStatus.FAILED.value: "失败",
        }
        source_text = {
            TaskSource.MANUAL.value: "手动",
            TaskSource.EVENT.value: "入库",
        }

        rows = []
        for task_id, task in sorted_tasks:
            try:
                add_time = datetime.fromisoformat(task["add_time"]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                add_time = task.get("add_time", "-")
            try:
                complete_time = (datetime.fromisoformat(task["complete_time"]).strftime("%Y-%m-%d %H:%M:%S")
                                 if task.get("complete_time") else "-")
            except Exception:
                complete_time = "-"
            st = task.get("status", "")
            rows.append({
                "component": "tr",
                "props": {"class": "text-sm"},
                "content": [
                    {"component": "td", "text": add_time},
                    {"component": "td", "props": {"class": "text-break"},
                     "text": os.path.basename(task.get("video_file", ""))},
                    {"component": "td", "text": source_text.get(task.get("source", ""), task.get("source", ""))},
                    {"component": "td", "props": {"class": status_class.get(st, "")},
                     "text": status_text.get(st, st)},
                    {"component": "td", "props": {"class": "text-break text-muted"},
                     "text": task.get("message", "")},
                    {"component": "td", "text": complete_time},
                ],
            })
        if not rows:
            rows.append({
                "component": "tr",
                "content": [{"component": "td", "props": {"class": "text-center text-muted pa-4", "colspan": 6}, "text": "暂无处理记录"}],
            })

        return {
            "component": "div",
            "content": [
                {
                    "component": "VTable",
                    "props": {"hover": True, "density": "compact"},
                    "content": [
                        {
                            "component": "thead",
                            "content": [
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "添加时间"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "视频文件"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "来源"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "状态"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "说明"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "完成时间"},
                            ]
                        },
                        {"component": "tbody", "content": rows}
                    ]
                }
            ]
        }

    def stop_service(self):
        """停止插件服务"""
        self._stop_event.set()
        self._running = False
        if self._consumer_thread and self._consumer_thread.is_alive():
            logger.info("[DualSub] 正在停止消费线程...")
            self._consumer_thread.join(timeout=5)
        # 把待处理/处理中的任务标记为失败
        if self._tasks:
            changed = False
            for t in self._tasks.values():
                if t.get("status") in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value):
                    t["status"] = TaskStatus.FAILED.value
                    t["complete_time"] = datetime.now().isoformat()
                    t["message"] = t.get("message") or "插件停止, 任务中断"
                    changed = True
            if changed:
                self.save_tasks()
        logger.info("[DualSub] 双语字幕合并服务已停止")
