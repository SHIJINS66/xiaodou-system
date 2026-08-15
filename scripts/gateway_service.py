#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from common import StepError, openclaw_env


def service_env() -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/sh",
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "Asia/Shanghai",
        "HOME": str(Path.home()),
    }
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    runtime = Path(f"/run/user/{uid}")
    if runtime.is_dir():
        env["XDG_RUNTIME_DIR"] = str(runtime)
        bus = runtime / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    for key in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "OPENCLAW_PROFILE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=service_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepError(f"command failed: {Path(command[0]).name}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise StepError(
            f"command returned nonzero: {Path(command[0]).name}: {detail or 'no detail'}"
        )
    return result


def _units(user: bool) -> list[str]:
    command = (
        ["systemctl", "--user", "list-units", "--all", "openclaw-gateway*.service", "--no-legend", "--no-pager"]
        if user
        else ["systemctl", "list-units", "--all", "openclaw-gateway*.service", "--no-legend", "--no-pager"]
    )
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
            env=service_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    units: list[str] = []
    for raw in result.stdout.splitlines():
        fields = raw.split()
        if fields and fields[0].startswith("openclaw-gateway") and fields[0].endswith(".service"):
            units.append(fields[0])
    return sorted(set(units))


def restart(openclaw_bin: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for unit in _units(False):
        result = subprocess.run(
            ["systemctl", "restart", unit],
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
            env=service_env(),
        )
        attempts.append({"method": "systemd_system", "unit": unit, "exit_code": result.returncode})
        if result.returncode == 0:
            return {"method": "systemd_system", "unit": unit, "attempts": attempts}
    for unit in _units(True):
        result = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
            env=service_env(),
        )
        attempts.append({"method": "systemd_user", "unit": unit, "exit_code": result.returncode})
        if result.returncode == 0:
            return {"method": "systemd_user", "unit": unit, "attempts": attempts}
    result = subprocess.run(
        [openclaw_bin, "gateway", "restart"],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
        env=service_env(),
    )
    attempts.append({"method": "openclaw_cli", "unit": None, "exit_code": result.returncode})
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise StepError(f"all Gateway restart methods failed: {detail or 'no detail'}")
    return {"method": "openclaw_cli", "unit": None, "attempts": attempts}


def rpc_healthy(openclaw_bin: str) -> bool:
    commands = [
        [openclaw_bin, "gateway", "status", "--require-rpc", "--json"],
        [openclaw_bin, "health", "--json", "--timeout", "15000"],
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=40,
                check=False,
                env=service_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
    return True


def wait_rpc(openclaw_bin: str, *, attempts: int = 24, delay_seconds: int = 5) -> None:
    for _ in range(attempts):
        if rpc_healthy(openclaw_bin):
            return
        time.sleep(delay_seconds)
    raise StepError("Gateway RPC health did not recover")
