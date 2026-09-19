#!/usr/bin/env python3
"""AI 字幕翻译引擎：支持所有 OpenAI 兼容接口（DeepSeek / OpenAI / Claude / Ollama 等）。"""

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

from .models import SubtitleItem

# 语言映射
LANG_MAPPINGS = {
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
}


class AITranslator:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        target_lang: str = "zh-CN",
        temperature: float = 0.2,
        cache_enabled: bool = True,
        mark_ai: bool = False,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.target_lang = target_lang or "zh-CN"
        self.temperature = temperature
        self.cache_enabled = cache_enabled
        self.mark_ai = mark_ai

    def _normalize_base_url(self) -> str:
        base = self.base_url
        if not base:
            return ""
        if base.endswith("/v1"):
            return base
        return base + "/v1"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _request(
        self, method: str, url: str, payload: dict = None, timeout: int = 60
    ) -> Tuple[bool, Any]:
        """统一发送 HTTP 请求，局域网直连，外网优先走系统代理。"""
        try:
            is_local = any(
                h in url
                for h in (
                    "localhost",
                    "127.0.0.1",
                    "192.168.",
                    "10.",
                    "172.16.",
                    "172.17.",
                    "172.18.",
                    "172.19.",
                    "172.20.",
                    "172.21.",
                    "172.22.",
                    "172.23.",
                    "172.24.",
                    "172.25.",
                    "172.26.",
                    "172.27.",
                    "172.28.",
                    "172.29.",
                    "172.30.",
                    "172.31.",
                )
            )
            kwargs = {
                "headers": self._headers(),
                "timeout": timeout,
            }
            if is_local:
                kwargs["proxies"] = {"http": None, "https": None}

            if method.upper() == "GET":
                resp = requests.get(url, **kwargs)
            else:
                resp = requests.post(url, json=payload or {}, **kwargs)
        except requests.exceptions.Timeout:
            return False, {"code": "timeout", "message": "请求超时"}
        except requests.exceptions.ConnectionError as e:
            return False, {
                "code": "connection_error",
                "message": f"连接失败: {str(e)[:200]}",
            }
        except Exception as e:
            return False, {"code": "exception", "message": f"请求异常: {str(e)[:200]}"}

        ctype = (resp.headers.get("Content-Type", "") or "").lower()
        body = resp.text or ""
        if resp.status_code >= 400:
            msg = body[:300]
            try:
                err = (resp.json() or {}).get("error", {})
                if isinstance(err, dict):
                    msg = err.get("message", "") or json.dumps(
                        err, ensure_ascii=False
                    )[:300]
            except Exception:
                pass
            return False, {
                "code": "http_error",
                "status": resp.status_code,
                "message": msg or f"HTTP {resp.status_code}",
            }

        if "json" not in ctype:
            return False, {
                "code": "not_json",
                "status": resp.status_code,
                "message": f"返回非 JSON (Content-Type={ctype})",
            }

        try:
            return True, resp.json()
        except Exception as e:
            return False, {
                "code": "parse_error",
                "status": resp.status_code,
                "message": f"JSON 解析失败: {str(e)[:120]}",
            }

    def fetch_models(self) -> Tuple[bool, List[str], str]:
        """获取模型列表并返回 (ok, models, message)。"""
        if not self.base_url:
            return False, [], "请先填写 AI 接口地址"
        base = self._normalize_base_url()
        candidates = [base + "/models"]
        if base.endswith("/v1"):
            candidates.append(base.rstrip("/v1").rstrip("/") + "/models")

        last_err = "未能获取模型列表"
        for endpoint in candidates:
            ok, data = self._request("GET", endpoint, timeout=20)
            if not ok:
                err_msg = data.get("message", "")
                status = data.get("status")
                if status == 401:
                    last_err = f"API Key 认证失败 (HTTP 401): {err_msg}，请检查 API Key 是否正确"
                    return False, [], last_err
                elif status == 403:
                    last_err = f"API 访问受限 (HTTP 403): {err_msg}"
                    return False, [], last_err
                else:
                    last_err = f"{err_msg}" + (f" (HTTP {status})" if status else "")
                continue

            items = data.get("data", []) if isinstance(data, dict) else data
            models = []
            for it in items or []:
                mid = it.get("id") if isinstance(it, dict) else str(it)
                if mid and mid not in models:
                    models.append(mid)
            if models:
                models.sort()
                return True, models, f"成功获取 {len(models)} 个可用模型节点"
            last_err = "接口返回模型列表为空"

        return False, [], last_err

    def check_connection(self) -> Tuple[bool, Dict[str, Any]]:
        """检测连通性与模型调用。"""
        if not self.base_url:
            return False, {"code": "no_url", "message": "未填写 AI 接口地址 (Base URL)"}
        if not self.api_key:
            return False, {"code": "no_key", "message": "未填写 API Key"}

        base = self._normalize_base_url()
        t0 = time.time()
        ok, data = self._request("GET", base + "/models", timeout=15)
        latency = int((time.time() - t0) * 1000)

        # 尝试提取可用模型列表
        models = []
        if ok:
            items = data.get("data", []) if isinstance(data, dict) else data
            for it in items or []:
                mid = it.get("id") if isinstance(it, dict) else str(it)
                if mid and mid not in models:
                    models.append(mid)
            models.sort()

        if not ok:
            status = data.get("status")
            err_msg = data.get("message", "")
            if status == 401:
                return False, {
                    "code": "unauthorized",
                    "message": f"API Key 认证失败 (HTTP 401): {err_msg}，请检查 Key 是否填写正确",
                    "latency_ms": latency,
                }
            return False, {
                "code": data.get("code"),
                "message": f"接口连通失败: {err_msg}" + (f" (HTTP {status})" if status else ""),
                "latency_ms": latency,
            }

        if not self.model:
            return True, {
                "code": "ok_no_model",
                "message": f"接口连通正常 (延迟 {latency}ms)！发现 {len(models)} 个可用节点，请在下方选择节点。",
                "latency_ms": latency,
                "models": models,
            }

        # 若已填写/选择模型，发送最小测试对话
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 10,
        }
        t0 = time.time()
        ok2, data2 = self._request(
            "POST", base + "/chat/completions", payload, timeout=30
        )
        latency2 = int((time.time() - t0) * 1000)
        if not ok2:
            return False, {
                "code": data2.get("code"),
                "message": f"接口已连通，但当前节点 [{self.model}] 响应失败: {data2.get('message')}",
                "latency_ms": latency2,
                "models": models,
            }

        return True, {
            "code": "ok",
            "message": f"接口与节点连通正常！当前节点 [{self.model}] 响应正常 (延迟 {latency2}ms)",
            "latency_ms": latency2,
            "models": models,
            "model": self.model,
        }

    def test_translate(self, sample_text: str = "Hello world") -> Tuple[bool, str, int]:
        """进行单句翻译实测，验证模型翻译输出质量与可用性。"""
        if not self.model:
            return False, "请先选择要测试的节点/模型", 0
        t0 = time.time()
        test_items = [SubtitleItem(0.0, 1.0, sample_text)]
        res = self.translate_subtitle_items(test_items, {}, [], source_lang_hint="英文")
        latency = int((time.time() - t0) * 1000)
        if res and len(res) == 1 and res[0].text.strip():
            return True, res[0].text.strip(), latency
        return False, "翻译返回为空或请求失败", latency

    @staticmethod
    def _parse_translations(raw_content: str) -> Dict[int, str]:
        """从大模型响应中清洗并提取 {id: translation} 映射。"""
        if not raw_content:
            return {}
        text = raw_content.strip()
        # 清除 markdown 代码块
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        data = None
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\[[\s\S]*\]|\{[\s\S]*\}", text)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    pass

        if isinstance(data, dict):
            data = data.get("translations") or data.get("data") or []

        result = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    tid = item.get("id")
                    trans = (
                        item.get("translation")
                        or item.get("target")
                        or item.get("text")
                        or ""
                    ).strip()
                    if tid is not None and trans:
                        try:
                            result[int(tid)] = trans
                        except ValueError:
                            pass
        return result

    def translate_subtitle_items(
        self,
        items: List[SubtitleItem],
        cache: Dict[str, str],
        logs: List[str],
        source_lang_hint: str = "外文",
    ) -> List[SubtitleItem]:
        """批量将 items 翻译为 target_lang，并返回相同时间轴的翻译后 SubtitleItem 列表。"""
        if not self.base_url or not self.api_key or not self.model:
            logs.append("AI 翻译未配置或参数不完整，跳过补全")
            return []

        target_lang_name = LANG_MAPPINGS.get(self.target_lang, self.target_lang)
        logs.append(
            f"开始 AI 批量翻译（从 {source_lang_hint} 到 {target_lang_name}），共 {len(items)} 句..."
        )

        # 检查本地缓存
        translated_map = {}
        to_translate_indices = []
        for idx, it in enumerate(items):
            clean_txt = it.text.strip()
            if self.cache_enabled and clean_txt in cache:
                translated_map[idx] = cache[clean_txt]
            else:
                to_translate_indices.append(idx)

        logs.append(
            f"翻译缓存命中: {len(items) - len(to_translate_indices)}/{len(items)} 条，待翻译: {len(to_translate_indices)} 条"
        )

        if not to_translate_indices:
            # 全部命中
            results = []
            for idx, it in enumerate(items):
                txt = translated_map.get(idx, it.text)
                if self.mark_ai:
                    txt = "\u200b[AI]\u200b" + txt
                results.append(SubtitleItem(it.start, it.end, txt))
            return results

        # 按 50 条或字符数分批
        batches = []
        current_batch = []
        current_len = 0
        for idx in to_translate_indices:
            it = items[idx]
            prev_ctx = items[idx - 1].text if idx > 0 else ""
            entry = {"id": idx, "text": it.text, "context": prev_ctx}
            entry_len = len(it.text) + len(prev_ctx) + 60
            if current_batch and (len(current_batch) >= 50 or current_len + entry_len > 4000):
                batches.append(current_batch)
                current_batch = []
                current_len = 0
            current_batch.append(entry)
            current_len += entry_len
        if current_batch:
            batches.append(current_batch)

        endpoint = self._normalize_base_url() + "/chat/completions"
        new_cache_entries = {}
        success_batches = 0

        for b_idx, batch in enumerate(batches, 1):
            prompt = (
                f"请将下方 JSON 数组中的每一项 {source_lang_hint}影视字幕翻译成准确、通顺的{target_lang_name}。\n"
                f"规则说明：\n"
                f"1. context 字段是上一句字幕，仅供参考上下文语境，无需翻译 context。\n"
                f"2. 保持台词简短精练与影视原意，不要过度书面化。\n"
                f"3. 严禁改变 id！严格仅输出 JSON 数组，格式为：[{{\"id\": 0, \"translation\": \"译文\"}}]\n"
                f"4. 不要包含 Markdown 代码标记或任何多余文字。\n\n"
                + json.dumps(batch, ensure_ascii=False)
            )
            payload = {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [
                    {
                        "role": "system",
                        "content": f"你是一名资深的影视双语字幕专家，只负责输出严格的 JSON 翻译数组。目标语言：{target_lang_name}。",
                    },
                    {"role": "user", "content": prompt},
                ],
            }

            # 带 1 次重试机制
            data = None
            for attempt in range(2):
                ok, resp_data = self._request("POST", endpoint, payload, timeout=120)
                if ok:
                    data = resp_data
                    break
                else:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    logs.append(
                        f"第 {b_idx}/{len(batches)} 批翻译请求失败: {resp_data.get('message')}"
                    )

            if not data:
                continue

            raw = ""
            try:
                raw = data["choices"][0]["message"]["content"] or ""
            except Exception:
                pass

            trans_dict = self._parse_translations(raw)
            if trans_dict:
                success_batches += 1
                for tid, tr in trans_dict.items():
                    translated_map[tid] = tr
                    if tid < len(items):
                        new_cache_entries[items[tid].text.strip()] = tr
            else:
                logs.append(
                    f"第 {b_idx}/{len(batches)} 批未能解析翻译结果 (前100字): {raw[:100]}"
                )

        if self.cache_enabled and new_cache_entries:
            cache.update(new_cache_entries)

        # 构建最终输出
        results = []
        for idx, it in enumerate(items):
            trans_text = translated_map.get(idx)
            if trans_text:
                if self.mark_ai:
                    trans_text = "\u200b[AI]\u200b" + trans_text
                results.append(SubtitleItem(it.start, it.end, trans_text))

        logs.append(
            f"AI 翻译完成: 成功补全 {len(results)}/{len(items)} 句 (批次成功: {success_batches}/{len(batches)})"
        )
        return results
