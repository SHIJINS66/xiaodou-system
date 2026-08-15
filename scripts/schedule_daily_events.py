#!/usr/bin/env python3
"""schedule_daily_events — 等价迁移自旧 step02/bin/schedule_daily_events.py（stub-only）。

Step 2 计划阶段调度器：对 planner 产出的 daily JSON，把每个可调度事件映射到
at 队列。与 Step 3 实发版不同，本模块受保护——只允许 --stub-only 干跑/登记意图，
不真正产生可执行消费（Step 3 才是实际触发）。

保留线上语义的等价点：
    - 只调度非 silent 且状态未终结的事件
    - marker 用 SHA256(daily:event_id) 前 24 位；已存在于 at 队列的同标记作业
      直接恢复（recovered），不重复提交
    - 事件窗口过期（preferred/latest 均过去）→ skipped/cancel
    - at -t 提交后必须能唯一回读到该 marker 的作业，否则报错
    - 全部动作注册到 daily 的 runtime.event_states / scheduling，原子持久化

适配点（相对线上）：
    - 前缀 xd02- / env XIAODOU_STEP02_MARKER -> settings.system.instance_name 派生
    - executor 默认值不写死 /opt/xiaodou（由调用方/配置传入）
    - at/atq 路径与 env 走 settings.scheduling
    - 错误统一 StepError
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from at_adapter import at_timestamp
from common import (StepError, all_events, atomic_write_json, bootstrap, event_marker_id,
                    file_lock, instance_marker, load_json, make_at_env, marker_env_var,
                    now_iso, parse_iso)
from at_adapter import jobs_with_marker as adapter_jobs_with_marker

JOB_RE = re.compile(r"\bjob\s+(\d+)\b", re.I)
TERMINAL = {"running", "completed", "cancelled", "failed", "skipped"}


def is_schedulable(event: dict, state: dict) -> bool:
    return event.get("type") != "silent" and state.get("status") not in TERMINAL


def select_when(now: datetime, preferred: datetime, latest: datetime):
    when = preferred if preferred > now else now + timedelta(minutes=1)
    return None if when > latest else when


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--daily-file", required=True)
    p.add_argument("--executor", default=None)
    p.add_argument("--python-bin", default="/usr/bin/python3")
    p.add_argument("--settings", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--stub-only", action="store_true")
    p.add_argument("--lock-file", default=None)
    p.add_argument("--now", help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.apply and not a.stub_only:
        print("FATAL: Step 2 只允许 --stub-only", file=sys.stderr)
        return 2

    try:
        settings, _tz = bootstrap(a.settings)
        path = Path(a.daily_file).resolve()
        lock_file = Path(a.lock_file) if a.lock_file else Path(settings.get("runtime.root_dir")) / ".locks" / "step02-schedule.lock"
        executor = Path(a.executor) if a.executor else None

        with file_lock(lock_file):
            daily = load_json(path)
            from schema_validator import validate_daily
            errors, _warnings = validate_daily(daily)
            if errors:
                raise StepError("; ".join(errors))

            events = {e["event_id"]: e for e in all_events(daily)}
            states = {s["event_id"]: s for s in daily["runtime"]["event_states"]}
            now = parse_iso(a.now, _tz) if a.now else datetime.now(_tz)
            actions = []
            env_var = marker_env_var(settings)
            at_env = make_at_env(settings, home=str(path.parent))

            for eid, e in events.items():
                state = states[eid]
                if not is_schedulable(e, state):
                    continue
                token = event_marker_id(settings, path, eid)

                # 已存在同标记作业 → 恢复，不重复提交
                existing = adapter_jobs_with_marker(settings, token) if a.apply else []
                if len(existing) > 1:
                    raise StepError(f"{eid}: 队列中存在多个同标记作业，停止以避免重复")
                if existing:
                    old = state["status"]
                    state.update(status="scheduled", at_job_id=existing[0], decision_reason="recovered from at queue marker")
                    state["history"].append({"at": now_iso(), "from_status": old, "to_status": "scheduled", "reason": "recovered from at queue marker"})
                    atomic_write_json(path, daily)
                    continue

                preferred = parse_iso(e["time_window"]["preferred_at"])
                latest = parse_iso(e["time_window"]["latest_at"])
                when = select_when(now, preferred, latest)
                if when is None:
                    if a.apply:
                        old = state["status"]
                        state.update(status="skipped", decision="cancel", decision_reason="event window expired", completed_at=now_iso())
                        state["history"].append({"at": now_iso(), "from_status": old, "to_status": "skipped", "reason": "event window expired"})
                        atomic_write_json(path, daily)
                    actions.append({"event_id": eid, "action": "skip_expired"})
                    continue

                # stub-only：只登记意图，不实际提交；但仍构造可预览命令
                cmd_parts = ["env", f"{env_var}={token}"]
                if executor is not None:
                    cmd_parts += [str(executor), "--date", daily["date"], "--event-id", eid, "--daily-file", str(path)]
                cmd = " ".join(shlex.quote(x) for x in cmd_parts)

                actions.append({"event_id": eid, "schedule_for": when.isoformat(), "marker": token, "command": cmd})
                if not a.apply:
                    continue

                # stub-only 模式下也持久化"意图"，标记 scheduled（Step 2 不真触发）
                old = state["status"]
                state.update(status="scheduled", scheduled_for=when.isoformat(), at_job_id=None, decision_reason="Step 2 stub-only intent persisted")
                state["history"].append({"at": now_iso(), "from_status": old, "to_status": "scheduled", "reason": "Step 2 stub-only intent persisted"})
                atomic_write_json(path, daily)

            if a.apply:
                relevant = [states[eid]["status"] for eid, s in states.items() if events[eid]["type"] != "silent"]
                daily["runtime"]["scheduling"]["status"] = "scheduled" if all(v in {"scheduled", "skipped", "cancelled", "completed"} for v in relevant) else "partial"
                daily["runtime"]["scheduling"]["scheduled_at"] = now_iso()
                daily["plan_status"] = "scheduled"
                atomic_write_json(path, daily)

            print(json.dumps({"mode": "apply_stub_only" if a.apply else "dry_run", "warnings": [], "actions": actions}, ensure_ascii=False, indent=2))
            return 0
    except (StepError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
