#!/usr/bin/env python3
"""run_morning_pipeline_step03 — step03 晨间编排（等价迁移自旧 step03/bin/run_morning_pipeline_step03.py）。

step02 之后流水线的下一段：确保当日 daily 就绪（缺则调 step02 侧生成），
然后调用 schedule_step03_events 应用 `at` 级真实调度（非 stub-only）。

这是 晨间 planner(step02) → at 调度(step03) → 事件执行(step03 at 触发) 的
正式衔接入口。cron 里 step03 编排在本脚本，step02 编排在 run_morning_pipeline.py。

等价迁移保留的线上行为：
    - 生成 daily 的链路与 step02 编排器一致（build_daily_plan → initialize_daily_file）
    - 之后跑 schedule_step03_events（--apply 时真实提交 at，非 stub-only）
    - require_enabled 校验

适配点：
    - 配置从 step03.json 改为 settings；enable 标记从 settings runtime 读
    - 路径由 settings/包定位派生（python_bin 实参/绝对路径 /etc/xiaodou 等不写死）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import StepError, bootstrap, file_lock, package_root
from schema_tools import validate


def run(argv, settings=None):
    p = subprocess.run(argv, text=True, capture_output=True, check=False)
    if p.stdout:
        print(p.stdout, end="")
    if p.stderr:
        print(p.stderr, end="", file=sys.stderr)
    if p.returncode != 0:
        raise StepError(f"命令失败 {p.returncode}: {argv[:2]}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date")
    p.add_argument("--settings", default=None)
    p.add_argument("--config-dir", default=None)  # 兼容旧调用
    p.add_argument("--schedule-apply", action="store_true")
    p.add_argument("--require-enabled", action="store_true")
    p.add_argument("--weather-file")
    p.add_argument("--lock-file", default=None)
    a = p.parse_args()
    try:
        settings, tz = bootstrap(a.settings)
        if a.require_enabled and settings.get("runtime.enabled") is not True:
            raise StepError("Step 3 未启用（runtime.enabled != true），cron 安全退出")

        pkg = package_root(__file__)
        workspace = Path(settings.get("runtime.dirs.daily") or "daily")
        if not workspace.is_absolute():
            workspace = settings.root_dir / workspace
        target = a.date or datetime.now(tz).date().isoformat()
        daily = workspace / f"{target}.json"
        staging = settings.root_dir / "var" / "planner"
        staging.mkdir(parents=True, exist_ok=True)
        planner = staging / f"{target}.planner.json"
        lock = Path(a.lock_file) if a.lock_file else settings.root_dir / "var" / "lock" / "morning-pipeline-step03.lock"

        with file_lock(lock):
            if not daily.exists():
                cmd = [
                    sys.executable, str(pkg / "scripts" / "build_daily_plan.py"),
                    "--date", target, "--settings", str(settings.path),
                    "--output", str(planner),
                ]
                if a.weather_file:
                    cmd += ["--weather-file", a.weather_file]
                run(cmd, settings)
                run([
                    sys.executable, str(pkg / "scripts" / "initialize_daily_file.py"),
                    "--planner", str(planner), "--settings", str(settings.path),
                    "--output", str(daily),
                ], settings)
            cmd = [
                sys.executable, str(pkg / "scripts" / "schedule_step03_events.py"),
                "--settings", str(settings.path), "--daily-file", str(daily),
            ]
            if a.schedule_apply:
                cmd.append("--apply")
            run(cmd, settings)
        return 0
    except (StepError, ValueError, KeyError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
