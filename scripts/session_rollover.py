#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date as Date, datetime, time as Time, timedelta
from pathlib import Path
from typing import Any

from common import (
    StepError,
    atomic_write_json,
    file_lock,
    load_json,
    make_tz,
    now_iso,
    sha256_file,
)
TZ = make_tz("Asia/Shanghai")
from openclaw_memory_config import verify as verify_memory_config
from providers.gateway_history import collect_between, message_id, message_text, timestamp
from providers.gateway_sessions import get_session, reset_session, row_session_id, session_is_active
from raw_backup import verify as verify_raw_backup
from rollover_artifacts import carryover_path, rollover_receipt_path, rollover_state_path
from schema_tools import validate


def _schema(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / name


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_messages(messages: list[dict[str, Any]]) -> bytes:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _message_projection(row: dict[str, Any]) -> dict[str, Any] | None:
    role = row.get("role")
    if role not in {"user", "assistant"}:
        return None
    stamp = timestamp(row)
    if stamp is None:
        return None
    metadata = row.get("__openclaw") if isinstance(row.get("__openclaw"), dict) else {}
    identity = message_id(row)
    sequence = metadata.get("seq")
    kind = metadata.get("kind")
    return {
        "id": identity,
        "timestamp": stamp.isoformat(timespec="seconds"),
        "role": role,
        "content": message_text(row),
        "openclaw_seq": sequence if isinstance(sequence, int) else None,
        "kind": kind if isinstance(kind, str) else None,
    }


def _raw_from_projection(row: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if row.get("id") is not None:
        metadata["id"] = row["id"]
    if row.get("openclaw_seq") is not None:
        metadata["seq"] = row["openclaw_seq"]
    if row.get("kind") is not None:
        metadata["kind"] = row["kind"]
    return {
        "role": row["role"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "__openclaw": metadata,
    }


def _state_write(path: Path, state: dict[str, Any], phase: str, **updates: Any) -> dict[str, Any]:
    state.update(phase=phase, updated_at=now_iso(), **updates)
    state.setdefault("history", []).append({"at": state["updated_at"], "phase": phase})
    validate(state, _schema("session_rollover_state_v1.schema.json"))
    atomic_write_json(path, state)
    return state


ROLLOVER_SAFE_INCOMPLETE_PHASES = {
    "raw_backup_ready": "pending",
    "memory_pending": "pending",
    "memory_ready": "generated_unpublished",
    "mirrored": "published",
    "backup_intent": "published",
    "backup_failed": "published",
    "backup_unknown": "published",
}


def _finalization_ready(config: dict[str, Any], prior_date: str) -> dict[str, Any]:
    path = Path(config["state_root"]) / "state" / f"{prior_date}.json"
    if not path.is_file():
        raise StepError("previous-day finalization state is missing")
    state = load_json(path)
    validate(state, _schema("finalization_record_v1.schema.json"))
    phase = state.get("phase")
    if phase == "completed":
        backup = state.get("backup")
        if not isinstance(backup, dict) or backup.get("verified") is not True:
            raise StepError("previous-day final backup is not verified")
        archive = Path(str(backup.get("archive", "")))
        if not archive.is_file() or sha256_file(archive) != backup.get("sha256"):
            raise StepError("previous-day final backup archive drift")
        if isinstance(state.get("raw_backup"), dict):
            verify_raw_backup(state["raw_backup"])
        state["rollover_memory_status"] = "completed"
        return state
    if phase in ROLLOVER_SAFE_INCOMPLETE_PHASES:
        verify_raw_backup(state.get("raw_backup", {}))
        state["rollover_memory_status"] = ROLLOVER_SAFE_INCOMPLETE_PHASES[phase]
        return state
    raise StepError("previous-day raw evidence is not safely backed up")


def _rollover_window(config: dict[str, Any], now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.date(), Time(hour=int(config.get("rollover_hour", 4))), TZ)
    end = start + timedelta(minutes=int(config.get("rollover_window_minutes", 30)))
    return start, end


def _wait_quiescent(config: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + int(config.get("rollover_wait_seconds", 600))
    poll = max(1, int(config.get("rollover_poll_seconds", 5)))
    quiet_required = max(0, int(config.get("rollover_quiescence_seconds", 5)))
    stable_since: float | None = None
    stable_signature: tuple[str, Any, Any] | None = None

    while True:
        row = get_session(config)
        signature = (row_session_id(row), row.get("updatedAt"), row.get("status"))
        if not session_is_active(row):
            if signature == stable_signature:
                stable_since = stable_since or time.monotonic()
            else:
                stable_signature = signature
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= quiet_required:
                return row
        else:
            stable_since = None
            stable_signature = None
        if time.monotonic() >= deadline:
            raise StepError("active session did not become quiescent")
        time.sleep(min(poll, max(0.1, deadline - time.monotonic())))


def _verify_completed(state: dict[str, Any]) -> dict[str, Any]:
    carry = Path(state.get("carryover", {}).get("path", ""))
    receipt = Path(state.get("receipt", {}).get("path", ""))
    if not carry.is_file() or sha256_file(carry) != state.get("carryover", {}).get("sha256"):
        raise StepError("completed rollover carryover drift")
    if not receipt.is_file() or sha256_file(receipt) != state.get("receipt", {}).get("sha256"):
        raise StepError("completed rollover receipt drift")
    return {
        "mode": "apply",
        "status": "verified_noop",
        "date": state["date"],
        "state": state.get("state_path"),
        "carryover": state["carryover"],
        "receipt": state["receipt"],
    }



def contract_check(config: dict[str, Any], target: Date) -> dict[str, Any]:
    """Read-only rollover contract verification.

    This function intentionally acquires no filesystem locks and performs no
    writes or sessions.reset call. It is safe to run while the deployer already
    owns the shared lifecycle lock.
    """
    date_value = target.isoformat()
    previous_date = (target - timedelta(days=1)).isoformat()
    try:
        finalization = _finalization_ready(config, previous_date)
    except StepError as exc:
        return {
            "mode": "contract_check",
            "status": "blocked_finalization",
            "date": date_value,
            "reason": str(exc),
            "lock_acquisition_attempted": False,
            "writes_performed": False,
            "sessions_reset_called": False,
        }

    memory = verify_memory_config(config, check_hook_runtime=False)
    if not memory["passed"]:
        raise StepError("OpenClaw memory writers are not compliant")
    row = get_session(config)
    return {
        "mode": "contract_check",
        "status": "ready",
        "date": date_value,
        "previous_finalization_completed": finalization.get("phase") == "completed",
        "previous_backup_verified": finalization.get("backup", {}).get("verified") is True,
        "previous_raw_backup_verified": finalization.get("raw_backup", {}).get("verified") is True,
        "previous_memory_status": finalization.get("rollover_memory_status", "unknown"),
        "session_id_present": bool(row_session_id(row)),
        "has_active_run": row.get("hasActiveRun") is True,
        "session_status": row.get("status"),
        "planned_carryover": str(carryover_path(config, date_value)),
        "planned_receipt": str(rollover_receipt_path(config, date_value)),
        "lock_acquisition_attempted": False,
        "writes_performed": False,
        "sessions_reset_called": False,
    }

def run(args: argparse.Namespace) -> dict[str, Any]:
    from step04_config import load_step04_config
    config = load_step04_config(args.settings) if getattr(args, "settings", None) else load_json(Path(args.config))
    _schema_view = {k: v for k, v in config.items() if k != '_settings'}  # 运行期注入对象不参与 schema 校验
    validate(_schema_view, Path(config["config_schema_path"]))
    enabled = Path(config.get("rollover_enabled_gate") or (Path(config["state_root"]).parent / "step04.enabled"))
    now = datetime.now(TZ)
    target = Date.fromisoformat(args.date) if args.date else now.date()
    if getattr(args, "contract_check", False):
        if args.apply or args.ack:
            raise StepError("contract check cannot be combined with apply or ack")
        return contract_check(config, target)
    if args.apply:
        if args.ack != "SESSION_ROLLOVER" or not enabled.is_file():
            raise StepError("apply requires enabled gate and --ack SESSION_ROLLOVER")
        window_start, window_end = _rollover_window(config, now)
        if target != now.date() or not (window_start <= now < window_end):
            raise StepError("session rollover apply is outside the configured window")

    date_value = target.isoformat()
    previous_date = (target - timedelta(days=1)).isoformat()
    state_path = rollover_state_path(config, date_value)
    prior = load_json(state_path) if state_path.is_file() else {}
    if prior.get("phase") == "completed":
        return _verify_completed(prior)

    rollover_lock = Path(config.get("rollover_lock_file", "step04-rollover.lock"))
    finalize_lock = Path(config.get("finalize_internal_lock_file", "step04-finalize-internal.lock"))
    lifecycle_lock = Path(config.get("session_lifecycle_lock_file", "step04-session-lifecycle.lock"))

    with file_lock(rollover_lock, blocking=False):
        with file_lock(finalize_lock, blocking=False):
            with file_lock(lifecycle_lock, blocking=True):
                try:
                    finalization = _finalization_ready(config, previous_date)
                except StepError as exc:
                    if args.apply:
                        state = prior or {
                            "schema_version": "1.0",
                            "date": date_value,
                            "phase": "new",
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                            "history": [],
                            "state_path": str(state_path),
                        }
                        state_path.parent.mkdir(parents=True, exist_ok=True)
                        state_path.parent.chmod(0o700)
                        _state_write(state_path, state, "blocked_finalization", error=str(exc))
                    return {
                        "mode": "apply" if args.apply else "dry_run",
                        "status": "blocked_finalization",
                        "date": date_value,
                        "reason": str(exc),
                        "external_calls": [],
                    }

                memory = verify_memory_config(config, check_hook_runtime=False)
                if not memory["passed"]:
                    raise StepError("OpenClaw memory writers are not compliant")

                if not args.apply:
                    row = get_session(config)
                    return {
                        "mode": "dry_run",
                        "status": "ready",
                        "date": date_value,
                        "previous_finalization_completed": finalization.get("phase") == "completed",
                        "previous_backup_verified": finalization.get("backup", {}).get("verified") is True,
                        "previous_raw_backup_verified": finalization.get("raw_backup", {}).get("verified") is True,
                        "previous_memory_status": finalization.get("rollover_memory_status", "unknown"),
                        "session_id_present": bool(row_session_id(row)),
                        "has_active_run": row.get("hasActiveRun") is True,
                        "session_status": row.get("status"),
                        "planned_carryover": str(carryover_path(config, date_value)),
                        "planned_receipt": str(rollover_receipt_path(config, date_value)),
                        "sessions_reset_called": False,
                    }

                state = prior or {
                    "schema_version": "1.0",
                    "date": date_value,
                    "phase": "new",
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "history": [],
                    "state_path": str(state_path),
                }
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.parent.chmod(0o700)

                phase = state.get("phase")
                if phase in {"reset_unknown", "verification_failed"}:
                    raise StepError(f"rollover state {phase} requires manual review")

                if phase == "reset_succeeded":
                    response_session_id = state.get("new_session_id")
                    if not isinstance(response_session_id, str):
                        raise StepError("reset_succeeded state is missing new sessionId")
                elif phase == "reset_intent":
                    current = get_session(config)
                    current_id = row_session_id(current)
                    old_id = state.get("old_session_id")
                    if isinstance(old_id, str) and current_id != old_id:
                        response_session_id = current_id
                        _state_write(
                            state_path,
                            state,
                            "reset_succeeded",
                            new_session_id=response_session_id,
                            reset_response_sha256=None,
                        )
                    else:
                        _state_write(state_path, state, "reset_unknown", error="prior reset intent did not rotate session")
                        raise StepError("prior reset intent is unresolved; refusing a second reset")
                else:
                    try:
                        stable_row = _wait_quiescent(config)
                    except StepError as exc:
                        _state_write(state_path, state, "blocked_active_run", error=str(exc))
                        return {
                            "mode": "apply",
                            "status": "blocked_active_run",
                            "date": date_value,
                            "reason": str(exc),
                        }
                    old_session_id = row_session_id(stable_row)

                    if phase == "carryover_ready":
                        state_old = state.get("old_session_id")
                        carry = state.get("carryover", {})
                        carry_path_existing = Path(carry.get("path", ""))
                        if state_old != old_session_id:
                            raise StepError("session changed after carryover was prepared")
                        if not carry_path_existing.is_file() or sha256_file(carry_path_existing) != carry.get("sha256"):
                            raise StepError("prepared carryover drift")
                    else:
                        capture_end = datetime.now(TZ)
                        capture_start = datetime.combine(target, Time.min, TZ)
                        history = collect_between(config, capture_start, capture_end)
                        if history["session_id"] != old_session_id:
                            raise StepError("session changed before carryover capture completed")
                        projected = [item for row in history["messages"] if (item := _message_projection(row)) is not None]
                        canonical = _canonical_messages(projected)
                        carryover = {
                            "schema_version": "1.0",
                            "date": date_value,
                            "timezone": "Asia/Shanghai",
                            "window_start": capture_start.isoformat(timespec="seconds"),
                            "window_end": capture_end.isoformat(timespec="seconds"),
                            "session_key_sha256": _hash(config["session_key"]),
                            "source_session_id_sha256": _hash(old_session_id),
                            "captured_at": now_iso(),
                            "messages": projected,
                            "message_count": len(projected),
                            "content_sha256": hashlib.sha256(canonical).hexdigest(),
                            "status": "captured",
                        }
                        validate(carryover, _schema("session_carryover_v1.schema.json"))
                        carry_path_value = carryover_path(config, date_value)
                        carry_path_value.parent.mkdir(parents=True, exist_ok=True)
                        carry_path_value.parent.chmod(0o700)
                        if carry_path_value.exists():
                            existing = load_json(carry_path_value)
                            if existing != carryover:
                                raise StepError("refusing to overwrite existing carryover")
                        else:
                            atomic_write_json(carry_path_value, carryover)
                        carry_hash = sha256_file(carry_path_value)
                        _state_write(
                            state_path,
                            state,
                            "carryover_ready",
                            old_session_id=old_session_id,
                            carryover={"path": str(carry_path_value), "sha256": carry_hash, "message_count": len(projected)},
                        )

                    latest = get_session(config)
                    if row_session_id(latest) != old_session_id or session_is_active(latest):
                        _state_write(state_path, state, "blocked_active_run", error="session changed or became active after carryover")
                        return {
                            "mode": "apply",
                            "status": "blocked_active_run",
                            "date": date_value,
                            "reason": "session changed or became active after carryover",
                        }
                    _state_write(state_path, state, "reset_intent", reset_requested_at=now_iso())
                    try:
                        reset = reset_session(config)
                        response_session_id = reset["session_id"]
                        response_hash = hashlib.sha256(
                            json.dumps(reset["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest()
                    except StepError as exc:
                        current = get_session(config)
                        current_id = row_session_id(current)
                        if current_id == old_session_id:
                            _state_write(state_path, state, "reset_unknown", error=type(exc).__name__)
                            raise
                        response_session_id = current_id
                        response_hash = None
                    _state_write(
                        state_path,
                        state,
                        "reset_succeeded",
                        new_session_id=response_session_id,
                        reset_response_sha256=response_hash,
                    )

                old_session_id = state.get("old_session_id")
                if not isinstance(old_session_id, str):
                    raise StepError("rollover state is missing old sessionId")
                current = get_session(config)
                current_id = row_session_id(current)
                if current_id == old_session_id:
                    _state_write(state_path, state, "verification_failed", error="sessionId did not change")
                    raise StepError("sessions.reset did not rotate sessionId")
                if response_session_id != current_id:
                    _state_write(state_path, state, "verification_failed", error="response/list sessionId mismatch")
                    raise StepError("sessions.reset response does not match sessions.list")
                carry = state.get("carryover", {})
                carry_path = Path(carry.get("path", ""))
                if not carry_path.is_file() or sha256_file(carry_path) != carry.get("sha256"):
                    raise StepError("carryover drift before receipt")
                receipt = {
                    "schema_version": "1.0",
                    "date": date_value,
                    "status": "completed",
                    "session_key_sha256": _hash(config["session_key"]),
                    "old_session_id_sha256": _hash(old_session_id),
                    "new_session_id_sha256": _hash(current_id),
                    "reset_requested_at": state.get("reset_requested_at", now_iso()),
                    "verified_at": now_iso(),
                    "carryover_path": str(carry_path),
                    "carryover_sha256": sha256_file(carry_path),
                    "carryover_message_count": int(carry.get("message_count", 0)),
                    "previous_memory_status": finalization.get("rollover_memory_status", "unknown"),
                }
                validate(receipt, _schema("session_rollover_record_v1.schema.json"))
                receipt_path = rollover_receipt_path(config, date_value)
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.parent.chmod(0o700)
                if receipt_path.exists():
                    existing = load_json(receipt_path)
                    if existing != receipt:
                        raise StepError("refusing to overwrite existing rollover receipt")
                else:
                    atomic_write_json(receipt_path, receipt)
                _state_write(
                    state_path,
                    state,
                    "completed",
                    completed_at=now_iso(),
                    receipt={"path": str(receipt_path), "sha256": sha256_file(receipt_path)},
                    error=None,
                )
                return {
                    "mode": "apply",
                    "status": "completed",
                    "date": date_value,
                    "state": str(state_path),
                    "carryover": state["carryover"],
                    "receipt": state["receipt"],
                    "session_id_rotated": True,
                    "previous_memory_status": finalization.get("rollover_memory_status", "unknown"),
                }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--settings", help="framework settings.yaml（从它构造 config）")
    parser.add_argument("--config", help="旧式 step04.json（与 --settings 二选一）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ack")
    parser.add_argument("--contract-check", action="store_true")
    args = parser.parse_args()
    if not (args.settings or args.config):
        print("session_rollover 需要 --settings 或 --config", file=sys.stderr)
        return 2
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except (StepError, KeyError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
