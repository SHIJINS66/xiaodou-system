#!/usr/bin/env python3
"""schema_validator — 等价迁移自旧 step02/bin/schema_validator.py。

校验对像：
    validate_planner(plan)  -> daily_planner_output_v1_1.schema.json 的结构 + 业务规则
    validate_daily(daily)   -> daily_file_v1_1.schema.json 的结构 + 业务规则

保留的线上业务校验（逐条对齐，等价迁移）：
    - schema_version 必须 '1.1'
    - 所有时间必须显式 +08:00，并 astimezone 到模块时区
    - timeline 必须从当天 00:00 连续覆盖到次日 00:00，首尾/相邻严格连续
    - event_id 必须 YYYYMMDD-eNN 且日期前缀正确
    - time_window 必须 earliest <= preferred <= latest 且落在 segment 内
    - 不允许在 NO_OUTBOUND 回复态安排主动联系
    - 不能连续两个 selfie；20 分钟内三个主动事件给 warning
    - interaction_budget 必须与事件计数全等
    - 普通日 total 5~8 且需 exception_reason 说明例外
    - validate_daily：runtime 事件必须 YYYYMMDD-rNN、event_states 与事件一一对应、
      session_injected 要求 telegram_sent=true

适配点（相对线上）：
    - 错误类型统一用 StepError（替代 Step02Error）。
    - 时区不再写死字符串，改从 settings 读（模块级 init 时取）。
    - schema 文件用 scripts.common.schema_path(package_root, name) 定位，不写 /opt/xiaodou。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from common import StepError, load_json, make_tz, package_root, parse_iso, schema_path

# ---- 包级初始化：schema 随包定位，时区从 settings 读 ----
_PKG = package_root(__file__)
_PLANNER_SCHEMA = load_json(schema_path(_PKG, "daily_planner_output_v1_1"))
_DAILY_SCHEMA = load_json(schema_path(_PKG, "daily_file_v1_1"))

from schema_engine import audit_schema, validate  # noqa: E402

_TZ = make_tz("Asia/Shanghai")  # 缺省，入口 set_timezone 会覆盖


def set_timezone(tzname: str) -> None:
    """由脚本入口调用，把 settings 注入的时区写进模块级（供校验用）。"""
    global _TZ
    _TZ = make_tz(tzname)

NO_OUTBOUND = {"focused_no_reply", "sleeping", "washing_no_reply"}
REPLY = {
    "available", "available_short_reply", "commuting_chatty", "focused_no_reply",
    "sleeping", "washing_no_reply", "resting_low_contact", "overtime_low_contact",
}
TYPES = {"silent", "chat", "status", "selfie"}
PLANNER_ID = re.compile(r"^[0-9]{8}-e[0-9]{2}$")
RUNTIME_ID = re.compile(r"^[0-9]{8}-r[0-9]{2}$")


def validate_planner(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors = audit_schema(_PLANNER_SCHEMA) + validate(plan, _PLANNER_SCHEMA)
    warnings: list[str] = []
    if errors:
        return errors, warnings
    if plan.get("schema_version") != "1.1":
        errors.append("schema_version: 必须为 1.1")
    if plan.get("timezone") != "Asia/Shanghai":
        errors.append("timezone: 必须为 Asia/Shanghai")
    try:
        target = datetime.fromisoformat(str(plan.get("date"))).date()
    except ValueError:
        return ["date: 必须为 YYYY-MM-DD"], warnings
    ctx = plan.get("day_context")
    budget = plan.get("interaction_budget")
    timeline = plan.get("timeline")
    events = plan.get("events")
    if not isinstance(ctx, dict):
        errors.append("day_context: 必须是对象")
    if not isinstance(budget, dict):
        errors.append("interaction_budget: 必须是对象")
    if not isinstance(timeline, list):
        errors.append("timeline: 必须是数组")
    if not isinstance(events, list):
        errors.append("events: 必须是数组")
    if errors:
        return errors, warnings

    segs: dict[str, Any] = {}
    parsed: list[tuple[datetime, datetime, Any]] = []
    for i, seg in enumerate(timeline):
        p = f"timeline[{i}]"
        if not isinstance(seg, dict):
            errors.append(f"{p}: 必须是对象")
            continue
        req = {
            "segment_id", "start_at", "end_at", "location_type", "location_label",
            "activity", "reply_state", "mood", "attire", "continuity_note",
        }
        if req - set(seg):
            errors.append(f"{p}: 缺少字段 {sorted(req - set(seg))}")
            continue
        sid = seg["segment_id"]
        if sid in segs:
            errors.append(f"{p}.segment_id: 重复")
        segs[sid] = seg
        if seg["reply_state"] not in REPLY:
            errors.append(f"{p}.reply_state: 无效")
        try:
            start = parse_iso(seg["start_at"], _TZ)
            end = parse_iso(seg["end_at"], _TZ)
        except StepError as exc:
            errors.append(f"{p}: {exc}")
            continue
        if start.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8):
            errors.append(f"{p}: 时间必须显式使用 +08:00")
        start = start.astimezone(_TZ)
        end = end.astimezone(_TZ)
        if start >= end:
            errors.append(f"{p}: start_at 必须早于 end_at")
        parsed.append((start, end, seg))

    parsed.sort(key=lambda x: x[0])
    if parsed:
        begin = datetime.combine(target, datetime.min.time(), tzinfo=_TZ)
        finish = begin + timedelta(days=1)
        if parsed[0][0] != begin:
            errors.append("timeline: 必须从当天 00:00 开始")
        if parsed[-1][1] != finish:
            errors.append("timeline: 必须覆盖到次日 00:00")
        for a, b in zip(parsed, parsed[1:]):
            if a[1] != b[0]:
                errors.append("timeline: 片段必须连续且不能重叠或留空")

    ids: set[str] = set()
    counter: Counter[str] = Counter()
    preferred: list[tuple[datetime, str, str]] = []
    for i, e in enumerate(events):
        p = f"events[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{p}: 必须是对象")
            continue
        req = {
            "event_id", "origin", "created_at", "created_by", "runtime_reason",
            "supersedes_event_id", "type", "segment_id", "time_window", "priority",
            "intent", "topic_seed", "message_constraints", "context_requirements",
            "cancel_conditions",
        }
        if req - set(e):
            errors.append(f"{p}: 缺少字段 {sorted(req - set(e))}")
            continue
        eid = e["event_id"]
        if eid in ids:
            errors.append(f"{p}.event_id: 重复")
        ids.add(eid)
        if not PLANNER_ID.match(str(eid)):
            errors.append(f"{p}.event_id: 必须为 YYYYMMDD-eNN")
        if not str(eid).startswith(target.strftime("%Y%m%d")):
            errors.append(f"{p}.event_id: 日期前缀错误")
        if (
            e["origin"] != "planner"
            or e["created_by"] != "planner"
            or e["runtime_reason"] is not None
            or e["supersedes_event_id"] is not None
        ):
            errors.append(f"{p}: Planner 来源字段错误")
        typ = e["type"]
        counter[typ] += 1
        if typ not in TYPES:
            errors.append(f"{p}.type: 无效")
            continue
        seg = segs.get(e["segment_id"])
        if not seg:
            errors.append(f"{p}.segment_id: 引用不存在")
            continue
        try:
            created = parse_iso(e["created_at"], _TZ)
            w = e["time_window"]
            early = parse_iso(w["earliest_at"], _TZ)
            pref = parse_iso(w["preferred_at"], _TZ)
            late = parse_iso(w["latest_at"], _TZ)
            if any(x.utcoffset() != timedelta(hours=8) for x in (created, early, pref, late)):
                errors.append(f"{p}: 时间必须显式使用 +08:00")
            created = created.astimezone(_TZ)
            early = early.astimezone(_TZ)
            pref = pref.astimezone(_TZ)
            late = late.astimezone(_TZ)
            ss = parse_iso(seg["start_at"], _TZ).astimezone(_TZ)
            se = parse_iso(seg["end_at"], _TZ).astimezone(_TZ)
        except (KeyError, StepError) as exc:
            errors.append(f"{p}.time_window: {exc}")
            continue
        if not early <= pref <= late:
            errors.append(f"{p}.time_window: 必须 earliest <= preferred <= latest")
        if created.date() != target:
            errors.append(f"{p}.created_at: 必须属于目标日期")
        if created > early:
            errors.append(f"{p}.created_at: 不能晚于 earliest_at")
        if early < ss or late > se:
            errors.append(f"{p}.time_window: 必须位于 segment 内")
        if typ != "silent":
            if seg["reply_state"] in NO_OUTBOUND:
                errors.append(f'{p}: 不能在 {seg["reply_state"]} 主动联系')
            preferred.append((pref, eid, typ))
        if typ == "selfie" and "selfie_spec" not in e:
            errors.append(f"{p}: selfie 缺少 selfie_spec")
        if typ != "selfie" and "selfie_spec" in e:
            errors.append(f"{p}: 非 selfie 不得包含 selfie_spec")

    preferred.sort()
    seen: dict[str, str] = {}
    for t, eid, _ in preferred:
        if t in seen:
            errors.append(f"events: {eid} 与 {seen[t]} 使用相同 preferred_at")
        seen[t] = eid
    for i in range(len(preferred) - 2):
        if preferred[i + 2][0] - preferred[i][0] < timedelta(minutes=20):
            warnings.append(
                f"events: 20 分钟内安排三个主动事件 {[x[1] for x in preferred[i : i + 3]]}"
            )
    for a, b in zip(preferred, preferred[1:]):
        if a[2] == "selfie" and b[2] == "selfie":
            errors.append("events: 不能连续安排两个 selfie")

    actual = counter["chat"] + counter["status"] + counter["selfie"]
    expected = {
        "total_target": actual,
        "chat_target": counter["chat"],
        "status_target": counter["status"],
        "selfie_target": counter["selfie"],
        "silent_target": counter["silent"],
    }
    for k, v in expected.items():
        if budget.get(k) != v:
            errors.append(f"interaction_budget.{k}: 应为 {v}")
    exception = ctx.get("exception_reason")
    if not 5 <= actual <= 8 and not exception:
        errors.append("interaction_budget.total_target: 普通范围为 5～8，例外必须说明原因")
    if not exception and ctx.get("workload") == "normal":
        if not 3 <= counter["chat"] <= 4:
            warnings.append("interaction_budget.chat_target: 普通日通常 3～4")
        if not 1 <= counter["selfie"] <= 2:
            warnings.append("interaction_budget.selfie_target: 普通日通常 1～2")
    return errors, warnings


def validate_daily(daily: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors = audit_schema(_DAILY_SCHEMA) + validate(daily, _DAILY_SCHEMA)
    warnings: list[str] = []
    if errors:
        return errors, warnings
    if daily.get("schema_version") != "1.1":
        errors.append("schema_version: 必须为 1.1")
    plan = daily.get("plan")
    if not isinstance(plan, dict):
        return errors + ["plan: 必须是对象"], warnings
    pe, pw = validate_planner(plan)
    errors += ["plan." + x for x in pe]
    warnings += ["plan." + x for x in pw]
    if daily.get("date") != plan.get("date"):
        errors.append("date: 必须与 plan.date 一致")
    rt = daily.get("runtime")
    if not isinstance(rt, dict):
        return errors + ["runtime: 必须是对象"], warnings
    runtime_events = rt.get("runtime_events", [])
    states = rt.get("event_states", [])
    ids = {e.get("event_id") for e in plan.get("events", []) if isinstance(e, dict)}
    for i, e in enumerate(runtime_events):
        if not isinstance(e, dict):
            errors.append(f"runtime.runtime_events[{i}]: 必须是对象")
            continue
        eid = e.get("event_id")
        if not RUNTIME_ID.match(str(eid)):
            errors.append(f"runtime.runtime_events[{i}].event_id: 必须为 YYYYMMDD-rNN")
        if not str(eid).startswith(str(daily.get("date", "")).replace("-", "")):
            errors.append(f"runtime.runtime_events[{i}].event_id: 日期前缀错误")
        if e.get("origin") != "runtime" or not e.get("runtime_reason"):
            errors.append(f"runtime.runtime_events[{i}]: runtime 来源字段错误")
        if eid in ids:
            errors.append(f"runtime.runtime_events[{i}].event_id: 重复")
        ids.add(eid)
    state_ids = [s.get("event_id") for s in states if isinstance(s, dict)]
    if set(state_ids) != ids or len(state_ids) != len(ids):
        errors.append("runtime.event_states: 必须与全部事件一一对应")
    for i, s in enumerate(states):
        if isinstance(s, dict) and s.get("session_injected") and not s.get("telegram_sent"):
            errors.append(f"runtime.event_states[{i}]: session_injected 要求 telegram_sent=true")
    return errors, warnings
