#!/usr/bin/env python3
"""settings loader — 框架配置加载与 .env 合并。

替代旧系统里散落各处的：
    load_json("/etc/xiaodou/step03.json")
    load_env_file("/etc/xiaodou/xiaodou.env")
    load_json("/etc/xiaodou/runtime.json")

职责：
1. 定位 settings.yaml（优先 --settings 参数，其次环境变量 COMPANION_SETTINGS，
   再其次 <root_dir>/settings.yaml）。
2. 加载 YAML，合并 .env 里的环境变量，做 `${VAR}` 占位替换。
3. 派生出所有运行目录（runtime.dirs + var 锁目录 + state/journal/selfies）。
4. 校验 secrets.required_env 声明的密钥是否齐全（缺则报错并提示，不静默放行）。

设计约束（打包原则）：
    - 不 import 任何 /opt/xiaodou 的代码。
    - 不写死任何真实路径；root_dir 默认为 settings.yaml 所在目录。
    - 只依赖标准库 + PyYAML（requirements 里声明）。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class ConfigError(RuntimeError):
    """配置加载/校验失败。"""


def _expand_env(value: Any) -> Any:
    """递归展开字符串里的 ${VAR} 与 $VAR，值来自 os.environ。"""
    if isinstance(value, str):
        result = os.path.expandvars(value) if "$" in value else value
        return result
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _load_env_file(path: Path) -> dict[str, str]:
    """读取 .env（不覆盖已存在的进程环境变量）。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        # 进程环境变量优先，不覆盖
        if k and k not in os.environ:
            out[k] = v
    return out


@dataclass
class Settings:
    """加载并派生后的全部配置。"""

    path: Path
    root_dir: Path
    raw: dict[str, Any]
    env: dict[str, str] = field(default_factory=dict)

    @property
    def character(self) -> dict[str, Any]:
        return self.raw.get("character", {})

    @property
    def companion(self) -> dict[str, Any]:
        return self.raw.get("companion", {})

    @property
    def system(self) -> dict[str, Any]:
        return self.raw.get("system", {})

    @property
    def models(self) -> dict[str, Any]:
        return self.raw.get("models", {})

    @property
    def selfie(self) -> dict[str, Any]:
        return self.raw.get("selfie", {})

    @property
    def delivery(self) -> dict[str, Any]:
        return self.raw.get("delivery", {})

    @property
    def planning(self) -> dict[str, Any]:
        return self.raw.get("planning", {})

    @property
    def memory(self) -> dict[str, Any]:
        return self.raw.get("memory", {})

    @property
    def interaction_policy(self) -> dict[str, Any]:
        return self.raw.get("interaction_policy", {})

    @property
    def scheduling(self) -> dict[str, Any]:
        return self.raw.get("scheduling", {})

    # ---- 键值便捷读取（带默认值，替代散落各处 config.get(...)）----

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # ---- 运行目录派生（替代硬编码 /var/lib /var/lock /daily_selfies）----

    def dir(self, name: str) -> Path:
        """从 runtime.dirs 取目录名（相对 root_dir 解析）。"""
        dirs = self.raw.get("runtime", {}).get("dirs", {})
        rel = dirs.get(name)
        if not rel:
            raise ConfigError(f"runtime.dirs.{name} 未配置")
        return self.root_dir / rel

    @property
    def state_dir(self) -> Path:
        # 状态目录固定放 root_dir 下 var/state；旧系统是 /var/lib/xiaodou/step03
        return self.root_dir / "var" / "state"

    @property
    def journal_dir(self) -> Path:
        # 旧系统 /var/lib/xiaodou/journal
        return self.root_dir / "var" / "journal"

    @property
    def lock_dir(self) -> Path:
        # 旧系统 /var/lock/xiaodou-*
        return self.root_dir / "var" / "lock"

    def ensure_dirs(self) -> None:
        for p in (
            self.dir("daily"),
            self.dir("chatlog"),
            self.dir("memory"),
            self.dir("selfies"),
            self.dir("logs"),
            self.state_dir,
            self.journal_dir,
            self.lock_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _detect_settings_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("COMPANION_SETTINGS")
    if env:
        return Path(env).resolve()
    # 缺省：./settings.yaml（当前工作目录）
    return Path("settings.yaml").resolve()


def load_settings(explicit_path: str | None = None) -> Settings:
    """加载整套配置。外部脚本统一入口。"""
    if yaml is None:
        raise ConfigError("缺少 PyYAML 依赖；请运行 `pip install -r requirements.txt`")

    path = _detect_settings_path(explicit_path)
    if not path.is_file():
        raise ConfigError(f"找不到 settings 文件：{path}（可用 --settings 或 COMPANION_SETTINGS 指定）")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"settings.yaml 解析失败：{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("settings.yaml 根节点必须是对象")

    # root_dir：默认 settings 文件所在目录；支持 ~ 展开到用户主目录
    runtime = raw.get("runtime", {}) or {}
    root_dir_raw = runtime.get("root_dir", ".")
    root_dir = Path(root_dir_raw).expanduser()
    if not root_dir.is_absolute():
        root_dir = (path.parent / root_dir).resolve()

    # .env：默认 root_dir/.env，可用 runtime.env_file 覆盖
    env_file_raw = runtime.get("env_file", ".env")
    env_file = Path(env_file_raw)
    if not env_file.is_absolute():
        env_file = root_dir / env_file
    env = _load_env_file(env_file)
    os.environ.update(env)

    # ${VAR} 展开
    raw = _expand_env(raw)

    settings = Settings(path=path, root_dir=root_dir, raw=raw, env=env)
    _validate_secrets(settings)
    settings.ensure_dirs()
    return settings


def _validate_secrets(settings: Settings) -> None:
    """校验 secrets.required_env 声明的环境变量是否已就绪。"""
    required = settings.get("secrets.required_env") or []
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "缺少必需的环境变量："
            + ", ".join(missing)
            + "。请在 .env 或系统环境中配置（参考 .env.example）。"
        )


def _cli() -> int:
    """供手动自检：`python scripts/settings_loader.py --settings settings.yaml`"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="加载并自检 settings")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--show", action="store_true", help="打印派生后的关键字段")
    args = parser.parse_args()

    try:
        s = load_settings(args.settings)
    except ConfigError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    if args.show:
        payload = {
            "path": str(s.path),
            "root_dir": str(s.root_dir),
            "character": s.character,
            "companion": s.companion,
            "instance_name": s.get("system.instance_name"),
            "dirs": {
                name: str(s.dir(name))
                for name in ("daily", "chatlog", "memory", "selfies", "logs")
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"settings OK: {s.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
