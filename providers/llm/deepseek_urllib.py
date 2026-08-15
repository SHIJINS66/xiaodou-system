#!/usr/bin/env python3
"""DeepSeek 原生（urllib）LLM provider — 等价迁移自旧 step02/bin/providers/deepseek_client.py。

为什么保留 urllib 版而不是改用 openai SDK：
    - 线上 planner（step02）实际跑的就是这个 urllib 实现，行为必须等价。
    - 零依赖：不 import openai，用户装框架时少一个必装项。
    - 它有更细的重试/退避/密钥脱敏逻辑（HTTPError retry 表、Retry-After、
      Bearer/api_key token 脱敏），openai SDK 封装后这些细节会丢。
    - 与 step03 的 openai-SDK 版 deepseek_client 不同——本文件属于 step02 链路的等价迁移。

保留的线上行为（逐条对齐）：
    - __init__ 强校验 DEEPSEEK_API_KEY、thinking=enabled、reasoning_effort in (high,max)
      ——注意：thinking 强校验是 deepseek 写死，等价迁移先保留；
        若要换非 deepseek 模型，请改用 providers.llm.openai_compatible（可配置透传）。
    - json_completion 的 attempts=3 重试 + HTTP 408/409/425/429/5xx 退避 + 密钥脱敏。
    - parse_json_content 剥离 ``` 代码围栏。

适配点（相对线上）：
    - 继承 providers.base.LLMProvider 并实现 chat/chat_json，同时保留
      json_completion(system, user, temperature, max_tokens) 便捷方法，
      让迁移过来的脚本调用方式基本不变。
    - 用 register_llm("deepseek") 注册，settings 里 provider: "deepseek" 即命中。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from providers.base import ChatMessage, LLMProvider, register_llm
from scripts.settings_loader import ConfigError

_REDACT_PATTERN = re.compile(r"(?i)(bearer|api[_-]?key|token|secret|password)\s*[:=]\s*\S+")


def _safe_api_error(body: bytes) -> str:
    try:
        value = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return "unparseable_error_body"
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        return "api_error_without_object"
    fields: list[str] = []
    for key in ("type", "code", "param", "message"):
        item = error.get(key)
        if item is None:
            continue
        text = re.sub(r"[\r\n\t]+", " ", str(item)).strip()
        text = _REDACT_PATTERN.sub(r"\1=<REDACTED>", text)
        fields.append(f"{key}={text[:240]}")
    return "; ".join(fields) if fields else "api_error_without_safe_fields"


def parse_json_content(content: str) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ConfigError("模型返回空 content")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ConfigError("模型输出根节点不是对象")
    return value


@register_llm("deepseek")
class DeepSeekProvider(LLMProvider):
    def __init__(self, settings: Any, conf: dict[str, Any]):
        self.conf = conf
        api_key = os.environ.get(conf.get("api_key_env", "DEEPSEEK_API_KEY"), "")
        if not api_key:
            raise ConfigError(f"{conf.get('api_key_env', 'DEEPSEEK_API_KEY')} 未配置")
        self.api_key = api_key
        self.base_url = (conf.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        self.model = conf.get("model") or "deepseek-v4-flash"
        self.timeout = int(conf.get("timeout_seconds", 240) or 240)
        self.thinking_type = conf.get("thinking_type", "enabled")
        self.reasoning_effort = conf.get("reasoning_effort", "high")

        if self.thinking_type != "enabled":
            raise ConfigError("deepseek 原生 provider 要求 thinking=enabled")
        if self.reasoning_effort not in ("high", "max"):
            raise ConfigError("deepseek 原生 provider 要求 reasoning_effort=high 或 max")

    # ---- 与旧脚本调用方式对齐的便捷方法 ----
    def json_completion(
        self,
        system: str,
        user: str,
        temperature: float = 0.35,
        max_tokens: int = 16000,
        attempts: int = 3,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """对齐旧 deepseek_client.json_completion 的签名与返回 (value, meta)。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.thinking_type},
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": max_tokens,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        last: str | None = None
        for n in range(1, attempts + 1):
            req = urllib.request.Request(
                self.base_url + "/chat/completions",
                data=body,
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    envelope = json.loads(resp.read().decode())
                choice = envelope["choices"][0]
                message = choice["message"]
                content = message.get("content")
                reasoning = message.get("reasoning_content")
                finish_reason = choice.get("finish_reason")
                if not isinstance(content, str) or not content.strip():
                    raise ConfigError(
                        f"模型返回空 content；finish_reason={finish_reason}; "
                        f"reasoning_content_present={isinstance(reasoning, str) and bool(reasoning)}; "
                        f"reasoning_content_length={len(reasoning) if isinstance(reasoning, str) else 0}"
                    )
                value = parse_json_content(content)
                return value, {
                    "model": envelope.get("model", self.model),
                    "usage": envelope.get("usage"),
                    "attempt": n,
                    "thinking_type": self.thinking_type,
                    "reasoning_effort": self.reasoning_effort,
                    "finish_reason": finish_reason,
                    "reasoning_content_present": isinstance(reasoning, str) and bool(reasoning),
                    "reasoning_content_length": len(reasoning) if isinstance(reasoning, str) else 0,
                }
            except urllib.error.HTTPError as exc:
                try:
                    detail = _safe_api_error(exc.read(8192))
                except Exception:
                    detail = "error_body_unavailable"
                last = f"HTTP {exc.code}; {detail}"
                retryable = exc.code in {408, 409, 425, 429} or 500 <= exc.code < 600
                if not retryable or n == attempts:
                    break
                retry = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = min(float(retry), 30) if retry else min(2**n, 8)
                except ValueError:
                    delay = min(2**n, 8)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, ConfigError) as exc:
                last = f"{type(exc).__name__}: {str(exc)[:400]}"
                if n < attempts:
                    time.sleep(min(2**n, 8))
        raise ConfigError(f"DeepSeek 请求失败（未记录密钥、Prompt、响应正文或思维链）：{last}")

    # ---- 实现抽象接口（供通用调用方使用）----
    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        system = "\n".join(m.content for m in messages if m.role == "system")
        user = "\n".join(m.content for m in messages if m.role == "user")
        value, _ = self.json_completion(
            system,
            user,
            temperature=temperature if temperature is not None else 0.35,
            max_tokens=max_tokens or 16000,
        )
        return json.dumps(value, ensure_ascii=False)

    def chat_json(self, messages: list[ChatMessage], **kw: Any) -> dict[str, Any]:
        return parse_json_content(self.chat(messages, json_mode=True, **kw))
