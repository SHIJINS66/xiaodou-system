#!/usr/bin/env python3
"""schema_tools — 等价迁移自 step04-releases 的 schema_tools.py。

与 schema_engine（无副作用校验器）配合：读 schema 文件、audit、然后 validate 实例，
失败统一抛 StepError。step02/03/04 各有一份几乎相同的实现，这里统一一份。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import StepError
from schema_engine import audit_schema, validate as validate_instance


def validate(value: Any, path: Path) -> None:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        audit = audit_schema(schema)
        errors = validate_instance(value, schema)
    except Exception as exc:
        raise StepError(f"invalid schema {path}: {type(exc).__name__}") from exc
    if audit or errors:
        raise StepError("schema validation failed: " + "; ".join((audit + errors)[:20]))


def assert_schema(path: Path) -> None:
    schema = json.loads(path.read_text(encoding="utf-8"))
    errors = audit_schema(schema)
    if errors:
        raise StepError("; ".join(errors))
