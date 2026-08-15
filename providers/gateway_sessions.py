#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from common import StepError, openclaw_env

RUNNING_STATUSES = {
    "starting",
    "queued",
    "running",
    "processing",
    "cancelling",
    "aborting",
}


def call(config: dict[str, Any], method: str, params: dict[str, Any]) -> dict[str, Any]:
    command = [
        config["openclaw_bin"],
        "gateway",
        "call",
        method,
        "--params",
        json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        "--json",
        "--timeout",
        str(int(config.get("openclaw_timeout_seconds", 60)) * 1000),
    ]
    # 显式指定 gateway 实例（可选）：连任意 OpenClaw 实例，缺省由 CLI 依据 local loopback 推断。
    ws_url = config.get("ws_url")
    if ws_url:
        command += ["--url", str(ws_url)]
        # --url 强制要求显式凭据（OpenClaw 安全设计），传 --token 从 auth_token_env 对应 env 读。
        token_env = config.get("auth_token_env") or ""
        token = token_env and os.environ.get(token_env) or ""
        if token:
            command += ["--token", token]
    try:
        env = openclaw_env(config['_settings']) if isinstance(config.get('_settings'), dict) or hasattr(config.get('_settings'), 'get') else {}
        # token 授权：若配置了 auth_token_env 且环境里有值，注入子进程 env（CLI 会读它）。
        token_env = config.get("auth_token_env")
        if token_env:
            from os import environ
            if environ.get(token_env):
                env[token_env] = environ[token_env]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=int(config.get("openclaw_timeout_seconds", 60)) + 15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
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


def result_object(parsed: dict[str, Any]) -> dict[str, Any]:
    result = parsed.get("result")
    return result if isinstance(result, dict) else parsed


def _session_rows(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str | None]:
    result = result_object(parsed)
    rows = result.get("sessions") if isinstance(result.get("sessions"), list) else parsed.get("sessions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise StepError("unrecognized sessions.list response shape")
    has_more = result.get("hasMore") if "hasMore" in result else parsed.get("hasMore", False)
    next_offset = result.get("nextOffset") if "nextOffset" in result else parsed.get("nextOffset")
    if not isinstance(has_more, bool):
        raise StepError("sessions.list hasMore must be boolean")
    if next_offset is not None and not isinstance(next_offset, (str, int)):
        raise StepError("sessions.list nextOffset has invalid type")
    return rows, has_more, str(next_offset) if next_offset is not None else None


def _row_key(row: dict[str, Any]) -> str | None:
    value = row.get("key") or row.get("sessionKey") or row.get("session_key")
    return value.strip() if isinstance(value, str) and value.strip() else None


def row_session_id(row: dict[str, Any]) -> str:
    value = row.get("sessionId") or row.get("session_id")
    if not isinstance(value, str) or not value.strip():
        raise StepError("sessions.list row does not contain sessionId")
    return value.strip()


def list_sessions(config: dict[str, Any]) -> list[dict[str, Any]]:
    offset: str | None = None
    seen_offsets: set[str] = set()
    rows: list[dict[str, Any]] = []
    max_pages = min(int(config.get("max_pages", 100)), 1000)
    for _ in range(max_pages):
        params: dict[str, Any] = {}
        if offset is not None:
            params["offset"] = offset
        page, has_more, next_offset = _session_rows(call(config, "sessions.list", params))
        rows.extend(page)
        if not has_more:
            return rows
        if next_offset is None or next_offset in seen_offsets:
            raise StepError("sessions.list pagination did not advance")
        seen_offsets.add(next_offset)
        offset = next_offset
    raise StepError("sessions.list exceeded pagination safety limit")


def get_session(config: dict[str, Any]) -> dict[str, Any]:
    matches = [row for row in list_sessions(config) if _row_key(row) == config["session_key"]]
    if len(matches) != 1:
        if not matches:
            raise StepError("configured session_key is absent from sessions.list")
        raise StepError("sessions.list contains duplicate configured session_key rows")
    row_session_id(matches[0])
    return matches[0]


def session_is_active(row: dict[str, Any]) -> bool:
    if row.get("hasActiveRun") is True:
        return True
    status = row.get("status")
    return isinstance(status, str) and status.strip().lower() in RUNNING_STATUSES


def reset_session(config: dict[str, Any]) -> dict[str, Any]:
    parsed = call(
        config,
        "sessions.reset",
        {"key": config["session_key"], "reason": "reset"},
    )
    result = result_object(parsed)
    if result.get("ok") is not True:
        raise StepError("sessions.reset did not return ok=true")
    key = result.get("key")
    if isinstance(key, str) and key != config["session_key"]:
        raise StepError("sessions.reset returned a different session key")
    entry = result.get("entry")
    if not isinstance(entry, dict):
        raise StepError("sessions.reset response does not contain entry")
    new_session_id = entry.get("sessionId") or entry.get("session_id")
    if not isinstance(new_session_id, str) or not new_session_id.strip():
        raise StepError("sessions.reset response does not contain new sessionId")
    return {
        "ok": True,
        "key": config["session_key"],
        "session_id": new_session_id.strip(),
        "response": parsed,
    }
