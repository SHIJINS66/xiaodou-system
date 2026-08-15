#!/usr/bin/env python3
"""event_journal — 等价迁移自旧 step03/bin/event_journal.py。

只追加的事务日志（JSONL）：记录每次执行转场的关键字段，追溯提供方/模型/用量。
不保存对话原文或密钥；字段白名单校验 + schema 校验 + 原子追加。

等价迁移：逻辑逐条保留；schema 定位由 package_root 推导（替代写死的包路径）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import StepError, atomic_write_text, file_lock, now_iso, package_root
from schema_tools import validate


def append(journal_root: Path, date: str, record: dict[str, Any], lock_path: Path) -> None:
    allowed = {
        "event_id", "execution_id", "from_phase", "to_phase", "reason", "provider",
        "provider_message_id", "error_code", "model", "usage", "request_fingerprint",
    }
    unknown = set(record) - allowed
    if unknown:
        raise StepError(f"journal contains forbidden fields: {sorted(unknown)}")
    path = journal_root / f"{date}.jsonl"
    with file_lock(lock_path):
        rows: list[dict[str, Any]] = []
        if path.exists():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StepError(f"invalid journal line {number}") from exc
                if not isinstance(parsed, dict):
                    raise StepError(f"journal line {number} is not object")
                rows.append(parsed)
        entry = {"sequence": len(rows) + 1, "timestamp": now_iso(), **record}
        validate(entry, package_root(__file__) / "schemas" / "event_journal_record_v1.schema.json")
        rows.append(entry)
        payload = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
        atomic_write_text(path, payload)
