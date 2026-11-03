"""很薄的 AI 调用封装：chat_json(messages) -> dict。

用 httpx 直接调 OpenAI 兼容的 /chat/completions（目前是 DeepSeek），要求返回 JSON 对象。
换别家 AI 只改 config/llm.yaml（各家特有的参数写在 extra_body 里）。
- 每次请求超时 timeout_seconds（默认 60 秒）
- 429 / 5xx / 网络错误：退避后重试，最多 2 次
- 返回的内容不是合法 JSON，或输出被 max_tokens 截断：重试 1 次，还不行就报 LLMBadJSON
- 401 / 402（余额不足）/ 403：报 LLMFatal，后面的批次不用再试
- 可以在几个线程里同时调用（httpx.Client 线程安全；用量计数加了锁）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    """这一次调用失败。"""


class LLMFatal(LLMError):
    """Key 无效、余额不足之类，继续调用也没用。"""


class LLMBadJSON(LLMError):
    """返回的内容不是合法 JSON 对象。"""


class ChatClient:
    def __init__(self, cfg: dict, api_key: str, transport=None, sleep: Callable[[float], None] = time.sleep,
                 http_retries: int = 2, json_retries: int = 1, backoff: float = 2.0):
        self.url = cfg["base_url"].rstrip("/") + "/chat/completions"
        self.model = cfg["model"]
        self.temperature = cfg.get("temperature", 0)
        self.max_tokens = cfg.get("max_tokens")
        self.extra_body = dict(cfg.get("extra_body") or {})
        self.http_retries, self.json_retries, self.backoff = http_retries, json_retries, backoff
        self.sleep = sleep
        self.http = httpx.Client(
            timeout=float(cfg.get("timeout_seconds", 60)), transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def chat_json(self, messages: list) -> dict:
        for attempt in range(self.json_retries + 1):
            content = self._post(messages)
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
            except (TypeError, ValueError):
                pass
            log.warning("AI 返回的不是合法 JSON（第 %d 次）", attempt + 1)
        raise LLMBadJSON("AI 返回的内容不是合法 JSON")

    def _post(self, messages: list) -> Optional[str]:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "response_format": {"type": "json_object"}, "stream": False}
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        body.update(self.extra_body)
        for attempt in range(self.http_retries + 1):
            last = attempt == self.http_retries
            try:
                r = self.http.post(self.url, json=body)
            except httpx.HTTPError as e:
                if last:
                    raise LLMError(f"网络错误 {type(e).__name__}") from e
                self._wait(attempt, None, type(e).__name__)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if last:
                    raise LLMError(f"HTTP {r.status_code}（已重试 {self.http_retries} 次）")
                self._wait(attempt, r.headers.get("Retry-After"), f"HTTP {r.status_code}")
                continue
            if r.status_code in (401, 402, 403):
                raise LLMFatal(f"HTTP {r.status_code}：{_err_text(r)}")
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}：{_err_text(r)}")
            try:
                data = r.json()
                choice = data["choices"][0]
                usage = data.get("usage") or {}
                with self._lock:
                    for k in self.usage:
                        self.usage[k] += int(usage.get(k) or 0)
                if choice.get("finish_reason") == "length":
                    log.warning("AI 输出达到 max_tokens 被截断，按失败处理")
                    return None  # 截断的 JSON 不可信，交给 chat_json 重试
                return choice["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                return None  # 交给 chat_json 当作“不是合法 JSON”处理
        raise LLMError("不应走到这里")

    def _wait(self, attempt: int, retry_after: Optional[str], why: str) -> None:
        wait = self.backoff * (2 ** attempt)
        if retry_after and retry_after.strip().isdigit():
            wait = max(wait, min(float(retry_after), 60.0))
        log.warning("AI 请求失败（%s），%.0f 秒后重试", why, wait)
        self.sleep(wait)


def _err_text(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", ""))[:200]
    except Exception:
        return r.text[:200]
