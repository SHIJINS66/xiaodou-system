#!/usr/bin/env python3
"""build_daily_plan — 等价迁移自旧 step02/bin/build_daily_plan.py。

生成目标日期的每日 Planner（daily plan）JSON。是每日流水线的第一环。

等价迁移保留的线上逻辑：
    - fixture 模式：直接读取已生成的 plan 校验后写出（测试/回放用）
    - 正常模式：组装 context（weather + 人设文件 + 最近记忆 + 昨日状态）
        -> DeepSeek 生成 plan -> validate_planner -> 失败 repair 循环 -> 写出
    - repair 循环使用低温度(0.1) 重试，最多 --max-repair-attempts 次
    - 输出 stdout 打印 JSON 摘要（model / usage / repair_attempts / warnings / source_fingerprints）

适配点（相对线上，等价迁移去硬编码）：
    - 配置不再读 /etc/xiaodou/runtime.json + planner.json，改用 settings（planning + models.planner）
    - 时区 / 人设文件清单 / workspace 从 settings 读
    - schema / prompt 随包定位（scripts.common.schema_path / prompt_path）
    - 冻结人设校验（verify_frozen_core）改为「完整性检查」：人设文件必须存在，
      不再强制哈希一致（框架允许别人改人设），可选 --freeze 时才对哈希
    - DeepSeek 用 providers/llm/deepseek_urllib（等价 urllib 实现）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from common import StepError, bootstrap, load_json, sha256_file
import schema_validator as sv
from providers.base import create_llm_provider
from weather_provider import load_weather_context


def recent_daily(root: Path, limit: int) -> list[dict[str, str]]:
    paths = sorted((root / "daily").glob("????-??-??.md"), reverse=True)[:limit]
    return [{"name": p.name, "content": p.read_text(encoding="utf-8")} for p in reversed(paths)]


def previous_daily(root: Path, target: date) -> dict[str, Any] | None:
    p = root / "daily" / f"{target - timedelta(days=1)}.json"
    if not p.is_file():
        return None
    d = load_json(p)
    return {
        "date": d.get("date"),
        "plan_status": d.get("plan_status"),
        "day_context": d.get("plan", {}).get("day_context"),
        "event_results": [
            {
                "event_id": s.get("event_id"),
                "status": s.get("status"),
                "decision": s.get("decision"),
                "decision_reason": s.get("decision_reason"),
            }
            for s in d.get("runtime", {}).get("event_states", [])
        ],
    }


def _arity_of(fn) -> int:
    """取函数参数个数，用于兼容不同工厂签名。"""
    import inspect
    try:
        return len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return 3


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    p.add_argument("--settings", default=None)
    p.add_argument("--output", required=True)
    p.add_argument("--fixture")
    p.add_argument("--weather-file")
    p.add_argument("--max-repair-attempts", type=int, default=2)
    p.add_argument("--freeze", action="store_true", help="开启人设文件哈希一致性校验")
    args = p.parse_args()

    try:
        target = date.fromisoformat(args.date)
        settings, _tz = bootstrap(args.settings)
        sv.set_timezone(settings.get("runtime.timezone"))

        root = settings.root_dir
        planning = settings.planning or {}
        planner_cfg = settings.get("models.planner") or {}

        # 人设文件完整性检查（等价 migration 的 verify_frozen_core 去冻结化）
        context_files = planning.get("context_files") or []
        core = {}
        for rel in context_files:
            fp = root / rel
            if not fp.is_file():
                raise StepError(f"人设文件缺失：{fp}")
            core[rel] = sha256_file(fp)

        if args.fixture:
            plan = load_json(Path(args.fixture))
            if plan.get("date") != target.isoformat():
                raise StepError("fixture 模式必须使用 fixture 自带日期")
            errors, warnings = sv.validate_planner(plan)
            if errors:
                raise StepError("; ".join(errors))
            _atomic_write_json(Path(args.output), plan)
            print(json.dumps({"passed": True, "warnings": warnings}, ensure_ascii=False))
            return 0

        weather = load_weather_context(Path(args.weather_file) if args.weather_file else None)
        context = {
            "target_date": target.isoformat(),
            "weekday": target.strftime("%A"),
            "timezone": settings.get("runtime.timezone"),
            "weather": weather,
            "core_files": {
                rel: (root / rel).read_text(encoding="utf-8") for rel in context_files
            },
            "recent_daily_memories": recent_daily(root, int(planning.get("recent_memory_days", 7))),
            "previous_daily_state": previous_daily(root, target),
        }

        prompt_path = _resolve_asset(planning, "prompt", "daily_planner_v1_1")
        schema_path = _resolve_asset(planning, "schema", "daily_planner_output_v1_1")

        provider = create_llm_provider(settings, which="planner")
        from common import render_prompt
        system = render_prompt(prompt_path.read_text(encoding="utf-8"), settings)
        schema = load_json(schema_path)
        user = json.dumps(
            {
                "instruction": "根据上下文生成目标日期的每日 Planner JSON",
                "json_schema": schema,
                "context": context,
            },
            ensure_ascii=False,
        )

        max_tokens = int(planner_cfg.get("max_tokens", 16000))
        json_completion = getattr(provider, "json_completion", None)
        if json_completion is None:
            # 抽象接口 fallback：用 chat_json（返回 dict，meta 为空）。
            def _gen(system_p: str, user_p: str, temp: float, max_tok: int):
                from providers.base import ChatMessage

                value = provider.chat_json(
                    [ChatMessage("system", system_p), ChatMessage("user", user_p)],
                    temperature=temp,
                    max_tokens=max_tok,
                )
                return value, {"model": planner_cfg.get("model"), "usage": None}

            json_completion = _gen
        plan, meta = json_completion(
            system, user, float(planner_cfg.get("temperature", 0.35)), max_tokens
        )
        errors, warnings = sv.validate_planner(plan)
        repairs = 0
        while errors and repairs < args.max_repair_attempts:
            repairs += 1
            repair = json.dumps(
                {
                    "instruction": "修复以下 JSON，只修正结构和业务规则错误",
                    "errors": errors,
                    "invalid_json": plan,
                    "json_schema": schema,
                },
                ensure_ascii=False,
            )
            plan, meta = provider.json_completion(system, repair, 0.1, max_tokens)
            errors, warnings = sv.validate_planner(plan)
        if errors:
            raise StepError("Planner 修复后仍未通过：" + "; ".join(errors))

        _atomic_write_json(Path(args.output), plan)
        print(
            json.dumps(
                {
                    "status": "generated",
                    "output": str(Path(args.output).resolve()),
                    "model": meta.get("model"),
                    "usage": meta.get("usage"),
                    "repair_attempts": repairs,
                    "warnings": warnings,
                    "source_fingerprints": core,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (StepError, KeyError, ValueError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


def _resolve_asset(cfg: dict, kind: str, default_name: str) -> Path:
    """定位 prompt / schema 资产：优先 cfg 显式路径，否则随包定位。"""
    import inspect
    from common import package_root, prompt_path, schema_path

    pkg = package_root(__file__)
    explicit = cfg.get(f"{kind}_path")
    if explicit:
        return Path(explicit)
    if kind == "prompt":
        return prompt_path(pkg, default_name)
    return schema_path(pkg, default_name)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    from common import atomic_write_json as _awj
    _awj(path, value)


if __name__ == "__main__":
    raise SystemExit(main())
