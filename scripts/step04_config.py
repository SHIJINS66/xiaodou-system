#!/usr/bin/env python3
"""step04_config — 从 framework settings 构造 step04 编排所需要的 config 字典。

线上 step04 各 CLI（finalize_day / finalize_yesterday / update_memory_md /
session_rollover / rollover_artifacts / raw_backup / backup_openclaw /
mirror_openclaw_memory / normalize_chatlog / providers.gateway_*）原本读
`/etc/xiaodou/step04.json` 拿全部路径与参数。

本模块把**该 config 的字段集合**从 framework 的 settings 派生出来，
去掉了所有 /opt/xiaodou、/etc/xiaodou、/root/.openclaw、/var/lib 硬编码。
迁移后的 CLI 用它替代 `--config /etc/xiaodou/step04.json`：
    python scripts/finalize_day.py --date 2026-08-15 --settings ./instance/settings.yaml

step04 各脚本从 config dict 取值；这里保证字段名与线上 step04.json 完全一致，
从而迁移脚本只改「config 来源」，不必改每个取字段的代码。
"""
from __future__ import annotations

import os

from pathlib import Path
from typing import Any

from common import StepError, package_root, prompt_path, schema_path


def _dir(settings: Any, key: str) -> Path:
    dirs = (settings.get("runtime") or {}).get("dirs") or {}
    rel = dirs.get(key)
    if rel is None:
        raise StepError(f"runtime.dirs.{key} 未配置（step04 需要）")
    rel = os.path.expanduser(rel)  # 支持 ~ / 绝对路径
    if os.path.isabs(rel):
        return Path(rel).resolve()
    root = Path(os.path.expanduser(settings.get("runtime", {}).get("root_dir") or "."))
    return (root / rel).resolve()


def step04_config(settings: Any) -> dict[str, Any]:
    """把 framework settings 展开为线上 step04.json 同构的 config 字典。"""
    root = Path(os.path.expanduser(settings.get("runtime", {}).get("root_dir") or ".")).resolve()
    gw = (settings.get("scheduling") or {}).get("gateway") or {}
    env_file = settings.get("runtime", {}).get("env_file") or ".env"
    ds = (settings.get("models") or {}).get("deepseek") or {}

    def dirpath(key: str) -> str:
        return str(_dir(settings, key))

    def default(v: Any, fallback: Any) -> Any:
        return v if v is not None else fallback

    config: dict[str, Any] = {
        # 运行期注入的 settings 对象（供 make_at_env/openclaw_env 等子进程 env 用，不序列化）
        "_settings": settings,
        "schema_version": "1.0",
        # ---- 路径（来源：settings.runtime）----
        "workspace": str(root),
        "daily_json_root": dirpath("daily"),
        "journal_root": dirpath("journal"),
        "transaction_root": dirpath("transactions"),
        "state_root": dirpath("step04_state"),
        "backup_root": dirpath("backups"),
        "carryover_root": str(root / "state" / "carryover"),
        "rollover_root": str(root / "state" / "rollover"),
        "raw_backup_root": str(root / "state" / "raw-backup"),
        "env_file": str(root / env_file),
        # ---- schema / prompt（来源：随包定位）----
        "daily_schema_path": str(schema_path(package_root(__file__), "daily_file_v1_1")),
        "config_schema_path": str(schema_path(package_root(__file__), "step04_config_v1")),
        "memory_schema_path": str(schema_path(package_root(__file__), "memory_model_output_v1")),
        "memory_prompt_path": str(prompt_path(package_root(__file__), "daily_memory_v1")),
        # ---- OpenClaw gateway 会话（来源：settings.scheduling.gateway）----
        "session_key": gw.get("session_key") or "",
        "openclaw_bin": gw.get("openclaw_bin") or "/usr/bin/openclaw",
        "openclaw_home": str(Path.home()),
        "openclaw_timeout_seconds": int(gw.get("openclaw_timeout_seconds") or 60),
        "openclaw_config_path": "",
        "ws_url": gw.get("ws_url") or "",
        "auth_token_env": gw.get("auth_token_env") or "",
        # ---- DeepSeek 对话模型（来源：settings.models.deepseek）----
        "deepseek_base_url": ds.get("base_url") or "https://api.deepseek.com/v1",
        "deepseek_model": ds.get("model") or "deepseek-v4-flash",
        "deepseek_thinking_type": ds.get("thinking_type") or "enabled",
        "deepseek_reasoning_effort": ds.get("reasoning_effort") or "high",
        # ---- 会话历史采集（对齐线上默认值）----
        "page_size": 100,
        "max_pages": 100,
        "max_messages": 10000,
        "max_total_bytes": 52428800,
        # ---- 等待 / 重试 / 模型参数（对齐线上）----
        "running_wait_seconds": 600,
        "model_repair_attempts": int(settings.get("models", {}).get("memory", {}).get("generation_attempts") or 3),
        "request_timeout_seconds": int(ds.get("timeout") or 240),
        "temperature": float(ds.get("temperature") or 0.2),
        "max_tokens": int(ds.get("max_tokens") or 5000),
        # ---- rollover（来源：settings.step04，缺省对齐线上）----
        "rollover_hour": int(settings.get("step04", {}).get("rollover_hour") if settings.get("step04", {}).get("rollover_hour") is not None else 4),
        "rollover_window_minutes": int(settings.get("step04", {}).get("rollover_window_minutes") or 30),
        "rollover_wait_seconds": int(settings.get("step04", {}).get("rollover_wait_seconds") or 60),
        "rollover_poll_seconds": int(settings.get("step04", {}).get("rollover_poll_seconds") or 5),
        "rollover_quiescence_seconds": int(settings.get("step04", {}).get("rollover_quiescence_seconds") or 3),
        "session_lifecycle_lock_file": str(root / "state" / "locks" / "step04-session-lifecycle.lock"),
        "finalize_internal_lock_file": str(root / "state" / "locks" / "step04-finalize-internal.lock"),
        "rollover_lock_file": str(root / "state" / "locks" / "step04-rollover.lock"),
        # ---- memory 生成增强参数（对齐线上）----
        "memory_generation_attempts": int(settings.get("models", {}).get("memory", {}).get("generation_attempts") or 3),
        "memory_normal_max_tokens": int(settings.get("models", {}).get("memory", {}).get("normal_max_tokens") or 16384),
        "memory_repair_max_tokens": int(settings.get("models", {}).get("memory", {}).get("repair_max_tokens") or 16384),
        "memory_conflict_max_tokens": int(settings.get("models", {}).get("memory", {}).get("conflict_max_tokens") or 32768),
        "memory_recovery_max_tokens": int(settings.get("models", {}).get("memory", {}).get("recovery_max_tokens") or 32768),
    }
    return config


def load_step04_config(settings_arg: str | None = None) -> dict[str, Any]:
    """标准入口：按 settings 加载并返回 step04 config 字典。"""
    import settings_loader as sl

    settings = sl.load_settings(settings_arg)
    return step04_config(settings)


if __name__ == "__main__":
    import json
    import sys

    conf = load_step04_config(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(conf, ensure_ascii=False, indent=2))
