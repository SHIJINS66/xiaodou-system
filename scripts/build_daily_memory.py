#!/usr/bin/env python3
"""build_daily_memory — 等价迁移自旧 step04-releases/v1.0.5-src/bin/build_daily_memory.py。

生成一天融合后的 daily memory（供 MEMORY.md 与后续规划的上下文）。由
finalize 编排脚本调用（本模块只提供纯逻辑，不含 CLI）。

等价迁移保留的线上逻辑（逐条对齐）：
    - SECTIONS：8 个栏目（结构 key 保留线上值，如 companion_responses 不改，
      因为 evidence_bundle / memory_model_output schema 引用它）
    - generation_profiles：三档（normal / repair / conflict），thinking 分层，
      由 settings.models.memory.profiles 驱动；memory_recovery_mode 时单档 recovery
    - 先 strict_check（schema + evidence 引用 + quality 门）通过即返回；
      否则 sanitize 回退（文本/引用清洗后再度严格校验）
    - 多档失败后抛 MemoryGenerationError（含 attempts 明细）
    - render：把 value 渲染成 Markdown，标题由 settings.memory.section_templates 驱动，
      不再写死角色名/陪伴对象名，改用 settings 占位

适配点（相对线上）：
    - 错误类型统一 StepError（本模块内保留 MemoryGenerationError 子类）
    - schema 随包定位；时区从 settings
    - speaker 占位符 {character_name}/{companion_name} 在调用 provider 前
      用 settings 真实值替换（prompt 渲染机制与静态 prompt 一致）
    - provider 走 providers 工厂（可 deepseek 原生或 openai_compatible）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Protocol

from common import StepError, bootstrap, load_json, package_root, render_prompt, schema_path
from memory_quality import QUALITY_GATE_VERSION, model_payload, validate_quality
from schema_validator import validate_daily  # noqa: F401  (保持与线上一致的依赖注入)

# 结构 key 与人类可读标题。标题来自 settings.memory.section_templates，
# render 时渲染；这里仅作默认/占位。
SECTIONS = [
    ("actual_life", "actual_life_title"),
    ("proactive_shares", "proactive_shares_title"),
    ("companion_responses", "companion_responses_title"),
    ("important_conversations", "important_conversations_title"),
    ("emotional_and_relationship_notes", "emotional_notes_title"),
    ("unresolved_items", "unresolved_items_title"),
    ("tomorrow_implications", "tomorrow_implications_title"),
    ("long_term_memory_candidates", "long_term_candidates_title"),
]

# 线上写死的标题（等价迁移登记的默认，供无模板配置时兜底；不改结构 key）
_DEFAULT_TITLES = {
    "actual_life_title": "当天实际生活",
    "proactive_shares_title": "主动分享和自拍",
    "companion_responses_title": "陪伴对象的反应",
    "important_conversations_title": "重要对话",
    "emotional_notes_title": "情绪和关系",
    "unresolved_items_title": "未解决事项",
    "tomorrow_implications_title": "对明天的影响",
    "long_term_candidates_title": "长期记忆候选",
}


class MemoryGenerationError(StepError):
    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


def _allowed_evidence(evidence: dict[str, Any]) -> set[str]:
    return {
        item["evidence_id"]
        for collection in (evidence.get("messages", []), evidence.get("event_outcomes", []))
        for item in collection
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }


def _strict_check(
    value: dict[str, Any],
    schema: Path,
    allowed: set[str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    from schema_tools import validate

    validate(value, schema)
    for key, _ in SECTIONS:
        for item in value[key]:
            if not set(item["evidence_ids"]) <= allowed:
                raise StepError("memory references unknown evidence")
    return validate_quality(value, evidence)


def _safe_normalize(value: Any, allowed: set[str]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, Any] = {}
    for key, _ in SECTIONS:
        rows = source.get(key)
        kept: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                refs = item.get("evidence_ids")
                if not isinstance(text, str) or not text.strip() or len(text) > 800:
                    continue
                if not isinstance(refs, list):
                    continue
                clean_refs = []
                for ref in refs:
                    if isinstance(ref, str) and ref in allowed and ref not in clean_refs:
                        clean_refs.append(ref)
                if not clean_refs:
                    continue
                kept.append({"text": text.strip(), "evidence_ids": clean_refs[:20]})
                if len(kept) >= 12:
                    break
        normalized[key] = kept
    return normalized


def _has_unknown_references(value: Any, allowed: set[str]) -> bool:
    if not isinstance(value, dict):
        return False
    for key, _ in SECTIONS:
        rows = value.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            refs = item.get("evidence_ids")
            if isinstance(refs, list) and any(
                isinstance(ref, str) and ref not in allowed for ref in refs
            ):
                return True
    return False


def _error_summary(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:800] or 'no detail'}"


def _failure_record(exc: Exception, profile: dict[str, Any], attempt: int) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "request_profile": profile["name"],
        "thinking_type": profile["thinking_type"],
        "reasoning_effort": profile["reasoning_effort"],
        "max_tokens": profile["max_tokens"],
        "status": "failed",
        "error_type": getattr(exc, "error_type", "validation_or_quality"),
        "finish_reason": getattr(exc, "finish_reason", None),
        "reasoning_content_present": bool(getattr(exc, "reasoning_content_present", False)),
        "reasoning_content_length": int(getattr(exc, "reasoning_content_length", 0) or 0),
        "error_summary": _error_summary(exc),
    }


def generation_profiles(settings: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    """三档 profile 由 settings.models.memory.profiles 驱动（等价 migration）。"""
    if config.get("memory_recovery_mode") is True:
        return [
            {
                "name": "recovery_nonthinking_large",
                "thinking_type": "disabled",
                "reasoning_effort": None,
                "max_tokens": int(config.get("memory_recovery_max_tokens", 16384)),
            }
        ]
    profiles_cfg = (settings.get("models.memory.profiles") or {}) if settings else {}
    # 兼容两种形态：list 或 dict(name->…)
    profile_list: list[dict[str, Any]]
    if isinstance(profiles_cfg, dict):
        profile_list = list(profiles_cfg.values())
    else:
        profile_list = list(profiles_cfg)
    if not profile_list:
        # 默认三档（等价线上）
        profile_list = [
            {"name": "normal", "thinking": "disabled", "reasoning_effort": None, "max_tokens": 16384},
            {"name": "repair", "thinking": "disabled", "reasoning_effort": None, "max_tokens": 16384},
            {"name": "conflict", "thinking": "enabled", "reasoning_effort": "high", "max_tokens": 32768},
        ]
    profiles = [
        {
            "name": p.get("name", f"p{i}"),
            "thinking_type": p.get("thinking", "disabled"),
            "reasoning_effort": p.get("reasoning_effort"),
            "max_tokens": int(p.get("max_tokens", 16384)),
            "repair_instruction": p.get("repair_instruction"),
        }
        for i, p in enumerate(profile_list)
    ]
    limit = int(config.get("memory_generation_attempts", 3))
    if limit < 1 or limit > len(profiles):
        raise StepError("memory_generation_attempts must be between 1 and 3")
    return profiles[:limit]


def _profiled_config(config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    effective = dict(config)
    effective.update(
        memory_request_profile=profile["name"],
        memory_thinking_type=profile["thinking_type"],
        memory_reasoning_effort=profile["reasoning_effort"],
        memory_max_tokens=profile["max_tokens"],
    )
    return effective


class _Requester(Protocol):
    def __call__(
        self, config: dict[str, Any], api_key: str, prompt: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ...


def _make_requester(settings: Any) -> _Requester:
    """把通用 LLM provider 包装成线上 request_json 签名，供 generate 复用。"""
    import os
    from providers.base import ChatMessage, create_llm_provider

    provider = create_llm_provider(settings, which="caption")  # memory 记忆生成复用 caption 的 LLM 连接配置
    conf = settings.get("models.caption") or settings.get("models.memory") or {}
    api_key_env = conf.get("api_key_env", "DEEPSEEK_API_KEY")

    def _req(config: dict[str, Any], api_key: str, prompt: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        from providers.base import ChatMessage  # noqa

        # 用 provider 配置里的 api_key_env（优先），fallback 传入的 api_key
        effective_key = os.environ.get(api_key_env) or api_key
        # 注入会话上下文到环境中（provider 从 env 读 key）
        os.environ[api_key_env] = effective_key
        has_completion = getattr(provider, "json_completion", None)
        if has_completion is not None:
            value, meta = has_completion(
                prompt,
                json.dumps(payload, ensure_ascii=False),
                temperature=0.2,
                max_tokens=conf.get("max_tokens", 5000) or 5000,
            )
            meta = dict(meta or {})
            meta["thinking_type"] = config.get("memory_thinking_type")
            meta["reasoning_effort"] = config.get("memory_reasoning_effort")
            meta["max_tokens"] = config.get("memory_max_tokens")
            return value, meta
        value = provider.chat_json(
            [ChatMessage("system", prompt), ChatMessage("user", json.dumps(payload, ensure_ascii=False))],
            temperature=0.2,
            max_tokens=conf.get("max_tokens", 5000) or 5000,
        )
        return value, {}

    return _req


def generate(
    settings: Any,
    config: dict[str, Any],
    api_key: str,
    evidence: dict[str, Any],
    requester: _Requester | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成融合记忆。requester 缺省时用通用 provider 包装。"""
    if requester is None:
        requester = _make_requester(settings)

    prompt_template = Path(config["memory_prompt_path"]).read_text(encoding="utf-8").strip()
    if not prompt_template:
        raise StepError("memory prompt is empty")
    # 渲染 prompt 占位符（character_name / companion_key …）
    prompt = render_prompt(prompt_template, settings)

    schema = Path(config["memory_schema_path"])
    allowed = _allowed_evidence(evidence)
    payload = _resolve_speakers(model_payload(evidence), settings)
    failures: list[dict[str, Any]] = []
    last: Exception | None = None
    last_value: Any = None

    for attempt, profile in enumerate(generation_profiles(settings, config), start=1):
        repair = ""
        instr = profile.get("repair_instruction")
        if instr:
            repair = render_prompt(_format_repair(instr, last), settings)
        elif profile["name"] == "repair_nonthinking":
            repair = _default_repair(last)
        elif profile["name"] == "conflict_high":
            repair = _default_conflict(failures)
        try:
            value, usage = requester(
                _profiled_config(config, profile),
                api_key,
                prompt + repair,
                payload,
            )
            last_value = value
            quality = _strict_check(value, schema, allowed, evidence)
            usage = dict(usage or {})
            usage["attempt"] = attempt
            usage["attempt_history"] = failures + [
                {
                    "attempt": attempt,
                    "request_profile": profile["name"],
                    "thinking_type": profile["thinking_type"],
                    "reasoning_effort": profile["reasoning_effort"],
                    "max_tokens": profile["max_tokens"],
                    "status": "passed",
                    "finish_reason": usage.get("finish_reason"),
                }
            ]
            usage["deterministic_sanitization"] = False
            usage["quality_report"] = quality
            usage["quality_gate_version"] = QUALITY_GATE_VERSION
            usage["model_input"] = {
                "canonical_messages": len(payload["canonical_messages"]),
                "event_outcomes": len(payload["event_outcomes"]),
            }
            return value, usage
        except Exception as exc:
            last = exc
            failure = _failure_record(exc, profile, attempt)
            failures.append(failure)
            if failure["error_type"] in {"request_error", "length_empty", "empty_content"}:
                break
            if profile["name"] == "repair_nonthinking" and failure["error_type"] not in {
                "validation_or_quality",
                "invalid_json",
                "invalid_json_root",
            }:
                break

    if last_value is not None and not _has_unknown_references(last_value, allowed):
        normalized = _safe_normalize(last_value, allowed)
        try:
            quality = _strict_check(normalized, schema, allowed, evidence)
            return normalized, {
                "attempt": len(failures) + 1,
                "attempt_history": failures + [
                    {
                        "attempt": len(failures) + 1,
                        "request_profile": "deterministic_sanitization",
                        "thinking_type": None,
                        "reasoning_effort": None,
                        "max_tokens": None,
                        "status": "passed",
                    }
                ],
                "deterministic_sanitization": True,
                "last_validation_error": _error_summary(last or StepError("unknown")),
                "quality_report": quality,
                "quality_gate_version": QUALITY_GATE_VERSION,
                "model_input": {
                    "canonical_messages": len(payload["canonical_messages"]),
                    "event_outcomes": len(payload["event_outcomes"]),
                },
            }
        except Exception as exc:
            last = exc
            failures.append(
                {
                    "attempt": len(failures) + 1,
                    "request_profile": "deterministic_sanitization",
                    "thinking_type": None,
                    "reasoning_effort": None,
                    "max_tokens": None,
                    "status": "failed",
                    "error_type": "quality_gate",
                    "finish_reason": None,
                    "reasoning_content_present": False,
                    "reasoning_content_length": 0,
                    "error_summary": _error_summary(exc),
                }
            )

    raise MemoryGenerationError(
        f"memory generation exhausted {len(failures)} distinct strategies: "
        f"{_error_summary(last or StepError('unknown'))}",
        failures,
    )


