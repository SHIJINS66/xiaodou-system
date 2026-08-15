#!/usr/bin/env python3
"""schedule_step03_events — 等价迁移自旧 step03/bin/schedule_step03_events.py（实发 at）。

Step 3 执行阶段调度器：对已批准的执行事件做**真实 at 提交**（区别于 Step 2 的
stub-only）。它是主动消息真正落到系统 at 队列的环节。

保留线上语义的等价点：
    - 只调度非 silent 且状态未终结的事件
    - marker 用 SHA256(daily:event_id) 前 24 位，重复提交防护 + 队列恢复
    - 事件窗口过期 → skipped/cancel
    - 提交前先持久化 scheduling 意图状态，提交成功后再置 scheduled（原子、可回溯）
    - at 提交后必须唯一回读同 marker 作业，否则报错
    - 全部事件处理完更新 runtime.scheduling.status（scheduled/partial）

适配点（相对线上）：
    - 配置从 step03.json 改为 settings（executor/python/at 路径、schema、锁文件）
    - 前缀 xd03- / env XIAODOU_STEP03_MARKER -> settings.system.instance_name 派生
    - 错误统一 StepError
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from at_adapter import command, jobs_with_marker, marker, submit
from common import (StepError, all_events, atomic_write_json, bootstrap, bump_daily,
                    file_lock, load_json, now_iso, package_root, parse_iso)

TERMINAL = {"running", "completed", "cancelled", "failed", "skipped"}


def select_when(now: datetime, preferred: datetime, latest: datetime) -> datetime | None:
    chosen = preferred if preferred > now else now + timedelta(minutes=1)
    return None if chosen > latest else chosen


def config_paths(settings) -> dict:
    """从包根 + settings 解析执行器 / python / schema / 锁文件。

    executor 与 schema 属于框架包（package_root），不是实例数据目录；
    锁文件属于实例目录（settings.runtime.root_dir）。
    """
    pkg = package_root(__file__)
    run = settings.get("scheduling.executor") or {}
    return {
        "executor_path": pkg / (run.get("event_executor") or "scripts/execute_daily_event.py"),
        "python_bin": Path(run.get("python_bin") or "/usr/bin/python3"),
        "daily_schema_path": pkg / "schemas" / "daily_file_v1_1.schema.json",
        "daily_lock_file": settings.root_dir / (run.get("lock_file") or "var/lock/step03-schedule.lock"),
    }


def persist(path: Path, daily: dict) -> None:
    bump_daily(daily)
    atomic_write_json(path, daily)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-file", required=True)
    parser.add_argument("--config", default=None)  # 兼容旧调用，优先 settings
    parser.add_argument("--settings", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        settings, _tz = bootstrap(args.settings)
        daily_path = Path(args.daily_file).resolve()
        paths = config_paths(settings)
        # 兼容旧 config 传入的路径覆盖
        if args.config and Path(args.config).is_file():
            old_cfg = load_json(Path(args.config))
            for k in ("executor_path", "python_bin", "daily_schema_path", "daily_lock_file"):
                v = old_cfg.get(k)
                if v:
                    paths[k] = Path(v)

        with file_lock(paths["daily_lock_file"]):
            daily = load_json(daily_path)
            from schema_tools import validate
            validate(daily, paths["daily_schema_path"])
            events = {item["event_id"]: item for item in all_events(daily)}
            states = {item["event_id"]: item for item in daily["runtime"]["event_states"]}
            now = parse_iso(args.now, _tz) if args.now else datetime.now(_tz)
            actions = []
            for event_id, event in events.items():
                state = states[event_id]
                if event["type"] == "silent" or state["status"] in TERMINAL:
                    continue
                token = marker(settings, daily_path, event_id)
                existing = jobs_with_marker(settings, token) if args.apply else []
                if len(existing) > 1:
                    raise StepError(f"multiple queued jobs for {event_id}")
                if existing:
                    old = state["status"]
                    state.update(status="scheduled", at_job_id=existing[0], decision_reason="recovered from Step 3 at marker")
                    state["history"].append({"at": now_iso(), "from_status": old, "to_status": "scheduled", "reason": "recovered from Step 3 at marker"})
                    persist(daily_path, daily)
                    continue
                preferred = parse_iso(event["time_window"]["preferred_at"])
                latest = parse_iso(event["time_window"]["latest_at"])
                when = select_when(now, preferred, latest)
                if when is None:
                    actions.append({"event_id": event_id, "action": "skip_expired"})
                    if args.apply:
                        old = state["status"]
                        state.update(status="skipped", decision="cancel", decision_reason="event window expired", completed_at=now_iso(), at_job_id=None)
                        state["history"].append({"at": now_iso(), "from_status": old, "to_status": "skipped", "reason": "event window expired"})
                        persist(daily_path, daily)
                    continue
                cfg_for_cmd = _config_passthrough(settings)
                preview = command(settings, paths["executor_path"], paths["python_bin"], cfg_for_cmd, daily_path, daily["date"], event_id, token)
                actions.append({"event_id": event_id, "schedule_for": when.isoformat(), "marker": token, "command": preview})
                if not args.apply:
                    continue
                old = state["status"]
                state.update(status="scheduling", scheduled_for=when.isoformat(), decision_reason="Step 3 scheduling intent persisted")
                state["history"].append({"at": now_iso(), "from_status": old, "to_status": "scheduling", "reason": "Step 3 scheduling intent persisted"})
                persist(daily_path, daily)
                outcome = submit(settings, paths["executor_path"], paths["python_bin"], cfg_for_cmd, daily_path, daily["date"], event_id, when)
                state.update(status="scheduled", at_job_id=outcome["job_id"], decision_reason="Step 3 at marker verified")
                state["history"].append({"at": now_iso(), "from_status": "scheduling", "to_status": "scheduled", "reason": "Step 3 at marker verified"})
                persist(daily_path, daily)
            if args.apply:
                relevant = [states[event_id]["status"] for event_id, event in events.items() if event["type"] != "silent"]
                daily["runtime"]["scheduling"]["status"] = "scheduled" if all(value in {"scheduled", "skipped", "cancelled", "completed"} for value in relevant) else "partial"
                daily["runtime"]["scheduling"]["scheduled_at"] = now_iso()
                daily["plan_status"] = "scheduled"
                validate(daily, paths["daily_schema_path"])
                persist(daily_path, daily)
            print(json.dumps({"mode": "apply" if args.apply else "dry_run", "actions": actions}, ensure_ascii=False, indent=2))
            return 0
    except (StepError, KeyError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


def _config_passthrough(settings):
    """构造传给 at 任务的 settings 路径（executor 通过 --config 拿到完整设置）。"""
    return settings.path


if __name__ == "__main__":
    raise SystemExit(main())
