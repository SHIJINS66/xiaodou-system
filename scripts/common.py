#!/usr/bin/env python3
"""common — 三个 step（plan/execute/memory）共用的确定性工具与配置入口。

替代线上重复定义在 step02/common.py、step03/common.py、step04/common.py 里的：
    - TZ / AT_ENV / FIXED_ENV（三处写死 Asia/Shanghai + /root）
    - CORE_FILES（三处文件清单还不一致：6 / 7 个）
    - now_iso / parse_iso / sha256 / load_json / load_env_file
    - atomic_write_*（三个版本几乎逐字相同）
    - file_lock / safe_under / bump_daily / all_events / find_event_and_state
    - stable_execution_id / verify_*（冻结人设校验，改为从 settings 派生 marker）

设计约束（打包原则）：
    - 不 import 任何 /opt/xiaodou 的代码。
    - 所有路径、时区、marker 前缀、文件清单都从 Settings 读，不写死。
    - 只依赖标准库 + PyYAML（settings_loader 已引）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows verification only
    fcntl = None


def package_root(module_file: str) -> Path:
    """从当前 .py 文件推导包根：bin/ 或 scripts/ 的父目录，或框架根。"""
    p = Path(module_file).resolve()
    parts = p.parts
    for marker in ("bin", "scripts"):
        if marker in parts:
            idx = parts.index(marker)
            return Path(*parts[:idx])
    return p.parent


# ---- 包根引导：让 scripts/ 下的脚本能 import providers/、schemas/ ----
# 把本模块所在包根（bin 或 scripts 的父目录）插入 sys.path。
_PKG_ROOT = package_root(__file__)
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from settings_loader import ConfigError, load_settings, Settings

# 模块级默认时区兜底（bootstrap/显式 tz 未提供时用）。真实时区统一由 settings 驱动。
_DEFAULT_TZ = ZoneInfo("Asia/Shanghai")

TERMINAL_PHASES = {"completed", "cancelled", "failed", "skipped", "delivery_unknown"}
# 事件执行状态机的终态（调度/执行共用）
TERMINAL = {"running", "completed", "cancelled", "failed", "skipped"}


def make_tz(name: str) -> ZoneInfo:
    """从 settings.runtime.timezone 建 ZoneInfo；缺省用上海时区兜底。"""
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def make_at_env(settings: Settings, home: str | None = None) -> dict[str, str]:
    """at 任务执行的固定 env 白名单（少暴露密钥，仅允许 settings.scheduling.at_env）。"""
    tz = settings.get("scheduling.timezone") or settings.get("runtime.timezone") or "Asia/Shanghai"
    allow = settings.get("scheduling.at_env") or ["PATH", "SHELL", "LC_ALL", "LANG", "TZ", "HOME"]
    env: dict[str, str] = {
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/sh",
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": tz,
    }
    if "HOME" in allow:
        env["HOME"] = home or str(settings.root_dir)
    # 只保留白名单里能提供的
    return env


def openclaw_env(settings: Settings) -> dict[str, str]:
    """OpenClaw CLI 调用的固定非密 env（不泄漏至 at 队列）。

    默认以 settings.root_dir 作为 HOME 推断 OpenClaw state；若 settings 里显式配置了
    OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR / OPENCLAW_PROFILE（`scheduling.gateway.extra_env`
    或同名 settings 字段），则透传给 OpenClaw CLI，使其能指向任意实例（不依赖 HOME）。
    """
    tz = settings.get("runtime.timezone") or "Asia/Shanghai"
    extra: dict[str, str] = {}
    extra_cfg = settings.get("scheduling.gateway.extra_env") or {}
    if isinstance(extra_cfg, dict):
        for key in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "OPENCLAW_PROFILE"):
            val = extra_cfg.get(key)
            if val:
                extra[key] = str(val)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SHELL": "/bin/sh",
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": tz,
        # 显式指向实例时用真实 home（避免 HOME=root_dir 干扰 OpenClaw 用户环境）；
        # 否则沿用 root_dir 让 OpenClaw 从 $HOME/.openclaw 推断（线上缺省行为）。
        "HOME": str(Path.home()) if extra else str(settings.root_dir),
    }
    env.update(extra)
    return env


class StepError(RuntimeError):
    """统一错误类型（替代 Step02/03/04Error 三个子类）。"""


def now_iso(tz: ZoneInfo | None = None) -> str:
    if tz is None:
        tz = _DEFAULT_TZ
    return datetime.now(tz).isoformat(timespec="seconds")


def parse_iso(value: str, tz: ZoneInfo | None = None) -> datetime:
    """解析 ISO 时间并转到目标时区；tz 缺省时用模块级默认（上海）。

    step02/03/04 三套调法不同（有的传 tz 有的不传），统一提供默认值便于等价迁移。
    """
    tz = tz or _DEFAULT_TZ
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StepError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise StepError(f"timestamp has no timezone: {value}")
    return parsed.astimezone(tz)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StepError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StepError(f"JSON root must be object: {path}")
    return value


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:  # Windows verification only
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = path if path.exists() else path.parent
            stat = owner.stat()
            os.chown(tmp, stat.st_uid, stat.st_gid)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, payload, mode)


def atomic_write_text(path: Path, value: str, mode: int = 0o600) -> None:
    _atomic_write(path, value.encode("utf-8"), mode)


def atomic_write_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    _atomic_write(path, value, mode)


def safe_under(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    base = root.resolve()
    if resolved != base and base not in resolved.parents:
        raise StepError(f"path escapes root: {resolved} not under {base}")
    return resolved


@contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
            except BlockingIOError as exc:
                raise StepError(f"lock busy: {path}") from exc
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def bump_daily(daily: dict[str, Any], tz: ZoneInfo | None = None) -> None:
    daily["file_revision"] += 1
    daily["updated_at"] = now_iso(tz)


def all_events(daily: dict[str, Any]) -> list[dict[str, Any]]:
    return list(daily.get("plan", {}).get("events", [])) + list(daily.get("runtime", {}).get("runtime_events", []))


def find_event_and_state(daily: dict[str, Any], event_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    events = {item["event_id"]: item for item in all_events(daily)}
    states = {item["event_id"]: item for item in daily.get("runtime", {}).get("event_states", [])}
    if event_id not in events or event_id not in states:
        raise StepError(f"event/state missing: {event_id}")
    return events[event_id], states[event_id]


# ---------------------------------------------------------------------------
# marker 前缀 —— 替代线上 xd02- / xd03- / [xiaodou_event...]
# marker 会写进 session 历史，用系统实例名（默认 companion）不用角色名，
# 避免把"这是 AI 角色"暴露给模型。
# ---------------------------------------------------------------------------

def instance_marker(settings: Settings) -> str:
    """短前缀：如 xd03- -> {instance_name}-。实例名做安全归一化。"""
    name = (settings.get("system.instance_name") or "companion").strip().lower()
    # 只保留字母数字和中划线，防止非法时区/命令注入
    clean = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    return clean or "companion"


def event_marker_id(settings: Settings, daily_path: Path, event_id: str) -> str:
    """稳定执行标记（对应线上 stable_execution_id / marker）。"""
    name = instance_marker(settings)
    digest = sha256_bytes(f"{daily_path.resolve()}:{event_id}".encode("utf-8"))[:24]
    return f"{name}-" + digest


def execution_id(settings: Settings, daily_path: Path, event_id: str) -> str:
    """事务 execution_id（对应线上 step03 stable_execution_id：xd03-<sha256 前 32 位>）。

    与 event_marker_id（at 队列 24 位）格式不同但同源；保持等价迁移：
    {instance}-<sha256(daily_path\nevent_id) 前 32 位>。
    """
    name = instance_marker(settings)
    material = f"{daily_path.resolve()}\n{event_id}".encode("utf-8")
    digest = sha256_bytes(material)[:32]
    return f"{name}-" + digest


def internal_marker_regex(settings: Settings) -> str:
    """context_snapshot 用于从 session 历史剥离内部事件 marker 的正则片段。"""
    name = instance_marker(settings)
    return rf"(?:\r?\n){{0,2}}\[{name}_event\s+[^\]]*\]"


def marker_env_var(settings: Settings) -> str:
    """at 任务里携带 marker 的环境变量名，替代线上写死的 XIAODOU_STEP0X_MARKER。

    由实例名大写化 + _MARKER 派生：companion -> COMPANION_MARKER。
    标记会写进 at 队列与 session 历史，用实例名（非角色名）避免暴露角色属性。
    """
    name = instance_marker(settings)
    upper = "".join(ch.upper() if ch.isalnum() else "_" for ch in name)
    return f"{upper}_MARKER"


# ---------------------------------------------------------------------------
# schema / prompt 定位 —— 替代硬编码 /opt/xiaodou/step0X/schemas
# 约定包内布局：<包根>/schemas/... 与 <包根>/prompts/...
# （package_root 定义在文件顶部引导区）
# ---------------------------------------------------------------------------

def schema_path(package_root_dir: Path, name: str) -> Path:
    """schema 文件：<包根>/schemas/<name>.schema.json，缺失报错。"""
    p = package_root_dir / "schemas" / f"{name}.schema.json"
    if not p.is_file():
        raise StepError(f"missing schema: {p}")
    return p


def prompt_path(package_root_dir: Path, name: str) -> Path:
    """prompt 文件：<包根>/prompts/<name>.md，缺失报错。"""
    p = package_root_dir / "prompts" / f"{name}.md"
    if not p.is_file():
        raise StepError(f"missing prompt: {p}")
    return p


def render_prompt(prompt_text: str, settings: Settings) -> str:
    """把 prompt 模板里的角色/陪伴对象占位符替换为 settings 实例值。

    支持的占位符（等价迁移时把写死的角色名换为占位）：
        {character_name}   -> settings.character.name（缺省空串，不发占位）
        {character_display}-> settings.character.display_name（缺省同 name）
        {companion_key}    -> settings.companion.key（缺省'companion'）
        {companion_names}  -> settings.companion.names 逗号拼接
    模板里未出现的占位符不影响；替换后仍未替换的 {"{"} 原样保留（防止误伤 JSON）。
    """
    character = settings.character or {}
    companion = settings.companion or {}
    name = character.get("name") or ""
    display = character.get("display_name") or name
    key = companion.get("key") or "companion"
    names = companion.get("names") or []
    mapping = {
        "character_name": name,
        "character_display": display,
        "companion_key": key,
        "companion_names": ",".join(str(n) for n in names),
    }
    for k, v in mapping.items():
        prompt_text = prompt_text.replace("{" + k + "}", str(v))
    return prompt_text


# ---------------------------------------------------------------------------
# 顶层便捷：一调用例返回 settings + tz（脚本 main() 入口统一用）
# ---------------------------------------------------------------------------

def bootstrap(settings_arg: str | None = None, extra_cli: list | None = None) -> tuple[Settings, ZoneInfo]:
    """脚本标准入口：加载 settings → 建 tz → 返回。"""
    settings = load_settings(settings_arg)
    tz = make_tz(settings.get("runtime.timezone"))
    return settings, tz
