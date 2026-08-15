#!/usr/bin/env python3
"""message_generation — 等价迁移自旧 step03/bin/message_generation.py。

对"已批准事件"生成最终文案（JSON），带修复循环（修复失败则重试）与 schema 校验。

等价迁移：
    - 读 message 模板 prompt（相对包定位），发 LLM，校验 message_schema，非空校验
    - model_repair_attempts 次重试，带"上次失败需改正"后缀
    - 复用 common.render_prompt 渲染 {character_name}/{companion_key} 占位
    - LLM 走 provider 工厂（which=message），json_completion 鸭子类型
      （deepseek_urllib 与 openai_compatible 都提供该便捷方法）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import StepError, bootstrap, package_root, prompt_path, render_prompt
from schema_tools import validate


def _request(settings, conf: dict[str, Any], system: str, payload: dict[str, Any], attrs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """经 provider 工厂完成一次 JSON 请求；返回 (value, usage)。"""
    from providers.base import create_llm_provider
    provider = create_llm_provider(settings, which="caption")  # 复用 caption 的 LLM 连接配置(setting 无 models.message)
    user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    has_completion = getattr(provider, "json_completion", None)
    if has_completion is not None:
        value, meta = has_completion(
            system,
            user,
            temperature=conf.get("temperature", 0.35),
            max_tokens=conf.get("max_tokens", 4000),
        )
        meta = dict(meta or {})
        meta.update(attrs)
        return value, meta
    from providers.base import ChatMessage
    value = provider.chat_json([ChatMessage("system", system), ChatMessage("user", user)], temperature=conf.get("temperature", 0.35), max_tokens=conf.get("max_tokens", 4000))
    return value, dict(attrs)


def generate(
    settings,
    api_key: str,
    event: dict[str, Any],
    segment: dict[str, Any],
    snapshot: dict[str, Any],
    conf: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    conf = conf or {}
    prompt_path_ = prompt_path(package_root(__file__), "message_generation_v1")
    prompt = render_prompt(prompt_path_.read_text(encoding="utf-8").strip(), settings)
    schema = package_root(__file__) / "schemas" / "generated_message_v1.schema.json"
    last: Exception | None = None
    attempts = int(conf.get("model_repair_attempts", 2)) + 1
    for attempt in range(attempts):
        try:
            suffix = "" if attempt == 0 else "\nPrevious output failed structural validation. Return only a corrected JSON object matching the exact schema."
            value, usage = _request(settings, conf, prompt + suffix, {"event": event, "segment": segment, "context": snapshot}, {"repair_attempt": attempt})
            validate(value, schema)
            text = value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise StepError("generated text is empty")
            value["text"] = text.strip()
            return value, usage
        except (StepError, ValueError, KeyError) as exc:
            last = exc
    raise StepError(f"message JSON failed after repair limit: {type(last).__name__}")
