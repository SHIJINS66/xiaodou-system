#!/usr/bin/env python3
"""providers/openclaw_gateway — 等价迁移自旧 step03/bin/providers/openclaw_gateway.py。

OpenClaw Gateway 交互（读取当前 session 历史、解析 sessionId、chat.inject 注入）。

当前迁移范围（等价迁移的分阶段边界）：
    - 已迁：纯逻辑部分 —— SessionHistory 数据类、message_text / message_id /
      contains_marker / _needs_full_message 等无网络依赖的提取与判定
    - 未迁（标注为后置）：真实 _call / session_id / current_session_history /
      get_message / inject —— 它们调用系统 openclaw CLI（真实网关），
      在"接真实 provider 端到端"阶段迁移并隔离测试

去硬编码：marker 正则与实例前缀由 common.internal_marker_regex 派生；
openclaw CLI 路径 / session_key 走 settings（scheduling/gateway 段）。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import StepError, openclaw_env

_PAGE_LIMIT = 500
_MAX_PAGES = 1000
_PLACEHOLDER_MARKERS = (
    "chat.history omitted",
    "message too large",
    "history omitted",
)


@dataclass(frozen=True)
class SessionHistory:
    session_id: str | None
    messages: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 纯逻辑部分（已迁，无网络依赖）
# ---------------------------------------------------------------------------

def message_text(message: dict[str, Any]) -> str:
    content = message.get("content", message.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def message_id(message: dict[str, Any]) -> str | None:
    """Return the live history-row identifier without exposing it in logs."""
    for key in ("id", "messageId", "message_id"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = message.get("__openclaw")
    if isinstance(metadata, dict):
        for key in ("id", "messageId", "message_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def contains_marker(messages: list[dict[str, Any]], marker: str) -> bool:
    return any(marker in message_text(item) for item in messages)


def _needs_full_message(message: dict[str, Any]) -> bool:
    if message.get("truncated") is True or message.get("omitted") is True:
        return True
    role = message.get("role")
    text = message_text(message).strip().lower()
    if role in {"user", "assistant"} and not text and message_id(message) is not None:
        return True
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


# ---------------------------------------------------------------------------
# 真实 Gateway 调用（后置阶段迁移；此处保留结构供 endpoint 接入）
# ---------------------------------------------------------------------------

def _call(settings, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """调用系统 openclaw gateway。settings.scheduling.gateway 提供路径/session_key。"""
    gw = settings.get("scheduling.gateway") or {}
    command = [
        gw.get("openclaw_bin") or "/usr/bin/openclaw",
        "gateway",
        "call",
        method,
        "--params",
        json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        "--json",
    ]
    # 显式指定 gateway 实例（可选）：连任意 OpenClaw 实例，缺省由 CLI 依据 local loopback 推断。
    ws_url = gw.get("ws_url") or settings.get("scheduling.gateway.ws_url")
    if ws_url:
        command += ["--url", str(ws_url)]
        # --url 强制要求显式凭据，传 --token 从 auth_token_env 对应 env 读。
        token_env = gw.get("auth_token_env") or ""
        token = token_env and os.environ.get(token_env) or ""
        if token:
            command += ["--token", token]
    env = openclaw_env(settings)
    # 若配置了 gateway token 环境变量名，把对应值合入子进程 env（密不落 settings 明值）。
    # 默认本地 CLI 免 token（与线上一致）；开了 gateway.auth.token 时靠环境注入授权。
    token_env = gw.get("auth_token_env") or ""
    if token_env and os.environ.get(token_env):
        env[token_env] = os.environ[token_env]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=int(gw.get("openclaw_timeout_seconds", 60)),
            check=False,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise StepError(f"Gateway call unavailable: {method}") from exc
    if result.returncode != 0:
        raise StepError(f"Gateway call failed: {method}, code={result.returncode}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StepError(f"Gateway returned invalid JSON: {method}") from exc
    if not isinstance(parsed, dict):
        raise StepError(f"Gateway result is not object: {method}")
    return parsed


def _result(parsed: dict[str, Any]) -> dict[str, Any]:
    value = parsed.get("result")
    return value if isinstance(value, dict) else parsed


def _row_key(row: dict[str, Any]) -> str | None:
    value = row.get("key") or row.get("sessionKey") or row.get("session_key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _row_session_id(row: dict[str, Any]) -> str | None:
    value = row.get("sessionId") or row.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def get_session(settings) -> dict[str, Any]:
    gw = settings.get("scheduling.gateway") or {}
    rows = _session_rows(_call(settings, "sessions.list", {}))
    matches = [row for row in rows if _row_key(row) == gw.get("session_key")]
    if len(matches) != 1:
        if not matches:
            raise StepError("configured session_key is absent from sessions.list")
        raise StepError("sessions.list contains duplicate configured session_key rows")
    if _row_session_id(matches[0]) is None:
        raise StepError("sessions.list row does not contain sessionId")
    return matches[0]


def session_id(settings) -> str:
    value = _row_session_id(get_session(settings))
    if value is None:
        raise StepError("current sessionId is missing")
    return value


def _session_rows(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    result = _result(parsed)
    candidates = [result.get("sessions"), parsed.get("sessions")]
    rows = next((value for value in candidates if isinstance(value, list)), None)
    if rows is None or not all(isinstance(item, dict) for item in rows):
        raise StepError("unrecognized sessions.list response shape")
    return rows


def _history_rows(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str | None, str | None]:
    result = _result(parsed)
    candidates = [
        result.get("sessions"),
        parsed.get("sessions"),
        result.get("messages"),
        parsed.get("messages"),
    ]
    rows = next((value for value in candidates if isinstance(value, list)), None)
    if rows is None or not all(isinstance(item, dict) for item in rows):
        raise StepError("unrecognized chat.history response shape")
    has_more = result.get("hasMore") if "hasMore" in result else parsed.get("hasMore", False)
    next_offset = result.get("nextOffset") if "nextOffset" in result else parsed.get("nextOffset")
    page_session = result.get("sessionId") or parsed.get("sessionId")
    if not isinstance(has_more, bool):
        raise StepError("chat.history hasMore must be boolean")
    if next_offset is not None and not isinstance(next_offset, (str, int)):
        raise StepError("chat.history nextOffset has invalid type")
    return rows, has_more, str(next_offset) if next_offset is not None else None, str(page_session) if page_session else None


def current_session_history(settings) -> SessionHistory:
    """读取当前 session 的全量历史（分页）。（端到端阶段迁移验证。）"""
    expected_session_id = session_id(settings)
    offset: str | None = None
    seen_offsets: set[str] = set()
    seen_message_ids: set[str] = set()
    messages: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"sessionKey": (settings.get("scheduling.gateway") or {}).get("session_key"), "limit": _PAGE_LIMIT}
        if offset is not None:
            params["offset"] = offset
        rows, has_more, next_offset, page_session = _history_rows(_call(settings, "chat.history", params))
        if page_session and page_session != expected_session_id:
            raise StepError("chat.history sessionId changed during pagination")
        for row in rows:
            messages.append(row)
        if not has_more:
            break
        if next_offset is None or next_offset in seen_offsets:
            raise StepError("chat.history pagination did not advance")
        seen_offsets.add(next_offset)
        offset = next_offset
    if session_id(settings) != expected_session_id:
        raise StepError("active session changed while history was being read")
    return SessionHistory(session_id=expected_session_id, messages=messages)


def inject(settings, message: str) -> dict[str, Any]:
    """chat.inject 把已发送文案写入目标 session。（端到端阶段迁移验证。）"""
    parsed = _call(settings, "chat.inject", {"sessionKey": (settings.get("scheduling.gateway") or {}).get("session_key"), "message": message})
    result = _result(parsed)
    if result.get("ok") is not True:
        raise StepError("chat.inject did not return ok=true")
    injected_id = result.get("messageId") or result.get("message_id") or result.get("id")
    return {"message_id": str(injected_id) if injected_id is not None else None, "response": parsed}
