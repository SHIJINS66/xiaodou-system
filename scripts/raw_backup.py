#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable

from common import StepError, now_iso, sha256_file


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_arcname(name: str) -> str:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise StepError("unsafe raw-backup archive name")
    return path.as_posix()


def _entry(name: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise StepError(f"raw-backup source missing: {path}")
    return {
        "name": _safe_arcname(name),
        "source_path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(_safe_arcname(name))
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _verify_archive(
    path: Path,
    *,
    expected_date: str | None = None,
    expected_source_fingerprint: str | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise StepError("raw-backup archive is missing")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if any(
                member.name.startswith("/")
                or ".." in Path(member.name).parts
                or member.issym()
                or member.islnk()
                for member in members
            ):
                raise StepError("raw-backup archive contains unsafe member")
            manifest_member = archive.getmember("manifest.json")
            handle = archive.extractfile(manifest_member)
            if handle is None:
                raise StepError("raw-backup manifest is unreadable")
            manifest = json.loads(handle.read().decode("utf-8"))
            if manifest.get("schema_version") != "1.0":
                raise StepError("raw-backup manifest version mismatch")
            archive_date = manifest.get("date")
            source_fingerprint = manifest.get("source_fingerprint")
            if not isinstance(archive_date, str) or not archive_date:
                raise StepError("raw-backup manifest date is missing")
            if (
                not isinstance(source_fingerprint, str)
                or len(source_fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in source_fingerprint)
            ):
                raise StepError("raw-backup manifest source fingerprint is invalid")
            if expected_date is not None and archive_date != expected_date:
                raise StepError("raw-backup date mismatch")
            if (
                expected_source_fingerprint is not None
                and source_fingerprint != expected_source_fingerprint
            ):
                raise StepError("raw-backup source fingerprint mismatch")
            entries = manifest.get("entries")
            if not isinstance(entries, list) or not entries:
                raise StepError("raw-backup manifest entries are missing")
            expected_names = {"manifest.json"}
            for item in entries:
                if not isinstance(item, dict):
                    raise StepError("raw-backup manifest entry is invalid")
                name = _safe_arcname(str(item.get("name", "")))
                expected_names.add(name)
                member = archive.getmember(name)
                if not member.isfile():
                    raise StepError("raw-backup member is not a regular file")
                stream = archive.extractfile(member)
                if stream is None:
                    raise StepError("raw-backup member is unreadable")
                data = stream.read()
                if len(data) != item.get("size") or _sha_bytes(data) != item.get("sha256"):
                    raise StepError("raw-backup member verification failed")
            actual_names = [member.name for member in members]
            if len(actual_names) != len(set(actual_names)):
                raise StepError("raw-backup archive contains duplicate members")
            if set(actual_names) != expected_names:
                raise StepError("raw-backup member set differs from manifest")
    except (tarfile.TarError, json.JSONDecodeError, KeyError) as exc:
        raise StepError("raw-backup archive verification failed") from exc
    return {
        "archive": str(path),
        "verified": True,
        "date": archive_date,
        "source_fingerprint": source_fingerprint,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "entry_count": len(entries),
    }


def verify(record: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(record.get("archive", "")))
    checked = _verify_archive(
        path,
        expected_date=(str(record["date"]) if record.get("date") is not None else None),
        expected_source_fingerprint=(
            str(record["source_fingerprint"])
            if record.get("source_fingerprint") is not None
            else None
        ),
    )
    if record.get("verified") is not True or record.get("sha256") != checked["sha256"]:
        raise StepError("raw-backup state record drift")
    return checked


def create(
    config: dict[str, Any],
    *,
    date: str,
    source_fingerprint: str,
    files: Iterable[tuple[str, Path]],
) -> dict[str, Any]:
    root = Path(config.get("raw_backup_root") or (Path(config["backup_root"]) / "step04-raw")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    destination = root / date / f"{date}-{source_fingerprint[:16]}.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    if destination.is_file():
        return _verify_archive(
            destination,
            expected_date=date,
            expected_source_fingerprint=source_fingerprint,
        )

    entries = [_entry(name, path.resolve()) for name, path in files]
    names = [item["name"] for item in entries]
    if len(names) != len(set(names)):
        raise StepError("duplicate raw-backup archive name")
    manifest_entries = [
        {key: item[key] for key in ("name", "size", "sha256")}
        for item in entries
    ]
    manifest = {
        "schema_version": "1.0",
        "kind": "step04-raw-evidence",
        "date": date,
        "source_fingerprint": source_fingerprint,
        "created_at": now_iso(),
        "entries": manifest_entries,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            _add_bytes(archive, "manifest.json", manifest_bytes)
            for item in entries:
                _add_bytes(archive, item["name"], Path(item["source_path"]).read_bytes())
        temporary.chmod(0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _verify_archive(
            temporary,
            expected_date=date,
            expected_source_fingerprint=source_fingerprint,
        )
        os.replace(temporary, destination)
        destination.chmod(0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return _verify_archive(
        destination,
        expected_date=date,
        expected_source_fingerprint=source_fingerprint,
    )
