#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import date as Date, timedelta
from pathlib import Path
from typing import Any

from common import StepError, load_json, sha256_file
from schema_tools import validate
from common import package_root, schema_path


def carryover_root(config: dict[str, Any]) -> Path:
    return Path(config.get("carryover_root") or (Path(config["state_root"]) / "carryover"))


def rollover_root(config: dict[str, Any]) -> Path:
    return Path(config.get("rollover_root") or (Path(config["state_root"]) / "rollover"))


def carryover_path(config: dict[str, Any], date_value: str) -> Path:
    return carryover_root(config) / f"{date_value}.json"


def rollover_state_path(config: dict[str, Any], date_value: str) -> Path:
    return rollover_root(config) / date_value / "state.json"


def rollover_receipt_path(config: dict[str, Any], date_value: str) -> Path:
    return rollover_root(config) / date_value / "receipt.json"


def _schema(name: str) -> Path:
    return schema_path(package_root(__file__), name.replace(".schema.json", ""))


def load_carryover(config: dict[str, Any], target: Date) -> dict[str, Any] | None:
    path = carryover_path(config, target.isoformat())
    receipt = rollover_receipt_path(config, target.isoformat())
    if not path.is_file():
        if receipt.is_file():
            raise StepError("rollover receipt exists but carryover is missing")
        return None
    value = load_json(path)
    validate(value, _schema("session_carryover_v1.schema.json"))
    if value["date"] != target.isoformat():
        raise StepError("carryover date mismatch")
    canonical = json.dumps(value["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != value["content_sha256"]:
        raise StepError("carryover content hash mismatch")
    if receipt.is_file():
        receipt_value = load_json(receipt)
        validate(receipt_value, _schema("session_rollover_record_v1.schema.json"))
        if receipt_value.get("carryover_sha256") != sha256_file(path):
            raise StepError("rollover receipt carryover hash mismatch")
    return value


def next_rollover_seals(config: dict[str, Any], target: Date) -> dict[str, Any] | None:
    next_date = (target + timedelta(days=1)).isoformat()
    path = rollover_receipt_path(config, next_date)
    if not path.is_file():
        return None
    value = load_json(path)
    validate(value, _schema("session_rollover_record_v1.schema.json"))
    if value.get("status") != "completed":
        return None
    carry = Path(str(value.get("carryover_path", "")))
    if not carry.is_file() or sha256_file(carry) != value.get("carryover_sha256"):
        raise StepError("sealing rollover receipt references drifted carryover")
    return value
