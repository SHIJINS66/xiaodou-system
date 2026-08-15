#!/usr/bin/env python3
"""execute_daily_event — 等价迁移自旧 step03/bin/execute_daily_event.py。

at 触发后执行一个每日事件。核心是状态机 + 确定性门 + 事务 + 日志，最终
内容生成（文字/自拍）与投递（Telegram + 网关 session 注入）由 provider 抽象注入。

等价迁移保留的线上行为（逐条对齐）：
    - 生命周期锁 + 事件锁（阻塞/非阻塞两级）
    - 事务状态机（event_transaction）：acquired→gated→decision_ready→prepared
      →telegram_sending→telegram_sent→injecting→injected→completed，及失败/取消/跳过
    - 幂等恢复（重新触发时从 at 队列 marker / telegram_sent / injection 恢复）
    - Gate 1（获取锁后）+ Gate 2（内容生成后、发送前）双重 hard gate
    - 会话一致性校验（多处 session_id 一致性检查，防止 rollover 串 session）
    - do-not-disturb 不可被模型覆盖
    - 全链路 schema 校验 + 原子持久化 + journal 追溯

适配点（相对线上）：
    - 配置从 step03.json 改为 settings（路径由 common.bootstrap + package_root 派生，
      provider 走 providers 工厂）
    - 内容/投递经 provider 抽象（LLM / image / delivery / gateway），
      测试可注入 stub，不接真实发送
    - 内部 marker 由 settings 派生（internal_marker / event_marker_id）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from common import (StepError, all_events, atomic_write_json, bootstrap, bump_daily,
                    event_marker_id, file_lock, find_event_and_state, instance_marker,
                    internal_marker_regex, load_json, now_iso, package_root, sha256_bytes)
from context_snapshot import build as build_snapshot
from deterministic_gate import evaluate
from event_journal import append as journal_append
from event_transaction import load_or_create, save as save_transaction, transaction_path, transition
from message_generation import generate as generate_message
from providers.openclaw_gateway import SessionHistory, contains_marker, current_session_history, inject, session_id
from schema_tools import validate
from providers.image.seedream import build_prompt


def _segment(daily: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    for item in daily["plan"]["timeline"]:
        if item["segment_id"] == event["segment_id"]:
            return item
    raise StepError(f"segment missing: {event['segment_id']}")


def _resolve_paths(settings, daily_path: Path) -> dict:
    """把线上 config 里的可写路径映射到 settings 实例目录。"""
    from common import package_root as _pkg  # noqa
    pkg = _pkg(__file__)
    state_root = settings.root_dir / "var" / "state"
    journal_root = settings.root_dir / "var" / "journal"
    return {
        "state_root": state_root,
        "journal_root": journal_root,
        "selfie_output_root": settings.dir("selfies"),
        "daily_lock_file": settings.root_dir / "var" / "lock" / "daily.lock",
        "journal_lock_file": settings.root_dir / "var" / "lock" / "journal.lock",
        "event_lock_root": settings.root_dir / "var" / "lock" / "events",
        "daily_schema_path": pkg / "schemas" / "daily_file_v1_1.schema.json",
        "transaction_schema_path": pkg / "schemas" / "event_transaction_v1.schema.json",
        "context_schema_path": pkg / "schemas" / "context_snapshot_v1.schema.json",
        "env_file": settings.root_dir / (settings.get("runtime.env_file") or ".env"),
    }


def _session_history(settings, history_file: str | None) -> SessionHistory:
    if history_file:
        value = json.loads(Path(history_file).read_text(encoding="utf-8"))
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise StepError("history fixture must be array of objects")
        return SessionHistory(session_id=None, messages=value)
    return current_session_history(settings)


def _lifecycle_lock(daily_path: Path, history_file: str | None) -> Path:
    return daily_path.parent / ".step03-test" / "session-lifecycle.lock" if history_file else daily_path.parent / "var" / "lock" / "session-lifecycle.lock"


def _read_daily(runtime: dict, daily_path: Path, event_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with file_lock(Path(runtime["daily_lock_file"])):
        daily = load_json(daily_path)
        validate(daily, Path(runtime["daily_schema_path"]))
        event, state = find_event_and_state(daily, event_id)
        return daily, event, state


def _daily_state(runtime: dict, daily_path: Path, event_id: str, status: str, reason: str, **updates: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with file_lock(Path(runtime["daily_lock_file"])):
        daily = load_json(daily_path)
        validate(daily, Path(runtime["daily_schema_path"]))
        event, state = find_event_and_state(daily, event_id)
        if status and status != state["status"]:
            old = state["status"]
            state["status"] = status
            state["history"].append({"at": now_iso(), "from_status": old, "to_status": status, "reason": reason})
        state.update(updates)
        bump_daily(daily)
        validate(daily, Path(runtime["daily_schema_path"]))
        atomic_write_json(daily_path, daily)
        return daily, event, state


def _journal(runtime: dict, date: str, txn: dict[str, Any], from_phase: str, to_phase: str, reason: str, **extra: Any) -> None:
    journal_append(
        Path(runtime["journal_root"]),
        date,
        {"event_id": txn["event_id"], "execution_id": txn["execution_id"], "from_phase": from_phase, "to_phase": to_phase, "reason": reason, **extra},
        Path(runtime["journal_lock_file"]),
    )


def _fail_before_send(runtime: dict, daily_path: Path, txn_path: Path, txn: dict[str, Any], event_id: str, code: str, exc: Exception) -> dict[str, Any]:
    if txn["phase"] not in {"failed", "cancelled", "skipped", "delivery_unknown", "completed"}:
        txn = transition(
            txn_path,
            Path(runtime["transaction_schema_path"]),
            txn,
            "failed",
            code,
            error={"code": code, "message": type(exc).__name__, "retryable": False},
        )
    _daily_state(
        runtime,
        daily_path,
        event_id,
        "failed",
        code,
        error={"code": code, "message": "failure occurred before Telegram send", "retryable": False, "attempts": txn["attempt_count"]},
        completed_at=now_iso(),
        at_job_id=None,
    )
    return txn


def _bind_session(runtime: dict, txn_path: Path, txn: dict[str, Any], current_session_id: str | None) -> dict[str, Any]:
    if current_session_id is None:
        return txn
    stored = txn.get("session_id")
    if stored is None:
        if txn["telegram"]["sent"]:
            raise StepError("legacy sent transaction has no session binding; manual reconciliation required")
        txn["session_id"] = current_session_id
        return save_transaction(txn_path, Path(runtime["transaction_schema_path"]), txn)
    if stored != current_session_id:
        raise StepError("active session changed for this transaction")
    return txn


def _complete_injection(settings, runtime: dict, txn_path: Path, txn: dict[str, Any], expected_session_id: str | None, history_file: str | None = None) -> dict[str, Any]:
    schema = Path(runtime["transaction_schema_path"])
    marker = txn["injection"]["marker"]
    txn = transition(txn_path, schema, txn, "injecting", "history dedupe before chat.inject") if txn["phase"] != "injecting" else txn
    current = _session_history(settings, history_file)
    if expected_session_id is not None and current.session_id != expected_session_id:
        raise StepError("active session changed before chat.inject")
    if contains_marker(current.messages, marker):
        txn["injection"].update(injected=True, injected_at=now_iso())
        return transition(txn_path, schema, txn, "injected", "history already contains stable marker")
    result = inject(settings, txn["prepared"]["transcript"])
    txn["injection"].update(injected=True, message_id=result["message_id"], injected_at=now_iso())
    return transition(txn_path, schema, txn, "injected", "chat.inject succeeded")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)  # 兼容旧调用；优先 --settings
    parser.add_argument("--settings", default=None)
    parser.add_argument("--date", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--daily-file", required=True)
    parser.add_argument("--history-file", help="fixture only; never used by cron")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        settings_arg = args.settings or args.config  # --config 兼容旧调用：优先 --settings
        settings, _tz = bootstrap(settings_arg)
        daily_path = Path(args.daily_file).resolve()
        daily = load_json(daily_path)
        runtime = _resolve_paths(settings, daily_path)
        validate(daily, Path(runtime["daily_schema_path"]))
        if daily["date"] != args.date:
            raise StepError("date does not match daily file")
        event, state = find_event_and_state(daily, args.event_id)
        scope = _session_history(settings, args.history_file)
        snapshot = build_snapshot(settings, daily, event, scope.messages)
        validate(snapshot, Path(runtime["context_schema_path"]))
        gate = evaluate(event, state, snapshot)
        if args.dry_run:
            print(json.dumps({"mode": "dry_run", "date": args.date, "event_id": args.event_id, "gate": gate, "external_calls": []}, ensure_ascii=False, indent=2))
            return 0

        event_lock = Path(runtime["event_lock_root"]) / f"{args.date}-{args.event_id}.lock"
        txn_path = transaction_path(Path(runtime["state_root"]), args.date, args.event_id)
        txn_schema = Path(runtime["transaction_schema_path"])
        with file_lock(event_lock, blocking=False):
            with file_lock(_lifecycle_lock(daily_path, args.history_file)):
                scope = _session_history(settings, args.history_file)
                txn = load_or_create(settings, txn_path, txn_schema, daily_path, args.date, args.event_id)
                if txn["phase"] == "completed":
                    print(json.dumps({"status": "idempotent_completed", "event_id": args.event_id}))
                    return 0
                if txn["phase"] == "delivery_unknown":
                    raise StepError("Telegram delivery is ambiguous; manual reconciliation required")
                txn = _bind_session(runtime, txn_path, txn, scope.session_id)
                if txn["telegram"]["sent"] and txn["phase"] in {"telegram_sent", "injecting", "injection_pending", "injection_unknown", "injected"}:
                    try:
                        if txn["phase"] != "injected":
                            txn = _complete_injection(settings, runtime, txn_path, txn, txn.get("session_id"), args.history_file)
                        txn = transition(txn_path, txn_schema, txn, "completed", "injection recovery completed")
                        _daily_state(runtime, daily_path, args.event_id, "completed", "injection recovery completed", session_injected=True, completed_at=now_iso(), at_job_id=None, error=None)
                        print(json.dumps({"status": "completed_after_injection_recovery", "event_id": args.event_id}))
                        return 0
                    except StepError as exc:
                        if txn["phase"] == "injecting":
                            txn = transition(txn_path, txn_schema, txn, "injection_pending", "Gateway injection remains pending", error={"code": "gateway_injection_failed", "message": str(exc), "retryable": True})
                        _daily_state(runtime, daily_path, args.event_id, "running", "Telegram sent; injection pending", error={"code": "gateway_injection_failed", "message": "chat.inject failed; Telegram will not be resent", "retryable": True, "attempts": txn["attempt_count"]}, at_job_id=None)
                        raise
                if txn["phase"] == "delayed":
                    txn["attempt_count"] += 1
                    txn = transition(txn_path, txn_schema, txn, "acquired", "legacy delayed event triggered again")
                if txn["phase"] != "acquired":
                    raise StepError(f"transaction phase not executable: {txn['phase']}")

                daily, event, state = _daily_state(
                    runtime, daily_path, args.event_id, "running", "executor acquired event lock",
                    attempt_count=min(10, state["attempt_count"] + 1),
                    started_at=state["started_at"] or now_iso(),
                    at_job_id=None,
                    error=None,
                )
                segment = _segment(daily, event)
                scope = _session_history(settings, args.history_file)
                txn = _bind_session(runtime, txn_path, txn, scope.session_id)
                snapshot = build_snapshot(settings, daily, event, scope.messages)
                validate(snapshot, Path(runtime["context_schema_path"]))
                gate = evaluate(event, state, snapshot)
                if not gate["allowed"]:
                    phase = "skipped" if gate["code"] in {"late_execution", "too_early", "silent_event"} else "cancelled"
                    old_phase = txn["phase"]
                    txn = transition(txn_path, txn_schema, txn, phase, gate["reason"], decision="cancel", decision_reason=gate["code"])
                    _daily_state(runtime, daily_path, args.event_id, phase, gate["reason"], decision="cancel", decision_reason=gate["code"], completed_at=now_iso(), at_job_id=None)
                    _journal(runtime, args.date, txn, old_phase, phase, gate["code"])
                    print(json.dumps({"status": phase, "gate": gate}))
                    return 0

                txn = transition(txn_path, txn_schema, txn, "gated", "deterministic Gate 1 passed")
                txn = transition(txn_path, txn_schema, txn, "decision_ready", "planned event approved for realization", decision="execute", decision_reason="planned_event")

                from providers.base import create_delivery_provider, create_image_provider, create_llm_provider, DeliveryError
                image_provider = create_image_provider(settings)
                delivery = create_delivery_provider(settings)
                llm_conf = settings.get("models.caption") or {}
                image: dict[str, Any] | None = None
                try:
                    if event["type"] == "selfie":
                        prompt = build_prompt(event["selfie_spec"], segment, settings.character or {})
                        suffix = txn["execution_id"][-8:]
                        output = Path(runtime["selfie_output_root"]) / f"{args.date}-{args.event_id}-{suffix}"
                        res = image_provider.generate(prompt, output)
                        image = {"path": res.path, "sha256": res.sha256, "size_bytes": res.size_bytes}
                        txn["prepared"].update(image_path=res.path, image_sha256=res.sha256)
                        txn = save_transaction(txn_path, txn_schema, txn)
                    import os
                    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
                    message, message_usage = generate_message(settings, api_key, event, segment, snapshot, llm_conf)
                except (StepError, ValueError, KeyError, OSError) as exc:
                    txn = _fail_before_send(runtime, daily_path, txn_path, txn, args.event_id, "content_generation_failed", exc)
                    raise

                latest_daily, latest_event, latest_state = _read_daily(runtime, daily_path, args.event_id)
                latest_scope = _session_history(settings, args.history_file)
                if scope.session_id is not None and latest_scope.session_id != scope.session_id:
                    exc = StepError("active session changed during content generation")
                    txn = _fail_before_send(runtime, daily_path, txn_path, txn, args.event_id, "session_changed", exc)
                    raise exc
                latest_snapshot = build_snapshot(settings, latest_daily, latest_event, latest_scope.messages)
                validate(latest_snapshot, Path(runtime["context_schema_path"]))
                gate2 = evaluate(latest_event, latest_state, latest_snapshot)
                if not gate2["allowed"]:
                    phase = "skipped" if gate2["code"] in {"late_execution", "too_early", "silent_event"} else "cancelled"
                    txn = transition(txn_path, txn_schema, txn, phase, f"Gate 2: {gate2['reason']}", decision="cancel", decision_reason=gate2["code"])
                    _daily_state(runtime, daily_path, args.event_id, phase, gate2["reason"], decision="cancel", decision_reason=gate2["code"], completed_at=now_iso(), at_job_id=None)
                    print(json.dumps({"status": f"{phase}_by_gate2", "gate": gate2}))
                    return 0
                if scope.session_id is not None and session_id(settings) != scope.session_id:
                    exc = StepError("active session changed immediately before Telegram send")
                    txn = _fail_before_send(runtime, daily_path, txn_path, txn, args.event_id, "session_changed", exc)
                    raise exc

                text = message["text"]
                txn["prepared"].update(text=text, text_sha256=sha256_bytes(text.encode("utf-8")), image_path=image["path"] if image else None, image_sha256=image["sha256"] if image else None)
                txn = transition(txn_path, txn_schema, txn, "prepared", "content validated and persisted")
                _daily_state(runtime, daily_path, args.event_id, "running", "content prepared", decision="execute", decision_reason="planned_event", final_text=text, output_image=image["path"] if image else None)
                txn = transition(txn_path, txn_schema, txn, "telegram_sending", "Telegram send intent persisted")
                try:
                    delivered = delivery.send(text, Path(image["path"]) if image else None)
                except DeliveryError as exc:
                    phase = "delivery_unknown" if exc.ambiguous else "failed"
                    txn = transition(txn_path, txn_schema, txn, phase, str(exc), error={"code": phase, "message": str(exc), "retryable": False})
                    _daily_state(runtime, daily_path, args.event_id, "failed", "delivery not confirmed", error={"code": phase, "message": "delivery requires manual reconciliation" if exc.ambiguous else str(exc), "retryable": False, "attempts": txn["attempt_count"]}, completed_at=now_iso(), at_job_id=None)
                    raise

                message_id = delivered["message_id"]
                name = instance_marker(settings)
                marker = f"[{name}_event event_id={args.event_id} execution_id={txn['execution_id']} telegram_message_id={message_id} media={'yes' if image else 'none'}]"
                transcript = text + "\n\n" + marker
                txn["telegram"].update(sent=True, message_id=message_id, sent_at=now_iso())
                txn["injection"]["marker"] = marker
                txn["prepared"]["transcript"] = transcript
                txn = transition(txn_path, txn_schema, txn, "telegram_sent", "delivery success JSON persisted")
                _daily_state(runtime, daily_path, args.event_id, "running", "delivery sent; injection pending", telegram_sent=True, error=None)
                try:
                    txn = _complete_injection(settings, runtime, txn_path, txn, txn.get("session_id"), args.history_file)
                except StepError as exc:
                    if txn["phase"] == "injecting":
                        txn = transition(txn_path, txn_schema, txn, "injection_pending", "chat.inject failed after delivery success", error={"code": "gateway_injection_failed", "message": str(exc), "retryable": True})
                    _daily_state(runtime, daily_path, args.event_id, "running", "delivery sent; injection pending", error={"code": "gateway_injection_failed", "message": "chat.inject failed; retry injection only", "retryable": True, "attempts": txn["attempt_count"]})
                    print(json.dumps({"status": "injection_pending", "delivery_sent": True, "session_injected": False}))
                    return 3

                _journal(runtime, args.date, txn, "injected", "completed", "outbound transaction complete", provider="delivery", provider_message_id=message_id, model=(llm_conf.get("model") or ""), usage={"message": message_usage})
                txn = transition(txn_path, txn_schema, txn, "completed", "delivery and session injection completed")
                _daily_state(runtime, daily_path, args.event_id, "completed", "delivery and session injection completed", session_injected=True, completed_at=now_iso(), at_job_id=None, error=None)
                print(json.dumps({"status": "completed", "event_id": args.event_id, "delivery_sent": True, "session_injected": True}))
                return 0
    except (StepError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
