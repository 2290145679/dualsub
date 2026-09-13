#!/usr/bin/env python3
"""字幕生成 (SubGenerator) - MoviePilot V2 双语字幕自动合成插件。

核心功能：
1. 全自动：监听 MoviePilot TransferComplete 事件，新入库影片自动生成双语字幕。
2. 不破坏原视频：默认仅在同目录下生成 .zh-cn&en.default.ass 高清外挂字幕，零风险。
3. 可选项丰富：彩色 ASS 独立样式、可选 ffmpeg 无损封回 MKV、大模型 AI 缺失字幕自动补全。
4. 可视化看板：插件详情页内置媒体库浏览器，支持手动单片/批量生成、重写、任务追踪与日志。
"""

import json
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from app.core.config import settings
from app.core.event import eventmanager, Event as MPEvent
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .core.models import (
    VIDEO_EXTS,
    SubtitleItem,
    TrackInfo,
    TaskItem,
    TaskStatus,
    TaskSource,
    is_chinese_lang,
    is_english_lang,
    is_japanese_lang,
)
from .core.extractor import (
    find_external_subtitles,
    probe_embedded_subtitles,
    check_native_bilingual,
)
from .core.translator import AITranslator
from .core.processor import process_video_pipeline, has_existing_dual_subtitle
from .core.styler import normalize_ass_color

COLOR_PRESETS_ZH = [
    {"title": "⚪ 经典纯白 (#FFFFFF - 默认推荐)", "value": "#FFFFFF"},
    {"title": "🟡 电影胶片黄 (#FFFF00)", "value": "#FFFF00"},
    {"title": "✨ 暖金琥珀色 (#FFD700)", "value": "#FFD700"},
    {"title": "🌊 科技荧光青 (#00E5FF)", "value": "#00E5FF"},
    {"title": "🌿 柔和护眼绿 (#98FB98)", "value": "#98FB98"},
    {"title": "🌌 浅天蓝 (#87CEFA)", "value": "#87CEFA"},
    {"title": "🌸 暖粉珊瑚 (#FFA07A)", "value": "#FFA07A"},
    {"title": "🎨 自定义色号 (在右侧输入框填写)", "value": "custom"},
]

COLOR_PRESETS_EN = [
    {"title": "🟡 经典暖黄 (#FFE500 - 默认推荐)", "value": "#FFE500"},
    {"title": "🌊 科技荧光青 (#00E5FF)", "value": "#00E5FF"},
    {"title": "✨ 暖金琥珀色 (#FFD700)", "value": "#FFD700"},
    {"title": "⚪ 纯洁雪白 (#FFFFFF)", "value": "#FFFFFF"},
    {"title": "🌿 柔和护眼绿 (#98FB98)", "value": "#98FB98"},
    {"title": "🌌 浅天蓝 (#87CEFA)", "value": "#87CEFA"},
    {"title": "🌸 暖粉珊瑚 (#FFA07A)", "value": "#FFA07A"},
    {"title": "🎨 自定义色号 (在右侧输入框填写)", "value": "custom"},
]


def _detect_color_preset(color: str, presets: list) -> str:
    if not color:
        return presets[0]["value"]
    norm = normalize_ass_color(color).upper()
    for p in presets:
        if p["value"] == "custom":
            continue
        if normalize_ass_color(p["value"]).upper() == norm:
            return p["value"]
    return "custom"


