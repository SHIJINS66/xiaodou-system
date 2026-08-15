#!/usr/bin/env python3
"""validate_daily_plan — 等价迁移自旧 step02/bin/validate_daily_plan.py。

对 planner 输出或完整 daily 文件做结构 + 业务规则校验，输出 JSON 报告。
是调度/执行链路的入口校验器，也是手工检查工具。

等价迁移保留的线上行为：
    - --kind planner|daily 二选一必填
    - 校验通过输出 {generated_at, kind, path, errors, warnings, passed}
    - --report 额外把报告写入指定文件
    - 有 errors 时 exit 1，无 errors exit 0；配置/读取错误 exit 2

适配点（相对线上）：
    - 时区 / schema 路径走 settings（与 build_daily_plan 同一套 loading）
    - 错误类型统一 StepError
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import StepError, bootstrap, load_json, now_iso
import schema_validator as sv


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--kind", choices=["planner", "daily"], required=True)
    p.add_argument("--settings", default=None)
    p.add_argument("--report")
    args = p.parse_args()

    try:
        settings, tz = bootstrap(args.settings)
        sv.set_timezone(settings.get("runtime.timezone"))
        value = load_json(Path(args.path))
        errors, warnings = (
            sv.validate_planner(value) if args.kind == "planner" else sv.validate_daily(value)
        )
        report = {
            "generated_at": now_iso(tz),
            "kind": args.kind,
            "path": str(Path(args.path).resolve()),
            "errors": errors,
            "warnings": warnings,
            "passed": not errors,
        }
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.report:
            Path(args.report).write_text(text + "\n", encoding="utf-8")
        return 0 if not errors else 1
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
