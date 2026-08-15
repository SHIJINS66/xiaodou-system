#!/usr/bin/env python3
"""weather_provider — 等价迁移自旧 step02/bin/providers/weather_provider.py。

读取可选天气文件；未提供时返回「不得虚构」的标记，供 planner prompt 注入。
零外部依赖，纯文件读取。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import load_json


def load_weather_context(path: Path | None) -> dict[str, Any]:
    if path and path.is_file():
        value = load_json(path)
        return {
            "available": True,
            "source": str(path),
            "summary": value.get("summary") or "天气文件未提供 summary",
            "raw": value,
        }
    return {
        "available": False,
        "source": None,
        "summary": "未提供实时天气。Planner 不得虚构具体气温或降水。",
        "raw": None,
    }
