#!/usr/bin/env python3
"""deterministic_gate — 等价迁移自旧 step03/bin/deterministic_gate.py。

执行事件的确定性硬门（Gate 1 / Gate 2）。不依赖任何外部 API，纯逻辑：
    - 事件状态未终结
    - 未被标记为已发送 Telegram
    - 非 silent 事件
    - 时间窗已打开（earliest_at）且未过期（latest_at）
    - 会话为非打扰状态（do_not_disturb）
    - 未被 superseded

等价迁移：逻辑逐条保留，仅时区默认值由 common 兜底。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from common import _DEFAULT_TZ, parse_iso

TERMINAL_EVENT_STATES = {"completed", "cancelled", "failed", "skipped"}


def evaluate(
    event: dict[str, Any],
    state: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(_DEFAULT_TZ)).astimezone(_DEFAULT_TZ)
    if state["status"] in TERMINAL_EVENT_STATES:
        return {"allowed": False, "code": "event_terminal", "reason": f"event status is {state['status']}"}
    if state.get("telegram_sent"):
        return {"allowed": False, "code": "telegram_already_sent", "reason": "daily records Telegram success"}
    if event.get("type") == "silent":
        return {"allowed": False, "code": "silent_event", "reason": "silent event has no outbound action"}
    earliest = parse_iso(event["time_window"]["earliest_at"])
    latest = parse_iso(event["time_window"]["latest_at"])
    if current < earliest:
        return {"allowed": False, "code": "too_early", "reason": "event window has not opened"}
    if current > latest:
        return {"allowed": False, "code": "late_execution", "reason": "event window expired"}
    if snapshot["do_not_disturb"]:
        return {"allowed": False, "code": "user_dnd", "reason": "current session explicitly requests no contact"}
    if snapshot.get("superseded"):
        return {"allowed": False, "code": "superseded", "reason": "event was superseded"}
    return {"allowed": True, "code": "allowed", "reason": "planned event passed deterministic hard gates"}
