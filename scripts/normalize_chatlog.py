#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from common import StepError, instance_marker
from providers.gateway_history import timestamp

def make_marker(settings=None):
    """构造内部事件 marker 正则。实例名前缀从 settings.system.instance_name 泛化，
    不再写死 xiaodou_event / xd03-。"""
    name = instance_marker(settings) if settings is not None else "companion"
    pattern = (
        "\\[" + name + "_event event_id=([0-9]{8}-[er][0-9]{2}) "
        "execution_id=(" + name + "-[a-f0-9]{32}) telegram_message_id=([^ ]+) media=([^\\]]+)\\]"
    )
    return re.compile(pattern)
_SPACE = re.compile(r"\s+")


def text(row: dict[str, Any]) -> str:
    content = row.get("content", row.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("__openclaw") if isinstance(row.get("__openclaw"), dict) else {}


def _content_key(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _event_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_event = left.get("event_id")
    right_event = right.get("event_id")
    if left_event is None and right_event is None:
        return True
    return (
        left_event is not None
        and left_event == right_event
        and left.get("execution_id") is not None
        and left.get("execution_id") == right.get("execution_id")
    )


def _near(left: datetime | None, right: datetime | None, seconds: int = 15) -> bool:
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) <= seconds


def normalize(
    rows: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    settings=None,
) -> list[dict[str, Any]]:
    marker_re = make_marker(settings)
    tx_by_exec = {
        item.get("execution_id"): item
        for item in transactions
        if item.get("execution_id")
    }
    prepared: list[dict[str, Any]] = []
    fallback_counts: dict[str, int] = {}

    for source_order, row in enumerate(rows):
        raw_body = text(row)
        marker = marker_re.search(raw_body)
        event_id = execution_id = telegram_id = None
        image = None
        if marker:
            event_id, execution_id, telegram_id, image = marker.groups()
        visible_body = marker_re.sub("", raw_body).strip()
        meta = _meta(row)
        stamp = timestamp(row)
        txn = tx_by_exec.get(execution_id, {})

        stable_key: str | None = None
        if meta.get("id") is not None and meta.get("seq") is not None:
            stable_key = f"openclaw|{meta['id']}|{meta['seq']}"
        elif execution_id and telegram_id:
            stable_key = f"event|{execution_id}|{telegram_id}"
        elif meta.get("id") is not None:
            stable_key = f"openclaw|{meta['id']}"

        content_key = _content_key(visible_body)
        role = str(row.get("role", "unknown"))
        if stable_key is None:
            fallback_base = (
                f"fallback|{role}|"
                f"{stamp.isoformat(timespec='microseconds') if stamp else 'untimed'}|"
                f"{content_key}"
            )
            occurrence = fallback_counts.get(fallback_base, 0)
            fallback_counts[fallback_base] = occurrence + 1
            identity = f"{fallback_base}|{occurrence}"
        else:
            # Bind evidence identity to the immutable message role and visible
            # content. A reused transport ID with different content is evidence
            # corruption and is rejected below rather than silently deduped.
            identity = f"{stable_key}|{role}|{content_key}"

        evidence_id = "ev-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        prepared.append(
            {
                "_source_order": source_order,
                "_stamp": stamp,
                "_stable_key": stable_key,
                "_content_key": content_key,
                "evidence_id": evidence_id,
                "timestamp": stamp.isoformat(timespec="seconds") if stamp else None,
                "role": row.get("role", "unknown"),
                "direction": "inbound" if row.get("role") == "user" else "outbound",
                "text": visible_body,
                "openclaw_id": str(meta.get("id")) if meta.get("id") is not None else None,
                "openclaw_seq": meta.get("seq"),
                "kind": meta.get("kind"),
                "event_id": event_id or txn.get("event_id"),
                "execution_id": execution_id,
                "telegram_message_id": telegram_id or txn.get("telegram", {}).get("message_id"),
                "telegram_sent": bool(
                    txn.get("telegram", {}).get("sent", bool(telegram_id))
                ),
                "session_injected": bool(
                    txn.get("injection", {}).get("injected", bool(marker))
                ),
                "image_path": None if image in {None, "none"} else image,
                "duplicate_of": None,
                "heuristic_dedupe": False,
            }
        )

    prepared.sort(
        key=lambda item: (
            item["timestamp"] or "9999",
            item["_source_order"],
            item["evidence_id"],
        )
    )

    stable_seen: dict[str, dict[str, Any]] = {}
    assistant_semantic: dict[str, list[dict[str, Any]]] = {}
    fallback_exact: dict[tuple[str, str, str | None], str] = {}

    for item in prepared:
        duplicate: str | None = None
        stable_key = item["_stable_key"]
        if stable_key is not None and stable_key in stable_seen:
            prior = stable_seen[stable_key]
            if (
                prior["role"] != item["role"]
                or prior["_content_key"] != item["_content_key"]
            ):
                raise StepError(
                    "stable message identity collision with different content"
                )
            duplicate = prior["evidence_id"]
        elif stable_key is None:
            fallback_key = (item["role"], item["_content_key"], item["timestamp"])
            if fallback_key in fallback_exact:
                duplicate = fallback_exact[fallback_key]
                item["heuristic_dedupe"] = True

        if duplicate is None and item["role"] == "assistant" and item["_content_key"]:
            candidates = assistant_semantic.get(item["_content_key"], [])
            for prior in reversed(candidates[-8:]):
                if _near(item["_stamp"], prior["_stamp"]) and _event_compatible(item, prior):
                    duplicate = prior["evidence_id"]
                    item["heuristic_dedupe"] = True
                    break

        item["duplicate_of"] = duplicate
        if duplicate is None:
            if stable_key is not None:
                stable_seen.setdefault(stable_key, item)
            else:
                fallback_exact.setdefault(
                    (item["role"], item["_content_key"], item["timestamp"]),
                    item["evidence_id"],
                )
            if item["role"] == "assistant" and item["_content_key"]:
                assistant_semantic.setdefault(item["_content_key"], []).append(item)

    result = []
    for item in prepared:
        cleaned = {
            key: value
            for key, value in item.items()
            if key not in {"_source_order", "_stamp", "_stable_key", "_content_key"}
        }
        result.append(cleaned)
    return result


def transaction_outcomes(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for txn in transactions:
        identity = txn.get("execution_id") or txn.get("event_id") or "unknown"
        evidence_id = "ev-" + hashlib.sha256(
            ("txn|" + str(identity)).encode()
        ).hexdigest()[:24]
        rows.append(
            {
                "evidence_id": evidence_id,
                "event_id": txn.get("event_id"),
                "execution_id": txn.get("execution_id"),
                "phase": txn.get("phase"),
                "decision": txn.get("decision"),
                "decision_reason": txn.get("decision_reason"),
                "telegram_sent": bool(txn.get("telegram", {}).get("sent")),
                "session_injected": bool(txn.get("injection", {}).get("injected")),
                "error": txn.get("error"),
            }
        )
    return rows
