#!/usr/bin/env python3
"""at_adapter — 等价迁移自旧 step03/bin/at_adapter.py。

封装 at / atq 交互与 marker 生命周期，供调度器（schedule_step03_events.py）使用。

等价迁移保留的线上行为：
    - marker(daily, event_id)：稳定标记，SHA256(daily:event_id) 前 24 位
    - command(...)：拼 executor 调用命令（env MARKER = token）
    - queued_jobs / jobs_with_marker：检索 at 队列里带指定 marker 的作业
    - submit(...)：先查重（重复则报错/复用），未命中才 at -t 提交并回读唯一 job id；
      at 返回的 job id 与队列标记不一致时报错
    - 环境白名单（env）：只允许固定键，绝不携带调用者密钥

适配点（相对线上）：
    - marker 前缀与环境变量名由 settings.system.instance_name 派生（替代 xd03- /
      XIAODOU_STEP03_MARKER），经 common.marker_env_var / event_marker_id
    - /usr/bin/at、/usr/bin/atq 路径由 settings.scheduling.scheduler.at_bin /
      atq_bin 提供；AT_ENV 由 common.make_at_env(settings) 生成
    - 错误统一 StepError
"""
from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (StepError, event_marker_id, make_at_env, marker_env_var, now_iso)

JOB_RE = re.compile(r"\bjob\s+(\d+)\b", re.I)


def at_bin(settings: Any) -> str:
    return (settings.get("scheduling.scheduler.at_bin") or "/usr/bin/at")


def atq_bin(settings: Any) -> str:
    return (settings.get("scheduling.scheduler.atq_bin") or "/usr/bin/atq")


def marker(settings: Any, daily_path: Path, event_id: str) -> str:
    """稳定执行标记（对应线上 xd03-<digest>）。"""
    return event_marker_id(settings, daily_path, event_id)


def at_timestamp(when: datetime) -> str:
    return when.strftime("%Y%m%d%H%M.%S")


def command(
    settings: Any,
    executor: Path,
    python_bin: Path,
    config: Path,
    daily: Path,
    date: str,
    event_id: str,
    token: str,
) -> str:
    env_var = marker_env_var(settings)
    parts = [
        "env", f"{env_var}={token}", str(python_bin), str(executor),
        "--config", str(config), "--date", date, "--event-id", event_id, "--daily-file", str(daily),
    ]
    return " ".join(shlex.quote(item) for item in parts)


def queued_jobs(settings: Any) -> list[str]:
    env = make_at_env(settings)
    result = subprocess.run([atq_bin(settings)], text=True, capture_output=True, timeout=20, check=False, env=env)
    if result.returncode:
        raise StepError("atq failed")
    return [line.split()[0] for line in result.stdout.splitlines() if line.split() and line.split()[0].isdigit()]


def jobs_with_marker(settings: Any, token: str) -> list[str]:
    env = make_at_env(settings)
    env_var = marker_env_var(settings)
    probe = f"{env_var}={token}"
    found: list[str] = []
    for job in queued_jobs(settings):
        result = subprocess.run([at_bin(settings), "-c", job], text=True, capture_output=True, timeout=20, check=False, env=env)
        if result.returncode == 0 and probe in result.stdout:
            found.append(job)
    return found


def submit(
    settings: Any,
    executor: Path,
    python_bin: Path,
    config: Path,
    daily: Path,
    date: str,
    event_id: str,
    when: datetime,
) -> dict[str, Any]:
    token = marker(settings, daily, event_id)
    existing = jobs_with_marker(settings, token)
    if len(existing) > 1:
        raise StepError(f"multiple at jobs with marker for {event_id}")
    if existing:
        return {"job_id": existing[0], "marker": token, "recovered": True}
    body = command(settings, executor, python_bin, config, daily, date, event_id, token)
    env = make_at_env(settings)
    result = subprocess.run([at_bin(settings), "-t", at_timestamp(when)], input=body + "\n", text=True, capture_output=True, timeout=30, check=False, env=env)
    if result.returncode:
        raise StepError(f"at submit failed for {event_id}")
    reported = JOB_RE.search(result.stderr + "\n" + result.stdout)
    recovered = jobs_with_marker(settings, token)
    if len(recovered) != 1:
        raise StepError(f"cannot uniquely recover at job for {event_id}")
    if reported and reported.group(1) != recovered[0]:
        raise StepError(f"at returned job id mismatch for {event_id}")
    return {"job_id": recovered[0], "marker": token, "recovered": False}
