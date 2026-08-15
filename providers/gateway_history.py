#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import date as Date, datetime, time, timedelta
from typing import Any

from common import StepError, make_tz, parse_iso

TZ = make_tz("Asia/Shanghai")
from providers.gateway_sessions import call, get_session, result_object, row_session_id

_PLACEHOLDER_MARKERS = (
    "chat.history omitted",
    "message too large",
    "history omitted",
)


def timestamp(row: dict[str, Any]) -> datetime | None:
    raw = row.get("timestamp") or row.get("createdAt") or row.get("created_at")
    metadata = row.get("__openclaw") if isinstance(row.get("__openclaw"), dict) else {}
    raw = raw or metadata.get("recordTimestampMs")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 10_000_000_000 else raw, TZ)
    if isinstance(raw, str):
        try:
            return parse_iso(raw)
        except Exception:
            return None
    return None


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


def _history_rows(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str | None, str | None]:
    result = result_object(parsed)
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


def _is_tool_call_only(content: object) -> bool:
    if not isinstance(content, list) or not content:
        return False
    if not all(isinstance(item, dict) for item in content):
        return False
    return all(
        item.get("type") in {"toolCall", "tool_call"}
        for item in content
    )


def _needs_full_message(message: dict[str, Any]) -> bool:
    if message.get("truncated") is True or message.get("omitted") is True:
        return True
    content = message.get("content")
    if _is_tool_call_only(content):
        return False
    text = message_text(message).strip().lower()
    if message.get("role") in {"user", "assistant"} and not text and message_id(message) is not None:
        return True
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def _message_from_response(parsed: dict[str, Any], identity: str) -> dict[str, Any]:
    result = result_object(parsed)
    candidates = [result.get("message"), result.get("entry"), result.get("data"), result]
    for candidate in candidates:
        if isinstance(candidate, dict):
            merged = dict(candidate)
            merged.setdefault("id", identity)
            return merged
    raise StepError("chat.message.get response does not contain a message")


def get_message(config: dict[str, Any], identity: str) -> dict[str, Any]:
    errors: list[StepError] = []
    for key in ("messageId",):
        try:
            return _message_from_response(
                call(
                    config,
                    "chat.message.get",
                    {"sessionKey": config["session_key"], key: identity},
                ),
                identity,
            )
        except StepError as exc:
            errors.append(exc)
    raise StepError("chat.message.get failed for supported id variants") from errors[-1]


def collect_between(
    config: dict[str, Any],
    start: datetime,
    end: datetime,
    caller=call,
) -> dict[str, Any]:
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise StepError("invalid history time window")
    before = get_session(config)
    expected_session_id = row_session_id(before)
    offset: str | None = None
    seen_offsets: set[str] = set()
    seen_message_ids: set[str] = set()
    pages: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    total_bytes = 0

    for page_number in range(int(config["max_pages"])):
        params: dict[str, Any] = {
            "sessionKey": config["session_key"],
            "limit": int(config["page_size"]),
        }
        if offset is not None:
            params["offset"] = offset
        parsed = caller(config, "chat.history", params)
        rows, has_more, next_offset, page_session = _history_rows(parsed)
        raw = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()
        total_bytes += len(raw)
        if total_bytes > int(config["max_total_bytes"]):
            raise StepError("history exceeds max_total_bytes")
        if page_session and page_session != expected_session_id:
            raise StepError("chat.history sessionId changed during pagination")

        for row in rows:
            identity = message_id(row)
            if identity is not None and identity in seen_message_ids:
                continue
            item = row
            if identity is not None:
                seen_message_ids.add(identity)
                if _needs_full_message(row):
                    try:
                        item = get_message(config, identity)
                    except Exception:
                        item = row
                    if _needs_full_message(item):
                        item = row
            messages.append(item)
            if len(messages) > int(config["max_messages"]):
                raise StepError("history exceeds max_messages")

        pages.append(
            {
                "page": page_number + 1,
                "offset": offset,
                "count": len(rows),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        if not has_more:
            break
        if next_offset is None or next_offset in seen_offsets:
            raise StepError("invalid or repeated nextOffset")
        seen_offsets.add(next_offset)
        offset = next_offset
    else:
        raise StepError("history exceeds max_pages")

    after = get_session(config)
    if row_session_id(after) != expected_session_id:
        raise StepError("active session changed while history was being read")

    selected = [
        row
        for row in messages
        if (stamp := timestamp(row)) is not None and start <= stamp < end
    ]
    untimed = sum(1 for row in messages if timestamp(row) is None)
    return {
        "session_id": expected_session_id,
        "session_row_before": before,
        "session_row_after": after,
        "messages": selected,
        "pages": pages,
        "untimed_count": untimed,
        "source_message_count": len(messages),
    }


def collect(config: dict[str, Any], target: Date, caller=call) -> dict[str, Any]:
    start = datetime.combine(target, time.min, TZ)
    return collect_between(config, start, start + timedelta(days=1), caller=caller)
