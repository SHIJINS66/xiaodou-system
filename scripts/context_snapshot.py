#!/usr/bin/env python3
"""context_snapshot — 等价迁移自旧 step03/bin/context_snapshot.py。

构建执行决策所需的会话上下文快照：当前活跃 segment、最近消息、
DND/忙碌状态、（从当前 session 重建，不跨日）、回答计数、是否被 superseded 等。

等价迁移：
    - 逻辑逐条保留（时间窗匹配恰好一个活跃 segment、600k 字符上限、
      内部 marker 剥离、按时间排序、DND/忙碌短语判定）
    - 内部 marker 正则由 settings 派生（internal_marker_regex 替代 xiaodou_event）
    - message_text 来自 providers.openclaw_gateway（纯逻辑部分）
    - 时区默认由 common 兜底
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from common import _DEFAULT_TZ, StepError, internal_marker_regex, parse_iso
from providers.openclaw_gateway import message_text

MAX_CURRENT_SESSION_CHARS = 600_000


def build(
    settings,
    daily: dict[str, Any],
    event: dict[str, Any],
    messages: list[dict[str, Any]],
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    _marker = re.compile(internal_marker_regex(settings), re.IGNORECASE)
    captured = (captured_at or datetime.now(_DEFAULT_TZ)).astimezone(_DEFAULT_TZ)
    current = _active_segment(daily, captured)
    user = daily["runtime"]["user_context"]
    last_user_at = user.get("last_user_message_at")
    last_outbound_at = user.get("last_outbound_at")
    normalized: list[dict[str, Any]] = []
    total_chars = 0

    for item in messages:
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _visible_text(role, message_text(item), _marker)
        timestamp = _timestamp(item)
        if text:
            total_chars += len(text)
            if total_chars > MAX_CURRENT_SESSION_CHARS:
                raise StepError("current session context exceeds 600000 characters")
            normalized.append({"role": role, "text": text, "timestamp": timestamp, "_order": len(normalized)})
        if role == "user" and timestamp and (last_user_at is None or parse_iso(timestamp) > parse_iso(last_user_at)):
            last_user_at = timestamp

    normalized.sort(
        key=lambda item: (parse_iso(item["timestamp"]).timestamp(), item["_order"])
        if item["timestamp"]
        else (float("inf"), item["_order"])
    )
    for item in normalized:
        item.pop("_order", None)

    unanswered = int(user["unanswered_outbound_count"])
    if last_user_at and last_outbound_at and parse_iso(last_user_at) > parse_iso(last_outbound_at):
        unanswered = 0

    do_not_disturb = False
    declared_busy = False
    dnd_phrases = ("别打扰", "不要打扰", "先别联系", "do not disturb", "don't message")
    dnd_clear_phrases = ("可以聊", "现在有空", "可以联系", "不用免打扰", "可以找我")
    busy_phrases = ("我在忙", "现在忙", "开会", "赶工作", "busy")
    busy_clear_phrases = ("忙完了", "现在有空", "下班了", "可以聊")
    for item in normalized:
        if item["role"] != "user":
            continue
        text = item["text"].lower()
        if any(value in text for value in dnd_phrases):
            do_not_disturb = True
        if any(value in text for value in dnd_clear_phrases):
            do_not_disturb = False
        if any(value in text for value in busy_phrases):
            declared_busy = True
        if any(value in text for value in busy_clear_phrases):
            declared_busy = False

    states = {item["event_id"]: item for item in daily["runtime"]["event_states"]}
    threshold_sent = any(
        item.get("decision_reason") == "threshold_checkin" and item.get("telegram_sent")
        for item in states.values()
    )
    superseded = any(
        item.get("supersedes_event_id") == event["event_id"]
        for item in daily["runtime"]["runtime_events"]
    )
    return {
        "schema_version": "1.0",
        "captured_at": captured.isoformat(timespec="seconds"),
        "date": daily["date"],
        "event_id": event["event_id"],
        "current_segment_id": current["segment_id"],
        "current_reply_state": current["reply_state"],
        "declared_busy": declared_busy,
        "do_not_disturb": do_not_disturb,
        "unanswered_outbound_count": unanswered,
        "threshold_checkin_sent": threshold_sent,
        "last_user_message_at": last_user_at,
        "last_outbound_at": last_outbound_at,
        "superseded": superseded,
        "context_conflict": False,
        "recent_messages": normalized,
    }


def _timestamp(message: dict[str, Any]) -> str | None:
    raw = message.get("timestamp") or message.get("createdAt") or message.get("created_at")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 10_000_000_000 else raw, _DEFAULT_TZ).isoformat(timespec="seconds")
    if isinstance(raw, str):
        try:
            return parse_iso(raw).isoformat(timespec="seconds")
        except Exception:
            return None
    return None


def _active_segment(daily: dict[str, Any], captured: datetime) -> dict[str, Any]:
    matches = [
        segment
        for segment in daily["plan"]["timeline"]
        if parse_iso(segment["start_at"]) <= captured < parse_iso(segment["end_at"])
    ]
    if len(matches) != 1:
        raise StepError(f"timeline must contain exactly one active segment; matches={len(matches)}")
    return matches[0]


def _visible_text(role: str, raw: str, marker_re: "re.Pattern") -> str:
    text = raw
    if role == "assistant":
        text = marker_re.sub("", text)
        if text.strip().lower() in {"no_reply", "no reply"}:
            return ""
    return text.strip()
