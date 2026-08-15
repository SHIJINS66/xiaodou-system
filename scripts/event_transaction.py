#!/usr/bin/env python3
"""event_transaction — 等价迁移自旧 step03/bin/event_transaction.py。

执行事件的事务状态机：记录 execution 生命周期（acquired → gated → decision_ready
→ prepared → telegram_sending → telegram_sent → injecting → injected → completed，
及失败/取消/跳过/投递未知等终止态）。全部转场经 ALLOWED 校验 + schema 校验 +
原子写。

等价迁移：
    - 状态转场图 ALLOWED 逐条保留
    - transaction_path 布局 <state_root>/events/<date>/<event_id>/transaction.json 保留
    - execution_id 用 common.execution_id(settings,...)（对应线上 stable_execution_id，
      前缀 xd03- 换为实例名派生）
    - schema 路径由调用方传入（不再从包内臆测）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import StepError, atomic_write_json, execution_id, load_json, now_iso
from schema_tools import validate

ALLOWED: dict[str, set[str]] = {
    "acquired": {"gated", "cancelled", "skipped", "failed"},
    "gated": {"decision_ready", "cancelled", "skipped", "failed"},
    "decision_ready": {"prepared", "delayed", "cancelled", "failed"},
    "prepared": {"telegram_sending", "cancelled", "failed"},
    "telegram_sending": {"telegram_sent", "delivery_unknown", "failed"},
    "telegram_sent": {"injecting", "completed", "injection_pending"},
    "injecting": {"injected", "injection_pending", "injection_unknown"},
    "injection_pending": {"injecting", "injected"},
    "injection_unknown": {"injecting", "injected"},
    "injected": {"completed"},
    "delayed": {"acquired"},
    "cancelled": set(),
    "skipped": set(),
    "failed": set(),
    "delivery_unknown": set(),
    "completed": set(),
}


def transaction_path(state_root: Path, date: str, event_id: str) -> Path:
    return state_root / "events" / date / event_id / "transaction.json"


def create(settings, path: Path, schema_path: Path, daily_path: Path, date: str, event_id: str) -> dict[str, Any]:
    value = {
        "schema_version": "1.0",
        "execution_id": execution_id(settings, daily_path, event_id),
        "date": date,
        "event_id": event_id,
        "session_id": None,
        "phase": "acquired",
        "attempt_count": 1,
        "delay_count": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "decision": None,
        "decision_reason": None,
        "prepared": {"text": None, "text_sha256": None, "image_path": None, "image_sha256": None, "transcript": None},
        "telegram": {"sent": False, "message_id": None, "sent_at": None},
        "injection": {"injected": False, "message_id": None, "injected_at": None, "marker": None},
        "error": None,
        "history": [{"at": now_iso(), "from_phase": None, "to_phase": "acquired", "reason": "execution lock acquired"}],
    }
    validate(value, schema_path)
    atomic_write_json(path, value)
    return value


def load_or_create(settings, path: Path, schema_path: Path, daily_path: Path, date: str, event_id: str) -> dict[str, Any]:
    if not path.exists():
        return create(settings, path, schema_path, daily_path, date, event_id)
    value = load_json(path)
    validate(value, schema_path)
    expected = execution_id(settings, daily_path, event_id)
    if value["date"] != date or value["event_id"] != event_id or value["execution_id"] != expected:
        raise StepError("transaction identity mismatch")
    return value


def transition(path: Path, schema_path: Path, value: dict[str, Any], phase: str, reason: str, **updates: Any) -> dict[str, Any]:
    current = value["phase"]
    if phase == current:
        return value
    if phase not in ALLOWED.get(current, set()):
        raise StepError(f"illegal transaction transition: {current} -> {phase}")
    value.update(updates)
    value["phase"] = phase
    value["updated_at"] = now_iso()
    value["history"].append({"at": value["updated_at"], "from_phase": current, "to_phase": phase, "reason": reason})
    validate(value, schema_path)
    atomic_write_json(path, value)
    return value


def save(path: Path, schema_path: Path, value: dict[str, Any]) -> dict[str, Any]:
    value["updated_at"] = now_iso()
    validate(value, schema_path)
    atomic_write_json(path, value)
    return value