def _resolve_speakers(payload: dict[str, Any], settings: Any) -> dict[str, Any]:
    """把 model_payload 里的 {character_name}/{companion_name} 占位替换为 settings 值。"""
    character = (settings.character or {}) if settings else {}
    companion = (settings.companion or {}) if settings else {}
    cname = character.get("name") or ""
    ckey = companion.get("key") or "companion"
    mapping = {"character_name": cname, "companion_name": ckey}
    text = json_dumps_placeholder(payload)
    for k, v in mapping.items():
        text = text.replace("{" + k + "}", str(v))
    import json as _json

    return _json.loads(text)


def json_dumps_placeholder(payload: dict[str, Any]) -> str:
    import json as _json

    return _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _format_repair(instr: str, last: Exception | None) -> str:
    return instr.replace("{error_summary}", _error_summary(last or StepError("unknown")))


def _default_repair(last: Exception | None) -> str:
    return (
        "\n\n这是一次定向修复。上次输出未通过校验："
        + _error_summary(last or StepError("unknown"))
        + "\n请重新通读全部 canonical_messages。修复要点："
        + "（1）确保每条是对象而非字符串；（2）evidence_ids 必须来自输入且跨双方覆盖；"
        + "（3）合并同一连续情节，不逐句转写；（4）同一话题只选一个最合适的栏目。"
        + "只返回严格 JSON，不解释。"
    )


