#!/usr/bin/env python3
"""finalize_yesterday — 薄壳：编排 finalize_day 处理「昨天」的那一天。

用法：python finalize_yesterday.py --settings ./instance/settings.yaml [--apply]
把 --date（昨天）补上，其余参数原样透传给 finalize_day。
子进程 env 由 finalize_day 内部按 settings 构造（它读 config['_settings']），
这里直接继承当前环境即可，无需额外 FIXED_ENV。
"""
from __future__ import annotations
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from common import make_tz


def _tz_from_settings(settings_val: str) -> str:
    import settings_loader as sl
    settings = sl.load_settings(settings_val)
    return (settings.get("runtime") or {}).get("timezone") or "Asia/Shanghai"


def main() -> int:
    args = sys.argv[1:]
    settings_val = None
    i = 0
    while i < len(args):
        if args[i] == "--settings" and i + 1 < len(args):
            settings_val = args[i + 1]
            break
        i += 1
    tz = make_tz(_tz_from_settings(settings_val) if settings_val else "Asia/Shanghai")
    date = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    command = [sys.executable, str(Path(__file__).with_name("finalize_day.py")), "--date", date, *args]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
