#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

_PHASE_LABELS = {
    "completed": "已完成",
    "cancelled": "已取消",
    "failed": "失败",
    "skipped": "已跳过",
    "running": "执行中",
    "scheduled": "已计划",
    "pending": "待处理",
}


def _outcome_summary(item: dict[str, Any]) -> str:
    phase = str(item.get("phase") or "unknown")
    phase_label = _PHASE_LABELS.get(phase, phase)
    delivered = bool(item.get("telegram_sent") or item.get("session_injected"))
    delivery_label = "已送达" if delivered else "未送达"
    error_label = "；存在执行错误" if item.get("error") is not None else ""
    return f"**{phase_label}**；{delivery_label}{error_label}"


def _anomaly_summary(value: Any) -> str:
    text = str(value)
    if text.startswith("residual_at_job:"):
        return "存在未清理的计划任务记录。"
    if text.startswith("history_messages_without_timestamp:"):
        return "部分历史消息缺少时间戳。"
    if text == "event_journal_missing":
        return "事件日志缺失。"
    if "nonterminal" in text:
        return "存在未终结的事件状态。"
    return "存在内部一致性异常，详见受控的 Step 4 state/evidence。"


def render(
    date: str,
    messages: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    reconciliation: dict[str, Any],
    provenance: dict[str, Any],
    *,  # 名字参数：由调用方从 settings 注入，避免写死具体角色名
    character_name: str = "assistant",
    companion_name: str = "user",
) -> str:
    lines = [
        f"# Chatlog — {date}",
        "",
        "## Provenance",
        f"- Gateway pages: {provenance['page_count']}",
        f"- Current-session source messages: {provenance['source_message_count']}",
        f"- Current-session selected messages: {provenance.get('current_session_message_count', 0)}",
        f"- Carryover messages: {provenance.get('carryover_message_count', 0)}",
        f"- Daily revision: {provenance['daily_revision']}",
        "",
        "## Conversation",
    ]
    canonical = [item for item in messages if not item["duplicate_of"]]
    if not canonical:
        lines += ["", "_当天没有带有效时间戳的对话。_"]
    for item in canonical:
        when = (
            (item["timestamp"] or "unknown")[11:19]
            if item["timestamp"]
            else "unknown"
        )
        who = (
            companion_name
            if item["role"] == "user"
            else character_name
            if item["role"] == "assistant"
            else item["role"]
        )
        lines += [
            "",
            f"### {when} — {who}",
            item["text"] or "_空文本_",
            f"<!-- evidence:{item['evidence_id']} -->",
        ]
    lines += ["", "## Event outcomes"]
    if not outcomes:
        lines += ["", "_没有 Step 3 transaction 记录。_"]
    for index, item in enumerate(outcomes, 1):
        lines += [
            "",
            f"- 主动事件 {index} → {_outcome_summary(item)} "
            f"<!-- evidence:{item['evidence_id']} -->",
        ]
    lines += [
        "",
        "## Plan vs actual",
        f"- Planned: {reconciliation['planned_count']}",
        f"- Completed: {reconciliation['completed_count']}",
        f"- Cancelled: {reconciliation['cancelled_count']}",
        f"- Failed: {reconciliation['failed_count']}",
        f"- Skipped: {reconciliation['skipped_count']}",
        f"- Finalization status: {reconciliation['status']}",
        "",
        "## Anomalies",
    ]
    lines += (
        ["- " + _anomaly_summary(item) for item in reconciliation["anomalies"]]
        if reconciliation["anomalies"]
        else ["", "_无。_"]
    )
    return "\n".join(lines).rstrip() + "\n"