def _default_conflict(failures: list[dict[str, Any]]) -> str:
    return (
        "\n\n这是最后一次冲突处理。此前失败记录："
        + " | ".join(item["error_summary"] for item in failures[-2:])
        + "\n仅在证据存在冲突时谨慎判断；不确定时保守表述，不得虚构。"
        + "必须给最终 JSON 正文留足输出空间，只返回严格 JSON。"
    )


def render(date: str, value: dict[str, Any], settings: Any) -> str:
    """渲染 daily memory Markdown；标题由 settings.memory.section_templates 驱动。"""
    templates = (settings.memory or {}).get("section_templates", {}) if settings else {}
    title_template = (settings.memory or {}).get("title_template", "# {character_name} Daily Memory — {date}") if settings else "# Daily Memory — {date}"
    character = (settings.character or {}) if settings else {}
    cname = character.get("name") or ""
    title = title_template.replace("{character_name}", cname).replace("{date}", date)
    lines = [title, ""]

    # 结构 key 有序映射到标题 key；用 settings 模板 + 兜底默认
    for section, title_key in SECTIONS:
        tpl = templates.get(title_key) if isinstance(templates, dict) else None
        heading = tpl or _DEFAULT_TITLES[title_key]
        heading = heading.replace("{character_name}", cname).replace("{companion_key}",
            (settings.companion or {}).get("key", "companion") if settings else "companion")
        lines += [f"## {heading}"]
        lines += [
            "- " + item["text"] + " <!-- evidence:" + ",".join(item["evidence_ids"]) + " -->"
            for item in value[section]
        ] or ["- 无。"]
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"
