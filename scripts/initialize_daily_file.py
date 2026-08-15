#!/usr/bin/env python3
"""initialize_daily_file — 等价迁移自旧 step02/bin/initialize_daily_file.py。

把 planner 输出物化成当日 daily 文件：注入 runtime/user_context/event_states，
过 daily schema 校验后原子落盘。这是 planner（step02）→ 事件执行（step03）之间
的正式衔接点。

等价迁移保留的线上行为：
    - planner 过 validate_planner（结构 + 时间线 + 语义），有 errors 即拒绝
    - 已存在 daily 时需 --replace-draft 且只能是 draft/validated（防覆盖已调度计划）
    - 按当前时间定位 current_segment 写入 runtime.current_state
    - event_states 从 plan.events 展开为 status=planned 初始状态
    - 来源指纹 source_fingerprints（核心文件 + planner_json sha256）

适配点：
    - 去掉线上 CORE_FILES/verify_frozen_core（身份核验），改为对 settings 列出的
      核心文件做 sha256 指纹（文件列表走 settings.runtime.source_files）
    - 配置从 config-dir 改为 settings；时间区从 common bootstrap 拿
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from common import StepError, atomic_write_json, bootstrap, load_json, now_iso, parse_iso, sha256_file
from schema_validator import validate_daily, validate_planner


def current_segment(plan, now) -> tuple[str, str]:
    for seg in plan["timeline"]:
        if parse_iso(seg["start_at"]) <= now < parse_iso(seg["end_at"]):
            return seg["segment_id"], seg["reply_state"]
    first = plan["timeline"][0]
    return first["segment_id"], first["reply_state"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--planner", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--settings", default=None)
    p.add_argument("--config-dir", default=None)  # 兼容旧调用；优先 --settings
    p.add_argument("--output", default=None)
    p.add_argument("--replace-draft", action="store_true")
    a = p.parse_args()
    try:
        settings, _tz = bootstrap(a.settings)
        planner = Path(a.planner).resolve()
        workspace = Path(a.workspace) if a.workspace else settings.root_dir / "daily"
        workspace = workspace.resolve()
        plan = load_json(planner)
        errors, warnings = validate_planner(plan)
        if errors:
            raise StepError("; ".join(errors))
        output = Path(a.output).resolve() if a.output else workspace / f"{plan['date']}.json"
        if output.exists():
            existing = load_json(output)
            if not a.replace_draft:
                raise StepError(f"daily 文件已存在：{output}")
            if existing.get("plan_status") not in {"draft", "validated"}:
                raise StepError("只能替换 draft 或 validated")
        tz = _tz
        now = datetime.now(tz)
        sid, state = current_segment(plan, now)

        # 来源指纹：settings 指定 + planner
        source = settings.get("runtime.source_files") or []
        fingerprints = {}
        for name in source:
            fp_path = settings.root_dir / name if not Path(name).is_absolute() else Path(name)
            if fp_path.is_file():
                fingerprints[name] = sha256_file(fp_path)
        fingerprints["planner_json"] = sha256_file(planner)

        timezone = settings.get("runtime.timezone") or "Asia/Shanghai"
        daily = {
            "schema_version": "1.1",
            "date": plan["date"],
            "timezone": timezone,
            "plan_status": "validated",
            "file_revision": 1,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "source_fingerprints": fingerprints,
            "plan": plan,
            "runtime": {
                "validation": {"status": "passed", "validated_at": now_iso(), "errors": [], "warnings": warnings},
                "scheduling": {"status": "pending", "scheduled_at": None},
                "current_state": {"segment_id": sid, "reply_state": state, "updated_at": now_iso()},
                "user_context": {
                    "declared_busy": False,
                    "busy_until": None,
                    "do_not_disturb": False,
                    "unanswered_outbound_count": 0,
                    "last_user_message_at": None,
                    "last_outbound_at": None,
                },
                "runtime_events": [],
                "event_states": [
                    {
                        "event_id": e["event_id"],
                        "status": "planned",
                        "scheduled_for": None,
                        "at_job_id": None,
                        "attempt_count": 0,
                        "decision": "pending",
                        "decision_reason": None,
                        "started_at": None,
                        "completed_at": None,
                        "final_text": None,
                        "output_image": None,
                        "telegram_sent": False,
                        "session_injected": False,
                        "error": None,
                        "history": [{"at": now_iso(), "from_status": None, "to_status": "planned", "reason": "daily file initialized"}],
                    }
                    for e in plan["events"]
                ],
                "daily_memory": {"status": "pending", "generated_at": None, "path": None, "error": None},
            },
        }
        errors, more = validate_daily(daily)
        if errors:
            raise StepError("; ".join(errors))
        daily["runtime"]["validation"]["warnings"].extend(more)
        atomic_write_json(output, daily)
        print(output)
        return 0
    except (StepError, ValueError, KeyError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
