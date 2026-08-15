#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

from common import StepError, atomic_write_json, sha256_file


def _openclaw_config_path(config):
    """OpenClaw 配置文件路径：优先 config.openclaw_config_path，缺省 ~/.openclaw/openclaw.json。"""
    from pathlib import Path
    explicit = config.get("openclaw_config_path")
    if explicit:
        return Path(explicit)
    return Path.home() / ".openclaw" / "openclaw.json"

from gateway_service import service_env

ALLOWED_PATCH_PATHS = {
    "hooks.internal.entries.session-memory.enabled",
    "agents.defaults.compaction.memoryFlush.enabled",
}


def _nested(value: dict[str, Any], *keys: str) -> tuple[bool, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _ensure_object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise StepError(f"OpenClaw config path is not object: {key}")
    return value


def _set_nested(root: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    current = root
    for key in keys[:-1]:
        current = _ensure_object(current, key)
    current[keys[-1]] = value


def _leaf_diff(before: Any, after: Any, prefix: str = "") -> set[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        result: set[str] = set()
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                if isinstance((after if key in after else before).get(key), dict):
                    value = (after if key in after else before)[key]
                    result.update(_leaf_diff({} if key in after else value, value if key in after else {}, child))
                else:
                    result.add(child)
            else:
                result.update(_leaf_diff(before[key], after[key], child))
        return result
    if before != after:
        return {prefix}
    return set()


def _load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StepError("cannot read OpenClaw config") from exc
    if not isinstance(value, dict):
        raise StepError("OpenClaw config root is not object")
    return value


def facts(config: dict[str, Any]) -> dict[str, Any]:
    path = _openclaw_config_path(config)
    value = _load_config(path)
    session_memory_present, session_memory = _nested(
        value, "hooks", "internal", "entries", "session-memory", "enabled"
    )
    memory_flush_present, memory_flush = _nested(
        value, "agents", "defaults", "compaction", "memoryFlush", "enabled"
    )
    reset_mode_present, reset_mode = _nested(value, "session", "reset", "mode")
    reset_hour_present, reset_hour = _nested(value, "session", "reset", "atHour")
    reset_idle_present, reset_idle = _nested(value, "session", "reset", "idleMinutes")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "session_memory_present": session_memory_present,
        "session_memory_enabled": session_memory,
        "memory_flush_present": memory_flush_present,
        "memory_flush_enabled": memory_flush,
        "native_reset_mode_present": reset_mode_present,
        "native_reset_mode": reset_mode,
        "native_reset_hour_present": reset_hour_present,
        "native_reset_hour": reset_hour,
        "native_reset_idle_present": reset_idle_present,
        "native_reset_idle_minutes": reset_idle,
    }


def patch_memory_writers(config: dict[str, Any]) -> dict[str, Any]:
    path = _openclaw_config_path(config)
    before = _load_config(path)
    before_facts = facts(config)
    if any(
        before_facts[key]
        for key in (
            "native_reset_mode_present",
            "native_reset_hour_present",
            "native_reset_idle_present",
        )
    ):
        raise StepError("OpenClaw native session.reset must remain unconfigured")

    after = copy.deepcopy(before)
    _set_nested(after, ("hooks", "internal", "entries", "session-memory", "enabled"), False)
    _set_nested(after, ("agents", "defaults", "compaction", "memoryFlush", "enabled"), False)
    changed_paths = _leaf_diff(before, after)
    unexpected = changed_paths - ALLOWED_PATCH_PATHS
    if unexpected:
        raise StepError(f"OpenClaw config patch escaped whitelist: {sorted(unexpected)}")

    changed = bool(changed_paths)
    if changed:
        atomic_write_json(path, after, mode=path.stat().st_mode & 0o777)
    after_facts = facts(config)
    if after_facts["session_memory_enabled"] is not False:
        raise StepError("session-memory config patch did not persist")
    if after_facts["memory_flush_enabled"] is not False:
        raise StepError("memoryFlush config patch did not persist")
    return {
        "changed": changed,
        "changed_paths": sorted(changed_paths),
        "before_sha256": before_facts["sha256"],
        "after_sha256": after_facts["sha256"],
        "facts": after_facts,
    }


def inspect_hook_runtime(config: dict[str, Any]) -> dict[str, Any]:
    command = [config["openclaw_bin"], "hooks", "info", "session-memory", "--json"]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=service_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepError("cannot inspect session-memory hook") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise StepError(f"session-memory hook inspection failed: {detail or 'no detail'}")
    text = result.stdout.lstrip()
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise StepError("session-memory hook returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise StepError("session-memory hook result is not object")
    return {
        "disabled": parsed.get("disabled"),
        "enabledByConfig": parsed.get("enabledByConfig"),
        "eligible": parsed.get("eligible"),
        "loadable": parsed.get("loadable"),
        "runtime_disabled": (
            parsed.get("disabled") is True
            or parsed.get("enabledByConfig") is False
        ),
    }


def verify(config: dict[str, Any], check_hook_runtime: bool = False) -> dict[str, Any]:
    current = facts(config)
    errors: list[str] = []
    if current["session_memory_enabled"] is not False:
        errors.append("session-memory must be explicitly disabled")
    if current["memory_flush_enabled"] is not False:
        errors.append("compaction memoryFlush must be explicitly disabled")
    if current["native_reset_mode_present"] or current["native_reset_hour_present"] or current["native_reset_idle_present"]:
        errors.append("OpenClaw native session.reset must remain unconfigured")

    hook_runtime: dict[str, Any] | None = None
    if check_hook_runtime:
        hook_runtime = inspect_hook_runtime(config)
        if hook_runtime["runtime_disabled"] is not True:
            errors.append("session-memory hook runtime is still enabled")

    return {
        "passed": not errors,
        "facts": current,
        "hook_runtime": hook_runtime,
        "errors": errors,
    }
