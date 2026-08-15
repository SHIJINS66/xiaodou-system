#!/usr/bin/env python3
"""run_morning_pipeline — step02 晨间编排（等价迁移自旧 step02/bin/run_morning_pipeline.py）。

职责：若当日 daily 不存在则走  build_daily_plan → initialize_daily_file 生成它，
然后跑 schedule_daily_events（默认 stub-only 不真发）。

等价迁移保留的线上行为：
    - --schedule-apply 必须同时 --stub-only（真实 at 调度走 step03 编排器，
      step02 只负责 stub 登记，避免双发）
    - 文件锁串行，防并发同一天重复写
    - require_enabled 校验（enable 标记）

适配点：
    - 配置从 runtime.json 改为 settings；enable 标记从 settings runtime 读
    - 路径全由 settings/包定位派生，不再写 /var/lib、/etc/xiaodou
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
    p.add_argument("--stub-only", action="store_true")
    p.add_argument("--require-enabled", action="store_true")
    p.add_argument("--weather-file")
    p.add_argument("--lock-file", default=None)
    a = p.parse_args()
    try:
        if a.schedule_apply and not a.stub_only:
            raise StepError("真实 at 调度必须同时指定 --stub-only（走 step03 编排器）")
        settings, tz = bootstrap(a.settings)
        if a.require_enabled and settings.get("runtime.enabled") is not True:
            raise StepError("Step 2 未启用（runtime.enabled != true），cron 安全退出")

        pkg = package_root(__file__)
        workspace = Path(settings.get("runtime.dirs.daily") or "daily")
        if not workspace.is_absolute():
            workspace = settings.root_dir / workspace
        target = a.date or datetime.now(tz).date().isoformat()
        daily = workspace / f"{target}.json"
        staging = settings.root_dir / "var" / "planner"
        staging.mkdir(parents=True, exist_ok=True)
        planner = staging / f"{target}.planner.json"
        lock = Path(a.lock_file) if a.lock_file else settings.root_dir / "var" / "lock" / "morning-pipeline.lock"

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
                sys.executable, str(pkg / "scripts" / "schedule_daily_events.py"),
                "--daily-file", str(daily), "--settings", str(settings.path),
            ]
            if a.schedule_apply:
                cmd += ["--apply", "--stub-only"]
            run(cmd, settings)
        return 0
    except (StepError, ValueError, KeyError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
