#!/usr/bin/env python3
"""event_decision — 等价迁移自旧 step03/bin/event_decision.py。

晨间 pipeline 里，对 planner 生成的每个事件做"执行/延迟/加运行时事件/跳过"决策，
并施加确定性边界（模型不能覆盖 sleep / DND / 超范围 delay）。

等价迁移保留的线上行为：
    - 用决策 prompt + repair 循环（模型输出结构不过 schema 就带修复提示重试）
    - 结构校验：决策 schema + 运行时事件 schema（add_runtime_event 时）
    - deterministic_accept：delay_minutes 边界、add_runtime_event 必填、
      execute 不可覆盖 do-not-disturb

适配点：
    - provider 走框架 LLM 工厂（路由到 DeepSeek / OpenRouter 等），不再走旧
      providers/deepseek_client 的硬编码 endpoint
    - prompt/schema 相对包定位，不再写 config 里的绝对路径
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import ConfigError, StepError, package_root, prompt_path
from schema_tools import validate
from providers.base import create_llm_provider


def decide(settings, api_key: str, event: dict[str, Any], segment: dict[str, Any], snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """对一个事件做决策。返回 (value, usage)。"""
    provider = create_llm_provider(settings, which="decision")
    prompt = prompt_path(package_root(__file__), "event_decision_v1").read_text(encoding="utf-8")
    decision_schema = package_root(__file__) / "schemas" / "runtime_decision_v1.schema.json"
    runtime_request_schema = package_root(__file__) / "schemas" / "runtime_event_request_v1.schema.json"

    last: Exception | None = None
    for attempt in range(int(settings.get("planning.repairs.max_attempts", 2)) + 1):
        try:
            suffix = "" if attempt == 0 else (
                "\nPrevious output failed structural validation. Return only a corrected JSON object "
                "matching the exact schema."
            )
            value, usage = provider.json_completion(
                prompt + suffix,
                _dump({"event": event, "segment": segment, "context": snapshot}),
                temperature=settings.get("models.decision.temperature", 0.2),
                max_tokens=settings.get("models.decision.max_tokens", 1500),
            )
            validate(value, decision_schema)
            if value["action"] == "add_runtime_event":
                validate(value["runtime_event"], runtime_request_schema)
            deterministic_accept(value, event, snapshot)
            usage["repair_attempt"] = attempt
            return value, usage
        except (StepError, ValueError, KeyError, TypeError) as exc:
            last = exc
    raise StepError(f"decision JSON failed after repair limit: {type(last).__name__}")


def deterministic_accept(value: dict[str, Any], event: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """确定性边界：允许模型在约束内决策，但关键安全红线不可覆盖。"""
    action = value["action"]
    if action == "delay":
        minutes = value.get("delay_minutes")
        if not isinstance(minutes, int) or minutes < 5 or minutes > 120:
            raise ValueError("delay_minutes outside deterministic bounds")
    if action == "add_runtime_event" and not value.get("runtime_event"):
        raise ValueError("runtime_event required")
    if action == "execute" and snapshot["do_not_disturb"]:
        raise ValueError("model cannot override do-not-disturb")


def _dump(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
