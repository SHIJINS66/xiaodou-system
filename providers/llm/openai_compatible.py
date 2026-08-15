#!/usr/bin/env python3
"""OpenAI 兼容 LLM provider（覆盖 DeepSeek / Kimi / GPT 等）。

从旧 step02/bin/providers/deepseek_client.py + step03 的 deepseek_client 迁入，
去掉 "thinking=enabled 强校验" 这类 deepseek 专属写死，改为可配置透传。
"""
from __future__ import annotations

import json
import os
from typing import Any

from providers.base import ChatMessage, LLMProvider, register_llm
from scripts.settings_loader import ConfigError


def _parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines[1:]).strip() if len(lines) > 1 else text
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ConfigError("模型输出根节点不是对象")
    return value


@register_llm("openai_compatible")
class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, settings: Any, conf: dict[str, Any]):
        self.conf = conf
        self.base_url = conf.get("base_url")
        self.model = conf.get("model")
        self.timeout = int(conf.get("timeout_seconds", 60))
        self.api_key_env = conf.get("api_key_env", "DEEPSEEK_API_KEY")
        self.extra_body = conf.get("extra_body")  # 如 deepseek 的 thinking 参数，可透传

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise ConfigError(f"{self.api_key_env} 未配置")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigError("缺少 openai 依赖；请运行 pip install -r requirements.txt") from exc

        client = OpenAI(base_url=self.base_url, api_key=api_key, timeout=self.timeout)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise ConfigError("模型返回空 content")
        return content

    def chat_json(self, messages: list[ChatMessage], **kw: Any) -> dict[str, Any]:
        return _parse_json_content(self.chat(messages, json_mode=True, **kw))

    def json_completion(
        self, system: str, user: str, temperature: float | None = None, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """便捷：一个 system + 一个 user，返回 (value, usage_meta)。

        与 deepseek_urllib 的 json_completion 签名对齐，供 event_decision /
        message_generation 等调用方用鸭子类型切换。
        """
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise ConfigError(f"{self.api_key_env} 未配置")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigError("缺少 openai 依赖") from exc
        client = OpenAI(base_url=self.base_url, api_key=api_key, timeout=self.timeout)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        effort = self.conf.get("reasoning_effort")
        if effort:
            kwargs["reasoning_effort"] = effort
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content
        reasoning = getattr(msg, "reasoning_content", None)
        if not content or not content.strip():
            raise ConfigError(f"模型返回空 content; finish_reason={choice.finish_reason}")
        value = _parse_json_content(content)
        usage = getattr(resp, "usage", None)
        meta = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "model": getattr(resp, "model", self.model),
            "thinking_type": (self.extra_body or {}).get("thinking", {}).get("type") if isinstance((self.extra_body or {}).get("thinking"), dict) else self.extra_body.get("thinking") if isinstance(self.extra_body, dict) else None,
            "reasoning_effort": effort,
            "finish_reason": choice.finish_reason,
            "reasoning_content_present": isinstance(reasoning, str) and bool(reasoning),
            "reasoning_content_length": len(reasoning) if isinstance(reasoning, str) else 0,
        }
        return value, meta