class SubGenerator(_PluginBase):
    # 插件元信息
    plugin_name = "字幕生成"
    plugin_desc = "全自动为入库影片合成双语字幕(外挂ASS或封回MKV)，支持外挂与内嵌字幕智能嗅探、大模型缺失翻译补全、彩色排版定制与媒体库可视化管理。"
    plugin_icon = "subtitles.png"
    plugin_color = "#1E88E5"
    plugin_version = "2.0.0"
    plugin_author = "wuzhennana"
    author_url = "https://github.com/wuzhennana"
    plugin_config_prefix = "subgenerator"
    plugin_order = 15
    auth_level = 1

    # 配置项与内部状态
    _enabled: bool = False
    _listen_transfer_event: bool = True
    _send_notify: bool = False
    _transfer_paths: str = ""
    _mode: str = "srt"  # srt (外挂) | mux (封回) | both
    _subtitle_suffix: str = ".zh-cn&en.default.ass"
    _order: str = "zh_first"  # zh_first (中上英下) | en_first (英上中下)
    _backup: bool = True
    _overwrite: bool = False
    _min_file_size: int = 0
    _skip_if_exists: bool = True
    _browse_root: str = "/vol2/1000"
    _browse_path: str = ""
    _page_tab: str = "browse"

    # ASS 样式配置
    _font_chinese: str = "Microsoft YaHei"
    _font_english: str = "Arial"
    _chinese_color: str = "&H00FFFFFF"  # 白色
    _english_color: str = "&H0000E5FF"  # 暖金黄色
    _margin_v: int = 45
    _time_offset: float = 0.0  # 时间轴微调偏移(秒)，正数延后，负数提前

    # AI 翻译配置
    _ai_enabled: bool = False
    _ai_base_url: str = ""
    _ai_api_key: str = ""
    _ai_model: str = ""
    _ai_models: List[str] = []
    _ai_target_lang: str = "zh-CN"
    _ai_cache_enabled: bool = True
    _ai_mark_translated: bool = False
    _ai_translate_zh_to_en: bool = False
    _ai_cache: Dict[str, str] = {}
    _ai_connection_info: Dict[str, Any] = {}

    # 任务与并发
    _task_queue: Optional[queue.Queue] = None
    _consumer_thread: Optional[threading.Thread] = None
    _stop_event: Optional[threading.Event] = None
    _tasks: Dict[str, dict] = {}
    _file_locks: Dict[str, threading.Lock] = {}
    _file_locks_mutex: Optional[threading.Lock] = None

    def _init_runtime(self):
        if self._stop_event is None:
            self._stop_event = threading.Event()
        if self._file_locks_mutex is None:
            self._file_locks_mutex = threading.Lock()
        if self._task_queue is None:
            self._task_queue = queue.Queue()

    def init_plugin(self, config: dict = None):
        self._init_runtime()
        if not config:
            return

        self._enabled = bool(config.get("enabled", False))
        self._listen_transfer_event = bool(config.get("listen_transfer_event", True))
        self._send_notify = bool(config.get("send_notify", False))
        self._transfer_paths = config.get("transfer_paths", "") or ""
        self._mode = config.get("mode", "srt") or "srt"
        self._subtitle_suffix = config.get("subtitle_suffix", ".zh-cn&en.default.ass") or ".zh-cn&en.default.ass"
        self._order = config.get("order", "zh_first") or "zh_first"
        self._backup = bool(config.get("backup", True))
        self._overwrite = bool(config.get("overwrite", False))

        try:
            self._min_file_size = int(config.get("min_file_size", 0) or 0)
        except (TypeError, ValueError):
            self._min_file_size = 0

        self._skip_if_exists = bool(config.get("skip_if_exists", True))
        self._browse_root = config.get("browse_root", "") or "/vol2/1000"
        self._browse_path = config.get("browse_path", "") or self._browse_root
        self._page_tab = config.get("page_tab", "browse") or "browse"

        self._font_chinese = config.get("font_chinese", "Microsoft YaHei") or "Microsoft YaHei"
        self._font_english = config.get("font_english", "Arial") or "Arial"

        zh_preset = config.get("chinese_color_preset", "")
        zh_custom = (config.get("chinese_color", "") or "").strip()
        if zh_preset == "custom":
            self._chinese_color = normalize_ass_color(zh_custom, default="&H00FFFFFF")
        elif zh_custom and normalize_ass_color(zh_custom) != self._chinese_color and (not zh_preset or normalize_ass_color(zh_custom) != normalize_ass_color(zh_preset)):
            self._chinese_color = normalize_ass_color(zh_custom, default="&H00FFFFFF")
        elif zh_preset:
            self._chinese_color = normalize_ass_color(zh_preset, default="&H00FFFFFF")
        elif zh_custom:
            self._chinese_color = normalize_ass_color(zh_custom, default="&H00FFFFFF")
        else:
            self._chinese_color = "&H00FFFFFF"

        en_preset = config.get("english_color_preset", "")
        en_custom = (config.get("english_color", "") or "").strip()
        if en_preset == "custom":
            self._english_color = normalize_ass_color(en_custom, default="&H0000E5FF")
        elif en_custom and normalize_ass_color(en_custom) != self._english_color and (not en_preset or normalize_ass_color(en_custom) != normalize_ass_color(en_preset)):
            self._english_color = normalize_ass_color(en_custom, default="&H0000E5FF")
        elif en_preset:
            self._english_color = normalize_ass_color(en_preset, default="&H0000E5FF")
        elif en_custom:
            self._english_color = normalize_ass_color(en_custom, default="&H0000E5FF")
        else:
            self._english_color = "&H0000E5FF"

        try:
            self._margin_v = int(config.get("margin_v", 45) or 45)
        except (TypeError, ValueError):
            self._margin_v = 45

        try:
            self._time_offset = float(config.get("time_offset", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._time_offset = 0.0

        self._ai_enabled = bool(config.get("ai_enabled", False))
        self._ai_base_url = (config.get("ai_base_url", "") or "").strip().rstrip("/")
        self._ai_api_key = (config.get("ai_api_key", "") or "").strip()
        self._ai_model = config.get("ai_model", "") or self.get_data("ai_model") or ""
        self._ai_models = config.get("ai_models", []) or self.get_data("ai_models") or []
        self._ai_target_lang = config.get("ai_target_lang", "zh-CN") or "zh-CN"
        self._ai_cache_enabled = bool(config.get("ai_cache_enabled", True))
        self._ai_mark_translated = bool(config.get("ai_mark_translated", False))
        self._ai_translate_zh_to_en = bool(config.get("ai_translate_zh_to_en", False))

        # 加载持久化数据
        self._ai_cache = self.load_ai_cache()
        self._tasks = self.load_tasks()
        self._ai_connection_info = self.get_data("ai_connection_info") or {}

        # 启动消费队列线程
        if self._enabled:
            self.start_consumer()
        else:
            self.stop_consumer()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        self.stop_consumer()

    # ---------------- 队列与后台消费线程 ----------------
    def start_consumer(self):
        self._init_runtime()
        if self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._stop_event.clear()
        self._consumer_thread = threading.Thread(
            target=self._queue_worker, name="SubGeneratorWorker", daemon=True
        )
        self._consumer_thread.start()
        logger.info("[字幕生成] 后台任务队列消费线程已启动")

    def stop_consumer(self):
        if self._stop_event:
            self._stop_event.set()
        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=3)
        self._consumer_thread = None

    def _get_file_lock(self, path: str) -> threading.Lock:
        with self._file_locks_mutex:
            if path not in self._file_locks:
                self._file_locks[path] = threading.Lock()
            return self._file_locks[path]

    def add_task(
        self, video_path: str, source: str = "manual", time_offset: Optional[float] = None
    ) -> bool:
        """将视频路径加入任务队列。"""
        self._init_runtime()
        video_clean = str(Path(video_path).resolve())
        # 检查是否已有排队中或处理中的同一任务
        for tid, t in list(self._tasks.items()):
            if (
                t.get("video_file") == video_clean
                and t.get("status") in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value)
            ):
                return False

        task_id = f"task_{int(time.time() * 1000)}_{len(self._tasks) % 1000}"
        offset_val = float(time_offset) if time_offset is not None else self._time_offset
        task_info = {
            "task_id": task_id,
            "video_file": video_clean,
            "source": source,
            "time_offset": offset_val,
            "add_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": TaskStatus.PENDING.value,
            "complete_time": None,
            "message": "已排队等待处理",
            "logs": [],
        }
        self._tasks[task_id] = task_info
        self.save_tasks()
        self._task_queue.put(task_info)
        offset_hint = f" [时间轴偏移: {offset_val:+.2f}s]" if offset_val != 0.0 else ""
        logger.info(f"[字幕生成] 任务加入队列: {Path(video_clean).name} (来源: {source}{offset_hint})")
        return True

    def _queue_worker(self):
        while not self._stop_event.is_set():
            try:
                task_info = self._task_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            task_id = task_info["task_id"]
            video_file = task_info["video_file"]

            task_info["status"] = TaskStatus.IN_PROGRESS.value
            task_info["message"] = "正在处理中..."
            self.save_tasks()

            lock = self._get_file_lock(video_file)
            with lock:
                try:
                    # 组装配置字典
                    cfg = {
                        "mode": self._mode,
                        "subtitle_suffix": self._subtitle_suffix,
                        "order": self._order,
                        "backup": self._backup,
                        "overwrite": self._overwrite,
                        "min_file_size": self._min_file_size,
                        "skip_if_exists": self._skip_if_exists,
                        "time_offset": task_info.get("time_offset", self._time_offset),
                        "font_chinese": self._font_chinese,
                        "font_english": self._font_english,
                        "chinese_color": self._chinese_color,
                        "english_color": self._english_color,
                        "margin_v": self._margin_v,
                        "ai_enabled": self._ai_enabled,
                        "ai_translate_zh_to_en": self._ai_translate_zh_to_en,
                        "allow_single_zh": True,
                        "force": (task_info.get("source") == "manual_regen"),
                        "source": task_info.get("source", ""),
                    }

                    ai_trans = None
                    if self._ai_enabled and self._ai_base_url and self._ai_api_key:
                        ai_trans = AITranslator(
                            base_url=self._ai_base_url,
                            api_key=self._ai_api_key,
                            model=self._ai_model,
                            target_lang=self._ai_target_lang,
                            cache_enabled=self._ai_cache_enabled,
                            mark_ai=self._ai_mark_translated,
                        )

                    status, msg, logs, detail = process_video_pipeline(
                        video_file, cfg, ai_trans, self._ai_cache
                    )

                    task_info["status"] = status
                    task_info["message"] = msg
                    task_info["logs"] = logs
                    task_info["detail"] = detail
                    task_info["complete_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._tasks[task_id] = task_info

                    # 保存缓存与任务
                    if self._ai_cache_enabled:
                        self.save_ai_cache()
                    self.save_tasks()

                    # 发送通知
                    if self._send_notify:
                        if status == TaskStatus.COMPLETED.value:
                            ai_status_str = detail.get("ai_status") or "未调用"
                            zh_s = detail.get("zh_source") or "无"
                            en_s = detail.get("en_source") or "无"
                            total_cnt = detail.get("total", 0)
                            paired_cnt = detail.get("paired", 0)
                            pri_only = detail.get("primary_only", 0)
                            sec_only = detail.get("secondary_only", 0)

                            if pri_only or sec_only:
                                align_desc = f"共 {total_cnt} 句 (双语对齐 {paired_cnt} 句, 仅中 {pri_only} 句, 仅外 {sec_only} 句)"
                            else:
                                align_desc = f"共 {total_cnt} 句 (100% 双语对齐)"

                            notify_lines = [
                                f"🎬 影片：{Path(video_file).name}",
                                f"🤖 AI补全：{ai_status_str}",
                                f"📝 字幕源：{zh_s} + {en_s}",
                                f"📊 合成结果：{align_desc}",
                            ]
                            sub_file = detail.get("sub_file")
                            if sub_file:
                                notify_lines.append(f"📁 字幕文件：{sub_file}")

                            self.post_message(
                                mtype=NotificationType.Manual,
                                title="双语字幕生成完成",
                                text="\n".join(notify_lines),
                            )
                        elif "自带双语字幕" in msg:
                            self.post_message(
                                mtype=NotificationType.Manual,
                                title="片源自带双语字幕 (未重新生成)",
                                text=f"🎬 影片：{Path(video_file).name}\nℹ️ 说明：{msg}",
                            )
                        elif status == TaskStatus.FAILED.value:
                            self.post_message(
                                mtype=NotificationType.Manual,
                                title="双语字幕生成失败",
                                text=f"🎬 影片：{Path(video_file).name}\n❌ 原因：{msg}",
                            )

                except Exception as e:
                    err_detail = traceback.format_exc()
                    logger.error(f"[字幕生成] 处理异常 {video_file}: {e}\n{err_detail}")
                    task_info["status"] = TaskStatus.FAILED.value
                    task_info["message"] = f"处理失败: {str(e)[:150]}"
                    task_info["logs"] = [str(e), err_detail[-300:]]
                    task_info["complete_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._tasks[task_id] = task_info
                    self.save_tasks()

            self._task_queue.task_done()

    # ---------------- 事件监听 ----------------
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: MPEvent):
        """监听入库整理完成事件，全自动触发生成。"""
        if not self._enabled or not self._listen_transfer_event:
            return

        event_data = event.event_data or {}
        dest_file = (
            event_data.get("dest_file")
            or event_data.get("file_path")
            or event_data.get("target_path")
            or event_data.get("item_path")
            or ""
        )
        if not dest_file:
            return

        dest_path = Path(dest_file)
        if dest_path.is_dir():
            # 目录下递归发现视频
            for f in dest_path.rglob("*"):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    self._check_and_add_event_task(str(f))
        elif dest_path.is_file() and dest_path.suffix.lower() in VIDEO_EXTS:
            self._check_and_add_event_task(str(dest_path))

    def _check_and_add_event_task(self, video_path: str):
        # 检查白名单目录
        if self._transfer_paths:
            allowed_prefixes = [
                p.strip() for p in self._transfer_paths.splitlines() if p.strip()
            ]
            if allowed_prefixes and not any(
                video_path.startswith(prefix) for prefix in allowed_prefixes
            ):
                return
        self.add_task(video_path, source=TaskSource.EVENT.value)

    # ---------------- 持久化读写 ----------------
    def load_tasks(self) -> Dict[str, dict]:
        try:
            return self.get_data("tasks") or {}
        except Exception:
            return {}

    def save_tasks(self):
        try:
            # 只保留最新 200 条任务
            if len(self._tasks) > 200:
                sorted_keys = sorted(
                    self._tasks.keys(),
                    key=lambda k: self._tasks[k].get("add_time", ""),
                    reverse=True,
                )
                self._tasks = {k: self._tasks[k] for k in sorted_keys[:200]}
            self.save_data("tasks", self._tasks)
        except Exception as e:
            logger.error(f"[字幕生成] 保存任务历史失败: {e}")

    def load_ai_cache(self) -> Dict[str, str]:
        try:
            return self.get_data("ai_cache") or {}
        except Exception:
            return {}

    def save_ai_cache(self):
        try:
            self.save_data("ai_cache", self._ai_cache)
        except Exception as e:
            logger.error(f"[字幕生成] 保存翻译缓存失败: {e}")

    # ---------------- 插件 API ----------------
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/browse",
                "endpoint": self.api_browse,
                "methods": ["GET"],
                "summary": "浏览目录",
                "auth": "bear",
                "description": "列出指定目录结构并嗅探视频与字幕轨状态",
            },
            {
                "path": "/process",
                "endpoint": self.api_process,
                "methods": ["GET"],
                "summary": "处理单视频",
                "auth": "bear",
                "description": "将指定视频加入生成队列",
            },
            {
                "path": "/process_dir",
                "endpoint": self.api_process_dir,
                "methods": ["GET"],
                "summary": "批量处理目录",
                "auth": "bear",
                "description": "将目录下所有视频批量加入生成队列",
            },
            {
                "path": "/regenerate",
                "endpoint": self.api_regenerate,
                "methods": ["GET"],
                "summary": "重新生成字幕",
                "auth": "bear",
                "description": "删除旧字幕产物并强制加入队列",
            },
            {
                "path": "/cancel",
                "endpoint": self.api_cancel,
                "methods": ["GET"],
                "summary": "取消任务",
                "auth": "bear",
                "description": "取消正在排队的任务",
            },
            {
                "path": "/set_tab",
                "endpoint": self.api_set_tab,
                "methods": ["GET"],
                "summary": "切换Tab",
                "auth": "bear",
                "description": "切换页面显示Tab",
            },
            {
                "path": "/ai_models",
                "endpoint": self.api_ai_models,
                "methods": ["GET"],
                "summary": "获取模型列表",
                "auth": "bear",
                "description": "从 AI 接口拉取可用大模型列表",
            },
            {
                "path": "/ai_check",
                "endpoint": self.api_ai_check,
                "methods": ["GET"],
                "summary": "测试连接",
                "auth": "bear",
                "description": "测试大模型接口连通性与可用性",
            },
            {
                "path": "/select_model",
                "endpoint": self.api_select_model,
                "methods": ["GET"],
                "summary": "切换生效模型节点",
                "auth": "bear",
                "description": "选择并切换当前生效的 AI 大模型节点",
            },
            {
                "path": "/ai_test_translate",
                "endpoint": self.api_ai_test_translate,
                "methods": ["GET"],
                "summary": "单句翻译实测",
                "auth": "bear",
                "description": "使用当前模型节点测试单句实际翻译效果",
            },
            {
                "path": "/clear_cache",
                "endpoint": self.api_clear_cache,
                "methods": ["GET"],
                "summary": "清理翻译缓存",
                "auth": "bear",
                "description": "清空已保存的 AI 翻译文本缓存",
            },
            {
                "path": "/clear_tasks",
                "endpoint": self.api_clear_tasks,
                "methods": ["GET"],
                "summary": "清理任务历史",
                "auth": "bear",
                "description": "清空已完成或失败的历史任务列表",
            },
        ]

    def api_browse(self, path: str = ""):
        target = unquote(path or "").strip() or self._browse_root
        self._browse_path = target
        return self._list_browse(target)

    def api_set_tab(self, tab: str = "browse"):
        self._page_tab = tab
        return {"success": True, "tab": tab}

    def api_process(self, path: str = "", offset: float = 0.0):
        video_path = unquote(path or "").strip()
        if not video_path:
            return {"success": False, "message": "缺少视频路径"}
        try:
            offset_val = float(offset)
        except (ValueError, TypeError):
            offset_val = 0.0
        added = self.add_task(
            video_path,
            source="manual",
            time_offset=offset_val if offset_val != 0.0 else None,
        )
        if added:
            hint = f" (时间轴偏移: {offset_val:+.2f}s)" if offset_val != 0.0 else ""
            return {"success": True, "message": f"已将 {Path(video_path).name} 加入生成队列{hint}"}
        return {"success": False, "message": "任务已在队列或正在处理中"}

    def api_process_dir(self, path: str = ""):
        dir_path = unquote(path or "").strip()
        if not dir_path or not Path(dir_path).exists():
            return {"success": False, "message": "目录不存在"}
        p = Path(dir_path)
        count = 0
        for f in p.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                if self.add_task(str(f), source="manual_batch"):
                    count += 1
        return {"success": True, "message": f"已将目录下 {count} 个视频加入处理队列"}

    def api_regenerate(self, path: str = "", offset: float = 0.0):
        video_path = unquote(path or "").strip()
        if not video_path:
            return {"success": False, "message": "缺少视频路径"}
        try:
            offset_val = float(offset)
        except (ValueError, TypeError):
            offset_val = 0.0
        v = Path(video_path)
        # 删除旧产物
        deleted = []
        for s in (self._subtitle_suffix, ".zh-cn&en.default.ass", ".zh-CN.ass", ".zh-CN.srt", ".dual.ass", ".dual.srt"):
            old = v.with_name(v.stem + s)
            if old.exists():
                try:
                    old.unlink()
                    deleted.append(old.name)
                except Exception:
                    pass

        # 清除历史并加回队列
        for tid in list(self._tasks.keys()):
            if self._tasks[tid].get("video_file") == video_path:
                self._tasks.pop(tid, None)
        self.save_tasks()

        added = self.add_task(
            video_path,
            source="manual_regen",
            time_offset=offset_val if offset_val != 0.0 else None,
        )
        hint = f" (时间轴偏移: {offset_val:+.2f}s)" if offset_val != 0.0 else ""
        return {"success": True, "message": f"已清理 {len(deleted)} 个旧字幕产物，重新加入队列{hint}"}

    def api_cancel(self, path: str = ""):
        video_path = unquote(path or "").strip()
        for tid, t in list(self._tasks.items()):
            if t.get("video_file") == video_path and t.get("status") == TaskStatus.PENDING.value:
                t["status"] = TaskStatus.IGNORED.value
                t["message"] = "用户手动取消排队"
                self.save_tasks()
                return {"success": True, "message": "已取消该任务排队"}
        return {"success": False, "message": "未找到处于排队状态的任务"}

    def api_ai_models(self):
        translator = AITranslator(base_url=self._ai_base_url, api_key=self._ai_api_key)
        ok, models, msg = translator.fetch_models()
        self._init_runtime()
        if ok:
            self._ai_models = models
            if not self._ai_model and models:
                self._ai_model = models[0]
            self.save_data("ai_models", models)
            self.save_data("ai_model", self._ai_model)
            self._ai_connection_info["status"] = "connected"
            self._ai_connection_info["message"] = msg
            self._ai_connection_info["last_check_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_data("ai_connection_info", self._ai_connection_info)
            self.update_config(self._build_config())
            return {"success": True, "models": models, "message": msg}
        else:
            self._ai_connection_info["status"] = "error"
            self._ai_connection_info["message"] = msg
            self._ai_connection_info["last_check_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_data("ai_connection_info", self._ai_connection_info)
            return {"success": False, "message": msg}

    def api_ai_check(self):
        translator = AITranslator(
            base_url=self._ai_base_url,
            api_key=self._ai_api_key,
            model=self._ai_model,
        )
        ok, info = translator.check_connection()
        self._init_runtime()
        self._ai_connection_info["status"] = "connected" if ok else "error"
        self._ai_connection_info["message"] = info.get("message", "")
        self._ai_connection_info["latency_ms"] = info.get("latency_ms", 0)
        self._ai_connection_info["last_check_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if info.get("models") and not self._ai_models:
            self._ai_models = info.get("models")
            self.save_data("ai_models", self._ai_models)
        self.save_data("ai_connection_info", self._ai_connection_info)
        return {
            "success": ok,
            "message": info.get("message"),
            "latency_ms": info.get("latency_ms"),
            "model": info.get("model", self._ai_model),
        }

    def api_select_model(self, model: str = ""):
        m = unquote(model or "").strip()
        if not m:
            return {"success": False, "message": "未提供模型名称"}
        self._ai_model = m
        self.save_data("ai_model", m)
        cfg = self._build_config()
        cfg["ai_model"] = m
        self.update_config(cfg)
        return {"success": True, "message": f"已将当前生效节点切换为: {m}"}

    def api_ai_test_translate(self):
        if not self._ai_model:
            return {"success": False, "message": "请先在节点列表中选择或输入当前模型，再进行翻译实测"}
        translator = AITranslator(
            base_url=self._ai_base_url,
            api_key=self._ai_api_key,
            model=self._ai_model,
            target_lang=self._ai_target_lang,
        )
        sample = "Subtitles generated successfully with bilingual style."
        ok, trans, latency = translator.test_translate(sample)
        if ok:
            res_info = {
                "input": sample,
                "output": trans,
                "latency_ms": latency,
                "model": self._ai_model,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._ai_connection_info["test_translate_result"] = res_info
            self.save_data("ai_connection_info", self._ai_connection_info)
            return {"success": True, "message": f"✅ 实测成功 ({latency}ms)！\n原文: {sample}\n译文: {trans}"}
        return {"success": False, "message": f"❌ 实测失败: {trans}"}

    def api_clear_cache(self):
        size = len(self._ai_cache)
        self._ai_cache = {}
        self.save_ai_cache()
        return {"success": True, "message": f"已清空本地翻译缓存（共 {size} 条）"}

    def api_clear_tasks(self):
        self._tasks = {
            k: v
            for k, v in self._tasks.items()
            if v.get("status") in (TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value)
        }
        self.save_tasks()
        return {"success": True, "message": "已清空所有已完成/失败的任务历史"}

    # ---------------- 媒体库嗅探辅助 ----------------
    def _list_browse(self, dir_path: str) -> dict:
        p = Path(dir_path)
        if not p.exists() or not p.is_dir():
            return {"error": f"目录不存在: {dir_path}"}

        parent = str(p.parent) if p.parent != p else None
        dirs = []
        videos = []

        try:
            for item in sorted(p.iterdir(), key=lambda x: x.name):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    dirs.append({"name": item.name, "path": str(item)})
                elif item.is_file() and item.suffix.lower() in VIDEO_EXTS:
                    size_mb = item.stat().st_size / (1024 * 1024)
                    size_text = f"{size_mb / 1024:.2f} GB" if size_mb >= 1024 else f"{size_mb:.0f} MB"

                    # 探测字幕状态
                    status = "ready"
                    status_badge = {"text": "待生成", "color": "primary"}
                    can_process = True

                    # 检查是否有任务在队列或执行
                    video_resolved = str(item.resolve())
                    for t in self._tasks.values():
                        if t.get("video_file") == video_resolved:
                            if t.get("status") == TaskStatus.IN_PROGRESS.value:
                                status = "processing"
                                status_badge = {"text": "处理中", "color": "warning"}
                                can_process = False
                                break
                            elif t.get("status") == TaskStatus.PENDING.value:
                                status = "queued"
                                status_badge = {"text": "排队中", "color": "info"}
                                can_process = False
                                break

                    if status == "ready":
                        if has_existing_dual_subtitle(item, self._subtitle_suffix):
                            status = "done"
                            status_badge = {"text": "双语已生成", "color": "success"}
                            can_process = False
                        else:
                            has_native, native_desc = check_native_bilingual(item)
                            if has_native:
                                status = "native_dual"
                                status_badge = {"text": "自带双语", "color": "teal"}
                                can_process = False
                            else:
                                # 快速嗅探字幕轨
                                ext_tracks = find_external_subtitles(item)
                                emb_tracks = probe_embedded_subtitles(item)
                                all_t = ext_tracks + emb_tracks

                                has_zh = any(is_chinese_lang(t.lang) or "中" in (t.title or "") for t in all_t)
                                has_en = any(is_english_lang(t.lang) or "英" in (t.title or "") for t in all_t)
                                has_ja = any(is_japanese_lang(t.lang) or "日" in (t.title or "") for t in all_t)

                            if has_zh and has_en:
                                status_badge = {"text": "中英内嵌就绪", "color": "primary"}
                            elif has_en and not has_zh:
                                if self._ai_enabled:
                                    status_badge = {"text": "纯英(AI补译)", "color": "amber-darken-3"}
                                else:
                                    status_badge = {"text": "仅英文(需AI)", "color": "blue-grey"}
                            elif has_zh and not has_en:
                                status_badge = {"text": "纯中文(可生成)", "color": "indigo"}
                            elif has_ja:
                                status_badge = {"text": "日文源(可AI)", "color": "purple"}
                            elif not all_t:
                                status_badge = {"text": "无字幕轨", "color": "grey"}

                    videos.append({
                        "name": item.name,
                        "path": str(item),
                        "size_text": size_text,
                        "status": status,
                        "badge": status_badge,
                        "can_process": can_process,
                    })

        except Exception as e:
            return {"error": str(e)}

        return {
            "path": str(p),
            "parent": parent,
            "dirs": dirs,
            "videos": videos,
        }

    # ---------------- 插件配置 Schema (Vuetify Form) ----------------
    def _build_config(self) -> dict:
        return {
            "enabled": self._enabled,
            "listen_transfer_event": self._listen_transfer_event,
            "send_notify": self._send_notify,
            "transfer_paths": self._transfer_paths,
            "mode": self._mode,
            "subtitle_suffix": self._subtitle_suffix,
            "order": self._order,
            "backup": self._backup,
            "overwrite": self._overwrite,
            "min_file_size": self._min_file_size,
            "skip_if_exists": self._skip_if_exists,
            "browse_root": self._browse_root,
            "font_chinese": self._font_chinese,
            "font_english": self._font_english,
            "chinese_color_preset": _detect_color_preset(self._chinese_color, COLOR_PRESETS_ZH),
            "english_color_preset": _detect_color_preset(self._english_color, COLOR_PRESETS_EN),
            "chinese_color": self._chinese_color,
            "english_color": self._english_color,
            "margin_v": self._margin_v,
            "time_offset": self._time_offset,
            "ai_enabled": self._ai_enabled,
            "ai_base_url": self._ai_base_url,
            "ai_api_key": self._ai_api_key,
            "ai_model": self._ai_model,
            "ai_models": self._ai_models,
            "ai_target_lang": self._ai_target_lang,
            "ai_cache_enabled": self._ai_cache_enabled,
            "ai_mark_translated": self._ai_mark_translated,
            "ai_translate_zh_to_en": self._ai_translate_zh_to_en,
            "_api_token": getattr(settings, "API_TOKEN", "") or "",
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        form_schema = [
            {
                "component": "VForm",
                "content": [
                    # 第一行：总开关与自动入库
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "listen_transfer_event",
                                            "label": "入库自动执行",
                                            "hint": "新影片整理入库后，自动检测并生成双语字幕",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "send_notify",
                                            "label": "发送通知",
                                            "hint": "双语字幕合成完成后发送系统通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 生效目录
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "transfer_paths",
                                            "label": "入库自动执行生效目录（白名单）",
                                            "rows": 2,
                                            "placeholder": "容器内绝对路径，每行一个\n例如：/vol2/1000/Emby观影库\n留空 = 所有媒体入库均自动执行",
                                            "hint": "留空表示对所有入库视频生效；若指定目录，仅该目录下的影片入库会自动处理",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 第二行：处理模式与交付格式
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mode",
                                            "label": "交付模式",
                                            "items": [
                                                {"title": "仅生成外挂字幕 (推荐，零风险)", "value": "srt"},
                                                {"title": "仅封回 MKV 容器", "value": "mux"},
                                                {"title": "生成外挂 + 封回 MKV", "value": "both"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "subtitle_suffix",
                                            "label": "外挂字幕文件名格式",
                                            "items": [
                                                {"title": ".zh-cn&en.default.ass (Emby识别为默认双语ASS)", "value": ".zh-cn&en.default.ass"},
                                                {"title": ".zh-CN.ass (标准中文ASS)", "value": ".zh-CN.ass"},
                                                {"title": ".dual.ass (通用双语ASS)", "value": ".dual.ass"},
                                                {"title": ".zh-CN.srt (纯文本SRT)", "value": ".zh-CN.srt"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "order",
                                            "label": "双语上下排列顺序",
                                            "items": [
                                                {"title": "中文在上 / 外文在下 (推荐)", "value": "zh_first"},
                                                {"title": "外文在上 / 中文在下", "value": "en_first"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 第三行：中文字幕排版与色彩
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "font_chinese",
                                            "label": "中文字体",
                                            "placeholder": "Microsoft YaHei",
                                            "hint": "如 Microsoft YaHei, SimHei, PingFang SC",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "chinese_color_preset",
                                            "label": "中文字幕色盘 (快捷选色)",
                                            "items": COLOR_PRESETS_ZH,
                                            "hint": "直接点选常用色；选“自定义”可在右侧输入任意颜色",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "chinese_color",
                                            "label": "中文字幕色号 (HEX或ASS)",
                                            "placeholder": "#FFFFFF 或 &H00FFFFFF",
                                            "hint": "支持网页色 #FFFFFF 或 ASS码 &H00FFFFFF",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 第四行：外文字幕排版与色彩
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "font_english",
                                            "label": "外文字体",
                                            "placeholder": "Arial",
                                            "hint": "如 Arial, Trebuchet MS, Helvetica",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "english_color_preset",
                                            "label": "外文字幕色盘 (快捷选色)",
                                            "items": COLOR_PRESETS_EN,
                                            "hint": "直接点选常用色；选“自定义”可在右侧输入任意颜色",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "english_color",
                                            "label": "外文字幕色号 (HEX或ASS)",
                                            "placeholder": "#00E5FF 或 &H0000E5FF",
                                            "hint": "支持网页色 #00E5FF 或 ASS码 &H0000E5FF",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 第五行：排版边距与微调
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "margin_v",
                                            "label": "垂直边距 MarginV",
                                            "placeholder": "45",
                                            "hint": "默认 45 (1080p/4K自动等比缩放)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "time_offset",
                                            "label": "全局时间轴偏移 (秒)",
                                            "placeholder": "0.0",
                                            "hint": "正数延后，负数提前，如 +1.5 或 -1.0",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "skip_if_exists",
                                            "label": "已有双语字幕跳过",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 第四行：AI 补全大模型配置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "ai_enabled",
                                            "label": "启用 AI 缺失翻译补全",
                                            "hint": "当片源仅有单语言字幕时调用大模型翻译并合成双语",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ai_base_url",
                                            "label": "AI 接口地址 (Base URL)",
                                            "placeholder": "如 https://api.deepseek.com/v1",
                                            "hint": "兼容 OpenAI 标准接口，末尾自动补 /v1",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ai_api_key",
                                            "label": "API Key",
                                            "type": "password",
                                            "hint": "模型接口 API Key",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # AI 第二行：模型选择与配置
                    {
                        "component": "VRow",
                        "props": {"v-show": "ai_enabled"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCombobox",
                                        "props": {
                                            "model": "ai_model",
                                            "label": "AI 模型 / 节点名称",
                                            "items": self._ai_models or ["deepseek-chat", "gpt-4o-mini", "claude-3-5-sonnet", "qwen-plus"],
                                            "placeholder": "可直接手动输入或前往详情页一键拉取选择",
                                            "hint": "提示：保存后可前往插件详情页「AI 接口与模型管理」进行连通测试、拉取所有可用节点并一键切换",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "ai_cache_enabled",
                                            "label": "开启翻译持久化缓存 (节省Token)",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 媒体浏览根目录
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "browse_root",
                                            "label": "媒体库浏览根目录 (容器内路径)",
                                            "placeholder": "/vol2/1000",
                                            "hint": "插件详情页「媒体浏览」的初始根目录",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
        return form_schema, self._build_config()

    # ---------------- 插件详情页可视化渲染 (get_page) ----------------
    def get_page(self) -> List[dict]:
        tab = self._page_tab or "browse"
        browse_tab = self._render_browse_tab()
        history_tab = self._render_history_tab()
        ai_tab = self._render_ai_tab()

        return [
            # 顶部 Tab 切换按钮
            {
                "component": "div",
                "props": {"class": "mb-3 d-flex align-center"},
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
                                    "prepend-icon": "mdi-folder-play-outline",
                                },
                                "text": "媒体库浏览与生成",
                                "events": {
                                    "click": {
                                        "api": "plugin/SubGenerator/set_tab?tab=browse",
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
                                "text": f"任务历史 ({len(self._tasks)})",
                                "events": {
                                    "click": {
                                        "api": "plugin/SubGenerator/set_tab?tab=history",
                                        "method": "get",
                                    }
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "value": "ai",
                                    "size": "small",
                                    "variant": "text",
                                    "prepend-icon": "mdi-robot-outline",
                                },
                                "text": "AI 接口与模型管理",
                                "events": {
                                    "click": {
                                        "api": "plugin/SubGenerator/set_tab?tab=ai",
                                        "method": "get",
                                    }
                                },
                            },
                        ],
                    }
                ],
            },
            browse_tab if tab == "browse" else {"component": "div", "content": []},
            history_tab if tab == "history" else {"component": "div", "content": []},
            ai_tab if tab == "ai" else {"component": "div", "content": []},
        ]

    def _render_browse_tab(self) -> dict:
        browse = self._list_browse(self._browse_path or self._browse_root)
        if browse.get("error"):
            return {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": f"无法浏览路径: {browse['error']}，请在配置中检查「媒体库浏览根目录」。",
                },
            }

        current = browse.get("path", self._browse_path)
        parent = browse.get("parent")
        dirs = browse.get("dirs", [])
        videos = browse.get("videos", [])

        # 头部导航栏
        header_actions = []
        if parent:
            header_actions.append({
                "component": "VBtn",
                "props": {
                    "color": "secondary",
                    "variant": "tonal",
                    "size": "small",
                    "prepend-icon": "mdi-arrow-up",
                    "class": "me-2",
                },
                "text": "上一级",
                "events": {
                    "click": {
                        "api": f"plugin/SubGenerator/browse?path={quote(parent)}",
                        "method": "get",
                    }
                },
            })

        if videos:
            header_actions.append({
                "component": "VBtn",
                "props": {
                    "color": "primary",
                    "variant": "flat",
                    "size": "small",
                    "prepend-icon": "mdi-playlist-play",
                    "class": "me-2",
                },
                "text": f"批量处理当前目录 ({len(videos)})",
                "events": {
                    "click": {
                        "api": f"plugin/SubGenerator/process_dir?path={quote(current)}",
                        "method": "get",
                    }
                },
            })

        header_col = [
            {
                "component": "VCol",
                "props": {"cols": 12, "md": 7, "class": "d-flex align-center"},
                "content": [
                    {
                        "component": "VIcon",
                        "props": {"icon": "mdi-folder-open", "color": "amber", "class": "me-2"},
                    },
                    {
                        "component": "span",
                        "props": {"class": "text-subtitle-1 font-weight-medium text-truncate"},
                        "text": current,
                    },
                ],
            }
        ]
        if header_actions:
            header_col.append({
                "component": "VCol",
                "props": {"cols": 12, "md": 5, "class": "d-flex align-center justify-end flex-wrap"},
                "content": header_actions,
            })

        rows = []

        # 文件夹行
        for d in dirs:
            rows.append({
                "component": "div",
                "props": {
                    "class": "d-flex align-center pa-3 rounded-lg mb-2",
                    "style": "background: rgba(var(--v-theme-surface-variant), 0.35); cursor: pointer;",
                },
                "events": {
                    "click": {
                        "api": f"plugin/SubGenerator/browse?path={quote(d['path'])}",
                        "method": "get",
                    }
                },
                "content": [
                    {"component": "VIcon", "props": {"icon": "mdi-folder", "color": "amber", "class": "me-3", "size": "large"}},
                    {"component": "span", "props": {"class": "text-body-1 font-weight-medium text-truncate flex-grow-1"}, "text": d["name"]},
                    {"component": "VIcon", "props": {"icon": "mdi-chevron-right", "class": "ms-2"}},
                ],
            })

        # 视频文件行
        for v in videos:
            badge_info = v["badge"]
            right_items = [
                {"component": "span", "props": {"class": "text-body-2 text-disabled me-4"}, "text": v["size_text"]},
                {
                    "component": "VChip",
                    "props": {"size": "small", "color": badge_info["color"], "variant": "flat", "class": "me-3"},
                    "text": badge_info["text"],
                },
            ]

            if v["can_process"]:
                right_items.append({
                    "component": "VBtn",
                    "props": {
                        "color": badge_info["color"],
                        "variant": "flat",
                        "size": "small",
                        "prepend-icon": "mdi-subtitles-outline",
                    },
                    "text": "生成双语",
                    "events": {
                        "click": {
                            "api": f"plugin/SubGenerator/process?path={quote(v['path'])}",
                            "method": "get",
                        }
                    },
                })
            elif v["status"] in ("done", "native_dual"):
                right_items.append({
                    "component": "VBtn",
                    "props": {
                        "color": "secondary",
                        "variant": "tonal",
                        "size": "small",
                        "prepend-icon": "mdi-refresh",
                    },
                    "text": "重新生成",
                    "events": {
                        "click": {
                            "api": f"plugin/SubGenerator/regenerate?path={quote(v['path'])}",
                            "method": "get",
                        }
                    },
                })

            rows.append({
                "component": "div",
                "props": {
                    "class": "d-flex align-center pa-3 rounded-lg mb-2",
                    "style": "background: rgba(var(--v-theme-surface-variant), 0.2); border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));",
                },
                "content": [
                    {"component": "VIcon", "props": {"icon": "mdi-movie-open-outline", "color": "primary", "class": "me-3", "size": "large"}},
                    {"component": "span", "props": {"class": "text-body-2 font-weight-medium text-truncate flex-grow-1"}, "text": v["name"]},
                    {"component": "div", "props": {"class": "d-flex align-center"}, "content": right_items},
                ],
            })

        if not dirs and not videos:
            rows.append({
                "component": "div",
                "props": {"class": "text-center pa-8 text-disabled"},
                "content": [{"component": "span", "text": "此目录下未发现视频文件或子目录"}],
            })

        return {
            "component": "div",
            "content": [
                {"component": "VRow", "props": {"class": "mb-2"}, "content": header_col},
                {"component": "div", "content": rows},
            ],
        }

    def _render_history_tab(self) -> dict:
        db_tasks = self.load_tasks()
        if db_tasks:
            for k, v in db_tasks.items():
                if k not in self._tasks:
                    self._tasks[k] = v
                elif self._tasks[k].get("status") == TaskStatus.IN_PROGRESS.value and v.get("status") != TaskStatus.IN_PROGRESS.value:
                    self._tasks[k] = v

        task_list = sorted(
            self._tasks.values(),
            key=lambda x: x.get("add_time", ""),
            reverse=True,
        )

        rows = []
        for t in task_list[:50]:
            st = t.get("status")
            if st == TaskStatus.COMPLETED.value:
                chip_color = "success"
                st_text = "成功"
            elif st == TaskStatus.IN_PROGRESS.value:
                chip_color = "warning"
                st_text = "处理中"
            elif st == TaskStatus.PENDING.value:
                chip_color = "info"
                st_text = "排队中"
            elif st == TaskStatus.IGNORED.value:
                chip_color = "grey"
                st_text = "跳过"
            else:
                chip_color = "error"
                st_text = "失败"

            video_name = Path(t.get("video_file", "")).name
            add_time = t.get("add_time", "")
            msg = t.get("message", "")

            logs = t.get("logs") or []
            log_preview = "\n".join(logs[-4:]) if logs else ""

            row_content = [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between mb-1"},
                    "content": [
                        {"component": "span", "props": {"class": "font-weight-bold text-body-2 text-truncate"}, "text": video_name},
                        {
                            "component": "VChip",
                            "props": {"size": "x-small", "color": chip_color, "variant": "flat"},
                            "text": st_text,
                        },
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between text-caption text-disabled"},
                    "content": [
                        {"component": "span", "text": f"添加时间: {add_time}" + (f" | 时间轴偏移: {float(t.get('time_offset')):+.2f}s" if t.get('time_offset') else "")},
                        {"component": "span", "text": msg},
                    ],
                },
            ]

            if log_preview:
                row_content.append({
                    "component": "div",
                    "props": {
                        "class": "mt-2 pa-2 rounded text-caption",
                        "style": "background: rgba(0,0,0,0.25); font-family: monospace; white-space: pre-wrap;",
                    },
                    "text": log_preview,
                })

            rows.append({
                "component": "div",
                "props": {
                    "class": "pa-3 rounded-lg mb-2",
                    "style": "background: rgba(var(--v-theme-surface-variant), 0.2); border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));",
                },
                "content": row_content,
            })

        return {
            "component": "div",
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between mb-3"},
                    "content": [
                        {"component": "span", "props": {"class": "text-subtitle-2"}, "text": f"任务列表 (共 {len(self._tasks)} 条)"},
                        {
                            "component": "VBtn",
                            "props": {
                                "color": "error",
                                "variant": "text",
                                "size": "small",
                                "prepend-icon": "mdi-delete-outline",
                            },
                            "text": "清空历史记录",
                            "events": {
                                "click": {
                                    "api": "plugin/SubGenerator/clear_tasks",
                                    "method": "get",
                                }
                            },
                        },
                    ],
                },
                {"component": "div", "content": rows},
            ],
        }

    def _render_ai_tab(self) -> dict:
        conn = self._ai_connection_info or {}
        status = conn.get("status", "untested")
        latency = conn.get("latency_ms", 0)
        conn_msg = conn.get("message", "")
        last_check = conn.get("last_check_time", "")

        # 1. 状态总览卡片
        status_color = "grey"
        status_text = "未测试连通性"
        if status == "connected":
            status_color = "success"
            status_text = f"连通正常 ({latency}ms)"
        elif status == "error":
            status_color = "error"
            status_text = "连接异常"

        active_model = self._ai_model or "未选择节点"
        model_chip_color = "primary" if self._ai_model else "warning"

        masked_key = "未填写"
        if self._ai_api_key:
            if len(self._ai_api_key) > 8:
                masked_key = f"{self._ai_api_key[:4]}...{self._ai_api_key[-4:]}"
            else:
                masked_key = "已填写 (密文)"

        overview_card_content = [
            {
                "component": "div",
                "props": {"class": "d-flex align-center justify-space-between mb-3 flex-wrap"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {"component": "VIcon", "props": {"icon": "mdi-robot-outline", "color": "primary", "class": "me-2", "size": "large"}},
                            {"component": "span", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": "AI 大模型接口状态"},
                        ],
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center flex-wrap gap-2"},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"color": status_color, "variant": "flat", "size": "small", "class": "me-2"},
                                "text": status_text,
                            },
                            {
                                "component": "VChip",
                                "props": {"color": model_chip_color, "variant": "tonal", "size": "small"},
                                "text": f"当前生效节点: {active_model}",
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "props": {"class": "text-body-2"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {"component": "span", "props": {"class": "text-disabled"}, "text": "接口地址 (Base URL): "},
                            {"component": "span", "props": {"class": "font-weight-medium"}, "text": self._ai_base_url or "未配置"},
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {"component": "span", "props": {"class": "text-disabled"}, "text": "API Key 状态: "},
                            {"component": "span", "props": {"class": "font-weight-medium"}, "text": masked_key},
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {"component": "span", "props": {"class": "text-disabled"}, "text": "本地翻译缓存: "},
                            {"component": "span", "props": {"class": "font-weight-medium"}, "text": f"{len(self._ai_cache)} 条已缓存"},
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {"component": "span", "props": {"class": "text-disabled"}, "text": "上次检测时间: "},
                            {"component": "span", "props": {"class": "font-weight-medium"}, "text": last_check or "尚未测试"},
                        ],
                    },
                ],
            },
        ]

        if conn_msg:
            alert_type = "success" if status == "connected" else "error"
            overview_card_content.append({
                "component": "VAlert",
                "props": {
                    "type": alert_type,
                    "variant": "tonal",
                    "density": "compact",
                    "class": "mt-3",
                    "text": conn_msg,
                },
            })

        overview_card = {
            "component": "div",
            "props": {
                "class": "pa-4 rounded-lg mb-4",
                "style": "background: rgba(var(--v-theme-surface-variant), 0.25); border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));",
            },
            "content": overview_card_content,
        }

        # 2. 交互操作栏
        action_bar = {
            "component": "div",
            "props": {"class": "d-flex align-center flex-wrap gap-2 mb-4"},
            "content": [
                {
                    "component": "VBtn",
                    "props": {
                        "color": "info",
                        "variant": "flat",
                        "size": "small",
                        "prepend-icon": "mdi-lan-check",
                        "class": "me-2 mb-2",
                    },
                    "text": "1. 测试接口连通性",
                    "events": {
                        "click": {
                            "api": "plugin/SubGenerator/ai_check",
                            "method": "get",
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {
                        "color": "primary",
                        "variant": "flat",
                        "size": "small",
                        "prepend-icon": "mdi-cloud-download-outline",
                        "class": "me-2 mb-2",
                    },
                    "text": "2. 拉取所有可用节点",
                    "events": {
                        "click": {
                            "api": "plugin/SubGenerator/ai_models",
                            "method": "get",
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {
                        "color": "success",
                        "variant": "flat",
                        "size": "small",
                        "prepend-icon": "mdi-translate",
                        "class": "me-2 mb-2",
                    },
                    "text": "3. 当前节点翻译实测",
                    "events": {
                        "click": {
                            "api": "plugin/SubGenerator/ai_test_translate",
                            "method": "get",
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {
                        "color": "warning",
                        "variant": "tonal",
                        "size": "small",
                        "prepend-icon": "mdi-broom",
                        "class": "mb-2",
                    },
                    "text": f"清空本地翻译缓存 ({len(self._ai_cache)})",
                    "events": {
                        "click": {
                            "api": "plugin/SubGenerator/clear_cache",
                            "method": "get",
                        }
                    },
                },
            ],
        }

        # 3. 翻译实测结果卡片（如果曾测试过）
        test_res = conn.get("test_translate_result")
        test_card = {"component": "div", "content": []}
        if test_res:
            test_card = {
                "component": "div",
                "props": {
                    "class": "pa-3 rounded-lg mb-4",
                    "style": "background: rgba(var(--v-theme-surface-variant), 0.15); border: 1px dashed rgba(var(--v-border-color), var(--v-border-opacity));",
                },
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center justify-space-between mb-2"},
                        "content": [
                            {"component": "span", "props": {"class": "text-caption font-weight-bold text-success"}, "text": f"✨ 单句翻译实测结果 (模型: {test_res.get('model')} | 耗时: {test_res.get('latency_ms')}ms)"},
                            {"component": "span", "props": {"class": "text-caption text-disabled"}, "text": test_res.get("time", "")},
                        ],
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 font-italic text-disabled mb-1"},
                        "text": f"原文：{test_res.get('input')}",
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 font-weight-medium text-success"},
                        "text": f"译文：{test_res.get('output')}",
                    },
                ],
            }

        # 4. 可用节点列表
        model_rows = []
        if self._ai_models:
            for m in self._ai_models:
                is_selected = (m == self._ai_model)
                right_actions = []
                if is_selected:
                    right_actions.append({
                        "component": "VChip",
                        "props": {"color": "success", "variant": "flat", "size": "small", "prepend-icon": "mdi-check-circle"},
                        "text": "当前生效中",
                    })
                else:
                    right_actions.append({
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "tonal",
                            "size": "small",
                            "prepend-icon": "mdi-check",
                        },
                        "text": "设为当前节点",
                        "events": {
                            "click": {
                                "api": f"plugin/SubGenerator/select_model?model={quote(m)}",
                                "method": "get",
                            }
                        },
                    })

                bg_style = "background: rgba(var(--v-theme-surface-variant), 0.4); border: 1px solid rgba(var(--v-theme-primary), 0.5);" if is_selected else "background: rgba(var(--v-theme-surface-variant), 0.2); border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));"

                model_rows.append({
                    "component": "div",
                    "props": {
                        "class": "d-flex align-center justify-space-between pa-3 rounded-lg mb-2",
                        "style": bg_style,
                    },
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex align-center text-truncate flex-grow-1 me-3"},
                            "content": [
                                {"component": "VIcon", "props": {"icon": "mdi-cube-outline", "color": "primary" if is_selected else "disabled", "class": "me-3"}},
                                {"component": "span", "props": {"class": "text-body-2 font-weight-medium font-monospace"}, "text": m},
                            ],
                        },
                        {
                            "component": "div",
                            "content": right_actions,
                        },
                    ],
                })
        else:
            model_rows.append({
                "component": "div",
                "props": {"class": "text-center pa-8 text-disabled"},
                "content": [
                    {"component": "VIcon", "props": {"icon": "mdi-cloud-search-outline", "size": "x-large", "class": "mb-2"}},
                    {"component": "div", "props": {"class": "text-body-2"}, "text": "暂未获取到模型节点列表。请确认配置中的接口地址和 API Key 正确，并点击上方「2. 拉取所有可用节点」按钮。"},
                ],
            })

        models_container = {
            "component": "div",
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center justify-space-between mb-3"},
                    "content": [
                        {"component": "span", "props": {"class": "text-subtitle-2"}, "text": f"可用模型节点列表 (共 {len(self._ai_models)} 个)"},
                    ],
                },
                {"component": "div", "content": model_rows},
            ],
        }

        return {
            "component": "div",
            "content": [
                overview_card,
                action_bar,
                test_card,
                models_container,
            ],
        }


# 兼容旧版 DualSub 插件类名
DualSub = SubGenerator
