#!/usr/bin/env python3
from __future__ import annotations

import re
from typing import Any

from common import StepError as Step04Error

QUALITY_GATE_VERSION = "evidence-density-role-coverage-v1"

SECTIONS = (
    "actual_life",
    "proactive_shares",
    "companion_responses",
    "important_conversations",
    "emotional_and_relationship_notes",
    "unresolved_items",
    "tomorrow_implications",
    "long_term_memory_candidates",
)

_ACKS = {
    "嗯",
    "嗯嗯",
    "嗯呢",
    "好",
    "好的",
    "好滴",
    "好喔",
    "是",
    "是的",
    "是滴",
    "哦",
    "喔",
    "亲亲",
    "晚安",
    "谢谢",
    "哈哈",
    "哈哈哈",
    "ok",
    "okay",
}
_PUNCT = re.compile(r"[\s\u3000，。！？、；：,.!?;:～~…—_\-\"'“”‘’（）()\[\]{}<>《》]+")
_SELF_LIFE = re.compile(
    r"(?:^|[\n。！？])\s*(?:"
    r"我(?:呀|啊|这边|今天|现在)?|"
    r"刚(?:到公司|冲了|吃完|和同事|煮好|下班|回到)|"
    r"正在(?:工位|地铁|吃|回家|工作|追剧)|"
    r"现在在(?:工位|地铁|公司|家)|"
    r"下班啦|敷着面膜|准备睡觉"
    r").{0,60}(?:上班|下班|到公司|工位|工作|报销|对账|吃|喝|咖啡|"
    r"麻辣烫|煮|做饭|地铁|回家|追剧|看剧|敷面膜|睡觉|休息|摸鱼|犯困)"
)
_FORBIDDEN = re.compile(
    r"(?:execution_id|event_id|telegram_message_id|sessionKey|session_key|"
    r"reasoning_content|DEEPSEEK_API_KEY|MOONSHOT_API_KEY|openclaw|at_job|"
    r"\b[a-z0-9_-]+-[a-f0-9]{32}\b|/root/|/var/|/etc/xiaodou/)",
    re.IGNORECASE,
)


def _text_key(text: str) -> str:
    return _PUNCT.sub("", text).lower()


def is_substantive(text: str) -> bool:
    key = _text_key(text)
    if not key or key in _ACKS:
        return False
    return len(key) >= 4


def canonical_messages(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in evidence.get("messages", []):
        if not isinstance(item, dict) or item.get("duplicate_of") is not None:
            continue
        if item.get("role") not in {"user", "assistant"}:
            continue
        text = item.get("text")
        evidence_id = item.get("evidence_id")
        if not isinstance(text, str) or not text.strip() or not isinstance(evidence_id, str):
            continue
        rows.append(item)
    return rows


def _thresholds(substantive_count: int) -> tuple[int, int, int, int]:
    if substantive_count >= 40:
        return 6, 4, 8, 6
    if substantive_count >= 20:
        return 5, 4, 6, 4
    if substantive_count >= 10:
        return 3, 3, 4, 2
    if substantive_count >= 4:
        return 1, 1, 1, 1
    return 0, 0, 0, 0


def quality_contract(evidence: dict[str, Any]) -> dict[str, Any]:
    canonical = canonical_messages(evidence)
    substantive = [item for item in canonical if is_substantive(item["text"])]
    user_rows = [item for item in substantive if item.get("role") == "user"]
    assistant_rows = [item for item in substantive if item.get("role") == "assistant"]
    non_event_rows = [item for item in substantive if not item.get("event_id")]
    proactive_rows = [
        item
        for item in canonical
        if item.get("role") == "assistant"
        and item.get("event_id")
        and (item.get("telegram_sent") is True or item.get("session_injected") is True)
    ]
    self_life_rows = [
        item
        for item in assistant_rows
        if _SELF_LIFE.search(item.get("text", "")) is not None
    ]
    minimum_items, minimum_sections, minimum_citations, minimum_non_event = _thresholds(
        len(substantive)
    )
    return {
        "canonical_message_count": len(canonical),
        "substantive_message_count": len(substantive),
        "substantive_user_count": len(user_rows),
        "substantive_assistant_count": len(assistant_rows),
        "minimum_total_items": minimum_items,
        "minimum_non_empty_sections": minimum_sections,
        "minimum_cited_canonical_messages": minimum_citations,
        "minimum_cited_non_event_messages": minimum_non_event,
        "require_user_evidence": len(user_rows) >= 3,
        "require_assistant_evidence": len(assistant_rows) >= 3,
        "require_companion_responses": len(user_rows) >= 3,
        "require_actual_life": len(self_life_rows) >= 2,
        "required_proactive_message_evidence_ids": [
            item["evidence_id"] for item in proactive_rows
        ],
        "self_life_message_evidence_ids": [item["evidence_id"] for item in self_life_rows],
    }


def model_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    messages = []
    for item in canonical_messages(evidence):
        messages.append(
            {
                "evidence_id": item["evidence_id"],
                "timestamp": item.get("timestamp"),
                "speaker": "{companion_name}" if item.get("role") == "user" else "{character_name}",
                "role": item.get("role"),
                "text": item.get("text", ""),
                "is_proactive_share": bool(
                    item.get("role") == "assistant"
                    and item.get("event_id")
                    and (item.get("telegram_sent") or item.get("session_injected"))
                ),
                "has_image": bool(item.get("image_path")) or "[Image]" in item.get("text", ""),
            }
        )
    outcomes = []
    for item in evidence.get("event_outcomes", []):
        if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
            continue
        outcomes.append(
            {
                "evidence_id": item["evidence_id"],
                "phase": item.get("phase"),
                "delivered": bool(item.get("telegram_sent") or item.get("session_injected")),
                "error_present": item.get("error") is not None,
            }
        )
    reconciliation = evidence.get("reconciliation")
    if not isinstance(reconciliation, dict):
        reconciliation = {}
    return {
        "schema_version": "1.1",
        "date": evidence.get("date"),
        "canonical_messages": messages,
        "event_outcomes": outcomes,
        "reconciliation": {
            key: reconciliation.get(key)
            for key in (
                "planned_count",
                "completed_count",
                "cancelled_count",
                "failed_count",
                "skipped_count",
                "status",
            )
            if key in reconciliation
        },
        "quality_contract": quality_contract(evidence),
    }


def _items(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for section in SECTIONS:
        rows = value.get(section, [])
        if isinstance(rows, list):
            result.extend((section, item) for item in rows if isinstance(item, dict))
    return result


def validate_quality(value: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    contract = quality_contract(evidence)
    all_messages = {
        item.get("evidence_id"): item
        for item in evidence.get("messages", [])
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    canonical = {item["evidence_id"]: item for item in canonical_messages(evidence)}
    user_ids = {key for key, item in canonical.items() if item.get("role") == "user"}
    assistant_ids = {key for key, item in canonical.items() if item.get("role") == "assistant"}
    non_event_ids = {key for key, item in canonical.items() if not item.get("event_id")}
    duplicate_ids = {
        key for key, item in all_messages.items() if item.get("duplicate_of") is not None
    }
    proactive_ids = set(contract["required_proactive_message_evidence_ids"])
    event_outcome_ids = {
        item.get("evidence_id")
        for item in evidence.get("event_outcomes", [])
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }

    rows = _items(value)
    total_items = len(rows)
    non_empty_sections = sum(1 for section in SECTIONS if value.get(section))
    cited_ids: set[str] = set()
    normalized_texts: set[str] = set()

    for section, item in rows:
        text = item.get("text", "")
        refs = item.get("evidence_ids", [])
        if not isinstance(text, str) or not text.strip():
            raise Step04Error(f"memory quality failure: empty text in {section}")
        if text.strip() in {"无", "无。", "没有", "没有。"}:
            raise Step04Error(f"memory quality failure: placeholder item in {section}")
        if _FORBIDDEN.search(text):
            raise Step04Error(f"memory quality failure: internal identifier leaked in {section}")
        if text.lstrip().startswith("#") or "<!--" in text or "-->" in text:
            raise Step04Error(f"memory quality failure: markup leaked in {section}")
        text_key = _text_key(text)
        if text_key in normalized_texts:
            raise Step04Error("memory quality failure: duplicated memory text")
        normalized_texts.add(text_key)
        if not isinstance(refs, list):
            raise Step04Error(f"memory quality failure: invalid evidence list in {section}")
        ref_set = {ref for ref in refs if isinstance(ref, str)}
        cited_ids.update(ref_set)
        if ref_set & duplicate_ids:
            raise Step04Error("memory quality failure: duplicate transport evidence cited")
        if section == "actual_life" and not (ref_set & assistant_ids):
            raise Step04Error("memory quality failure: actual_life lacks Xiaodou message evidence")
        if section == "companion_responses" and not (ref_set & user_ids):
            raise Step04Error("memory quality failure: companion_responses lacks user evidence")
        if section == "proactive_shares" and not (
            ref_set & (proactive_ids | event_outcome_ids)
        ):
            raise Step04Error("memory quality failure: proactive_shares lacks proactive evidence")

    cited_canonical = cited_ids & set(canonical)
    cited_user = cited_ids & user_ids
    cited_assistant = cited_ids & assistant_ids
    cited_non_event = cited_ids & non_event_ids

    failures: list[str] = []
    if total_items < contract["minimum_total_items"]:
        failures.append(
            f"total_items={total_items}<minimum={contract['minimum_total_items']}"
        )
    if non_empty_sections < contract["minimum_non_empty_sections"]:
        failures.append(
            "non_empty_sections="
            f"{non_empty_sections}<minimum={contract['minimum_non_empty_sections']}"
        )
    if len(cited_canonical) < contract["minimum_cited_canonical_messages"]:
        failures.append(
            "cited_canonical_messages="
            f"{len(cited_canonical)}<minimum={contract['minimum_cited_canonical_messages']}"
        )
    if len(cited_non_event) < contract["minimum_cited_non_event_messages"]:
        failures.append(
            "cited_non_event_messages="
            f"{len(cited_non_event)}<minimum={contract['minimum_cited_non_event_messages']}"
        )
    if contract["require_user_evidence"] and not cited_user:
        failures.append("missing_user_evidence")
    if contract["require_assistant_evidence"] and not cited_assistant:
        failures.append("missing_assistant_evidence")
    if contract["require_companion_responses"] and not value.get("companion_responses"):
        failures.append("missing_companion_responses_section")
    if contract["require_actual_life"] and not value.get("actual_life"):
        failures.append("missing_actual_life_section")
    proactive_section_refs = {
        ref
        for item in value.get("proactive_shares", [])
        if isinstance(item, dict)
        for ref in item.get("evidence_ids", [])
        if isinstance(ref, str)
    }
    missing_proactive = proactive_ids - proactive_section_refs
    if missing_proactive:
        failures.append(f"missing_proactive_evidence={len(missing_proactive)}")
    if failures:
        raise Step04Error("memory quality failure: " + "; ".join(failures))

    return {
        "passed": True,
        "contract": contract,
        "observed": {
            "total_items": total_items,
            "non_empty_sections": non_empty_sections,
            "cited_canonical_messages": len(cited_canonical),
            "cited_user_messages": len(cited_user),
            "cited_assistant_messages": len(cited_assistant),
            "cited_non_event_messages": len(cited_non_event),
            "proactive_messages_required": len(proactive_ids),
            "proactive_messages_covered": len(proactive_ids & proactive_section_refs),
        },
    }
