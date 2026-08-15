#!/usr/bin/env python3
"""
Update workspace MEMORY.md with long-term memory candidates from daily memory.

Two modes:
- incremental: extract long_term_memory_candidates from a daily memory file and
  merge them into MEMORY.md (called nightly after memory generation)
- prune: remove daily memory files older than retention_days (called nightly)
- compress: deduplicate and compress MEMORY.md via LLM (called weekly)

Usage:
    python3 bin/update_memory_md.py --date 2026-07-27 --config /etc/xiaodou/step04.json --mode incremental
    python3 bin/update_memory_md.py --mode prune --retention-days 7 --config /etc/xiaodou/step04.json
    python3 bin/update_memory_md.py --mode compress --config /etc/xiaodou/step04.json --max-chars 5000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common import StepError, atomic_write_text, load_json, make_tz, sha256_file

TZ = ZoneInfo("Asia/Shanghai")
NOT_MODIFIED = "MEMORY_MD_NOT_MODIFIED"
UPDATED = "MEMORY_MD_UPDATED"
PRUNED = "MEMORY_MD_DAILY_PRUNED"

# ---- constants ----
MAX_MEMORY_MD_CHARS = 5000
DEFAULT_RETENTION_DAYS = 7
MEMORY_MD_SECTIONS = [
    "## 当前状态",
    "## 我和对方的长期共同经历",
    "## 对方的稳定偏好",
    "## 我的稳定偏好",
    "## 我们的关系长期状态",
    "## 持续话题与共同计划",
    "## 尚未解决的事项",
    "## 近期日常",
]
DAILY_BODIES_RETENTION_DAYS = 7
DAILY_BODY_SEPARATOR = "\n---\n"

# ---- prompt for weekly compression ----
COMPRESS_PROMPT = """你是一个长期记忆压缩器。将以下 MEMORY.md 的所有 section 内容整合为一个精炼版本，最大 {max_chars} 字。

原始内容：
{raw_content}

规则：
1. 保持六个 section 的结构不变：当前状态、长期共同经历、稳定偏好、关系状态、持续话题与计划、未解决事项
2. 删除重复条目，合并相似内容
3. 保留时间背景（日期标注），但相近日期可以合并（如 "2026-07-27~30: ..."）
4. 以下内容不可丢弃，即使只出现过一次：
   - 计划、承诺、约定
   - 关系变化或重要对话
   - 对方明确表达的稳定偏好
   - 尚未解决的事项
5. 已完成的事项可以删除（标记为"已完成"的不需要保留）
6. "当前状态" 更新压缩时间、条目数、总字数
7. 如果某 section 没有任何内容，写"暂无"即可
8. 直接输出完整的 MEMORY.md，不要加任何解释性文字"""


def workspace_dir(config: dict[str, Any]) -> Path:
    # fallback 到用户自己的 workspace（~/.openclaw/workspace），不写死系统 root。
    return Path(config.get("workspace", str(Path.home() / ".openclaw/workspace"))).resolve()


def memory_md_path(config: dict[str, Any]) -> Path:
    return workspace_dir(config) / "MEMORY.md"


def daily_md_path(config: dict[str, Any], day: str) -> Path:
    return workspace_dir(config) / "daily" / f"{day}.md"


def write_run_log(config: dict[str, Any], mode: str, payload: dict[str, Any]) -> None:
    """Persist a run's JSON result to a per-mode log file, so a run is never
    silently lost even if the cron redirect is missing. Best-effort: never raise."""
    try:
        log_root = Path(
            config.get("memory_md_log_root", "/var/log/xiaodou")
        ).resolve()
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / f"step04-memory-{mode}.log"
        payload.setdefault("mode", mode)
        payload.setdefault("ts", datetime.now(TZ).isoformat(timespec="seconds"))
        line = json.dumps(payload, ensure_ascii=False)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # never let logging break the run
        try:
            sys.stderr.write(f"STEP04_MEMORY_RUN_LOG_WARN: {exc}\n")
        except Exception:
            pass


def load_memory_md(config: dict[str, Any]) -> str:
    """Read current MEMORY.md. Returns empty str if missing."""
    path = memory_md_path(config)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def extract_section(text: str, heading: str) -> str:
    """Extract content under a specific heading from MEMORY.md.
    Includes bullet points and blank lines but stops at the next ## heading."""
    # Match the heading and grab everything until next ## heading
    pattern = rf"{re.escape(heading)}\n(.*?)(?=\n##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return ""
    return m.group(1).rstrip()


def replace_section(text: str, heading: str, new_content: str) -> str:
    """Replace content under a heading. Appends if heading doesn't exist."""
    pattern = rf"{re.escape(heading)}\n(.*?)(?=\n## |\Z)"
    replacement = f"{heading}\n{new_content}"
    if re.search(pattern, text, re.DOTALL):
        return re.sub(pattern, replacement, text, flags=re.DOTALL, count=1)
    # Heading not found — append at end
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + replacement


def extract_candidates(daily_path: Path) -> list[str]:
    """Read daily memory markdown and extract long_term_memory_candidates as individual lines."""
    if not daily_path.is_file():
        return []
    content = daily_path.read_text(encoding="utf-8")
    section = extract_section(content, "## 长期记忆候选")
    if not section or section.strip() in ("无", "暂无", "无。"):
        return []
    lines: list[str] = []
    for line in section.strip().split("\n"):
        stripped = line.strip()
        if stripped.startswith("- ") and stripped not in ("- 无", "- 无。", "- 暂无"):
            lines.append(stripped)
    return lines


def dedup_candidates(existing: str, new_lines: list[str]) -> list[str]:
    """Return only new_lines entries that don't already exist in MEMORY.md."""
    result = []
    existing_lower = existing.lower()
    for line in new_lines:
        # Check if a similar line already exists (case-insensitive substring match)
        # Extract the text part after "- "
        text = line[2:] if line.startswith("- ") else line
        if text.lower() not in existing_lower:
            result.append(line)
    return result


def append_to_section(memory_md: str, heading: str, lines: list[str]) -> str:
    """Append lines to an existing section in MEMORY.md. Creates section if missing."""
    current = extract_section(memory_md, heading)
    # Clean up divider lines and placeholder text from old sections
    current_clean = current.strip()
    for placeholder in ("暂无", "暂无。", "暂无需要从 daily memory 追加的内容。",
                        "暂无需要从 daily memory 追加的变化。"):
        if current_clean.startswith(placeholder):
            current_clean = current_clean[len(placeholder):].strip()
    # Remove leading --- dividers
    current_clean = re.sub(r'^---\s*', '', current_clean).strip()
    # Also remove trailing --- before appending
    current_clean = re.sub(r'\n?---\s*$', '', current_clean).strip()

    if current_clean and current_clean not in ("无", "暂无", "暂无。"):
        new_body = current_clean.rstrip() + "\n" + "\n".join(lines)
    else:
        new_body = "\n".join(lines)
    return replace_section(memory_md, heading, new_body)


def build_initial_memory_md() -> str:
    """Build a fresh MEMORY.md skeleton if the file is empty/missing."""
    return (
        "# MEMORY.md\n\n"
        "## 当前状态\n"
        "暂无\n\n"
        "## 我和对方的长期共同经历\n"
        "暂无\n\n"
        "## 对方的稳定偏好\n"
        "暂无\n\n"
        "## 我的稳定偏好\n"
        "暂无\n\n"
        "## 我们的关系长期状态\n"
        "暂无\n\n"
        "## 持续话题与共同计划\n"
        "暂无\n\n"
        "## 尚未解决的事项\n"
        "暂无\n\n"
        "## 近期日常\n"
        "暂无\n"
    )


# ============================================================================
# Mode: incremental — append candidates from one daily memory to MEMORY.md
# ============================================================================
def run_incremental(config: dict[str, Any], day: str) -> dict[str, Any]:
    daily_path = daily_md_path(config, day)
    mem_path = memory_md_path(config)

    # 1. Load (or create) MEMORY.md. If it's the old skeleton template,
    #    replace it with a clean structure.
    has_standard_sections = False
    if mem_path.is_file():
        memory_md = mem_path.read_text(encoding="utf-8")
        # Detect old skeleton template: contains placeholder text from initial workspace setup
        is_old_skeleton = any(
            phrase in memory_md
            for phrase in [
                "暂无需要从 daily memory 追加的内容。",
                "暂无需要从 daily memory 追加的变化。",
                "这是新工作区的长期记忆起点。",
                "不能为了填满本文件而重复抄写",
                "## 更新规则",
            ]
        )
        if not is_old_skeleton:
            has_standard_sections = True
    else:
        memory_md = ""

    if not has_standard_sections:
        # Migrate old content or start fresh
        memory_md = build_initial_memory_md()
        atomic_write_text(mem_path, memory_md)

    # 2. Extract candidates from daily memory
    candidates = extract_candidates(daily_path)

    # 3. Dedup against existing MEMORY.md content
    new_entries = dedup_candidates(memory_md, candidates)

    if not new_entries:
        return {
            "status": NOT_MODIFIED,
            "date": day,
            "candidates_found": len(candidates),
            "new_entries": 0,
            "memory_md_size": len(memory_md),
        }

    # 4. Append new entries to appropriate sections
    # long_term_memory_candidates are primarily life experiences and preferences.
    # We merge them into "长期共同经历" or "稳定偏好" based on content heuristics.
    life_lines: list[str] = []
    pref_lines: list[str] = []

    for entry in new_entries:
        text = entry[2:] if entry.startswith("- ") else entry  # strip "- "
        # Heuristic: preference-like candidates mention 喜欢/偏好/习惯
        if any(kw in text for kw in ["喜欢", "偏好", "习惯", "爱", "常用", "经常", "一直"]):
            pref_lines.append(entry)
        else:
            life_lines.append(entry)

    # Append to sections (with date prefix)
    date_prefix = day
    if life_lines:
        dated_life = [f"{line}（{date_prefix}）" for line in life_lines]
        memory_md = append_to_section(memory_md, "## 我和对方的长期共同经历", dated_life)
    if pref_lines:
        dated_pref = [f"{line}（{date_prefix}）" for line in pref_lines]
        memory_md = append_to_section(memory_md, "## 对方的稳定偏好", dated_pref)

    # 5. Update "当前状态" timestamp
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    status = f"上次更新: {now_str}（{day} 增量追加）"
    memory_md = replace_section(memory_md, "## 当前状态", status)

    # 6. Write
    total_chars = len(memory_md)
    if total_chars > MAX_MEMORY_MD_CHARS:
        # Soft warn — the weekly compress will fix this
        status += f"\n⚠️ 接近上限 ({total_chars}/{MAX_MEMORY_MD_CHARS} 字)，下次压缩将处理"
        memory_md = replace_section(memory_md, "## 当前状态", status)

    atomic_write_text(mem_path, memory_md)

    return {
        "status": UPDATED,
        "date": day,
        "candidates_found": len(candidates),
        "new_entries": len(new_entries),
        "life_entries": len(life_lines),
        "pref_entries": len(pref_lines),
        "memory_md_size": len(memory_md),
    }


# ============================================================================
# Mode: attach — inject full daily memory body into MEMORY.md (retain last 7 days)
# ============================================================================
def run_attach(config: dict[str, Any], day: str, retention_days: int = DAILY_BODIES_RETENTION_DAYS) -> dict[str, Any]:
    daily_path = daily_md_path(config, day)
    mem_path = memory_md_path(config)
    heading = "## 近期日常"

    # 1. Load daily memory
    if not daily_path.is_file():
        return {"status": NOT_MODIFIED, "reason": "daily_missing", "date": day}
    daily_raw = daily_path.read_text(encoding="utf-8")

    # 2. Strip "长期记忆候选" section (that's for incremental)
    daily_body = re.split(r"\n## 长期记忆候选\b", daily_raw)[0].rstrip()
    # Remove the leading "# <角色名> Daily Memory — YYYY-MM-DD" header line（角色名不写死）
    daily_body = re.sub(r"^# .+ Daily Memory.*\n\n?", "", daily_body, count=1)
    # Strip evidence comments (cleaner for prompt injection)
    daily_body = re.sub(r'\s*<!--\s*evidence:.*?-->', '', daily_body)
    if not daily_body.strip():
        return {"status": NOT_MODIFIED, "reason": "empty_body", "date": day}

    # Build entry with date header
    entry = f"### {day}\n{daily_body.strip()}"

    # 3. Load MEMORY.md
    has_standard = False
    if mem_path.is_file():
        memory_md = mem_path.read_text(encoding="utf-8")
        for heading_check in MEMORY_MD_SECTIONS:
            if heading_check in memory_md:
                has_standard = True
                break
    else:
        memory_md = ""
    if not has_standard:
        memory_md = build_initial_memory_md()
        atomic_write_text(mem_path, memory_md)

    # 4. Parse existing daily bodies
    cutoff_date = datetime.now(TZ).date() - timedelta(days=retention_days)
    cutoff_str = cutoff_date.isoformat()
    existing_section = extract_section(memory_md, heading)
    # Deduplicate existing entries
    oldest_kept_date: str | None = None
    if existing_section and existing_section.strip() not in ("暂无", "暂无。", "无"):
        entries: dict[str, str] = {}
        # Parse entries delimited by "### YYYY-MM-DD"
        parts = re.split(r"\n(?=### \d{4}-\d{2}-\d{2})", existing_section.strip())
        for part in parts:
            m = re.match(r"^### (\d{4}-\d{2}-\d{2})", part)
            if m:
                entries[m.group(1)] = part.strip()
        # Filter: keep only last `retention_days` days, exclude today's date
        kept: list[str] = []
        for d in sorted(entries.keys(), reverse=True):
            if d == day:
                continue  # will re-add fresh below
            if d >= cutoff_str:
                kept.append(entries[d])
            elif oldest_kept_date is None or d < oldest_kept_date:
                oldest_kept_date = d
        # Prepend today's entry
        kept = [entry] + kept
        new_body = DAILY_BODY_SEPARATOR.join(kept)
        removed_count = len(entries) - (len(kept) - 1)  # minus today's new one
    else:
        kept = [entry]
        new_body = entry
        removed_count = 0
        entries = {}

    memory_md = replace_section(memory_md, heading, new_body)

    # 5. Write
    atomic_write_text(mem_path, memory_md)

    return {
        "status": UPDATED,
        "date": day,
        "daily_body_size": len(daily_body),
        "entries_before": len(entries) if existing_section and existing_section.strip() not in ("暂无", "暂无。", "无") else 0,
        "entries_after": len(kept),
        "removed_old": removed_count,
        "oldest_removed": oldest_kept_date,
        "memory_md_size": len(memory_md),
    }


# ============================================================================
# Mode: prune — remove daily memory files older than retention_days
# ============================================================================
def run_prune(config: dict[str, Any], retention_days: int) -> dict[str, Any]:
    daily_dir = workspace_dir(config) / "daily"
    if not daily_dir.is_dir():
        return {"status": NOT_MODIFIED, "reason": "daily_dir_missing"}

    cutoff = datetime.now(TZ).date() - timedelta(days=retention_days)
    cutoff_str = cutoff.isoformat()
    removed: list[str] = []

    for child in sorted(daily_dir.iterdir()):
        if not child.is_file():
            continue
        # Match YYYY-MM-DD.md pattern
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", child.name)
        if not m:
            continue
        file_date = m.group(1)
        if file_date < cutoff_str:
            child.unlink()
            removed.append(file_date)

    return {
        "status": PRUNED if removed else NOT_MODIFIED,
        "retention_days": retention_days,
        "cutoff_date": cutoff_str,
        "removed": removed,
        "count_removed": len(removed),
    }


# ============================================================================
# Mode: compress — deduplicate and compress MEMORY.md via LLM
# ============================================================================
def run_compress(config: dict[str, Any], max_chars: int) -> dict[str, Any]:
    mem_path = memory_md_path(config)
    if not mem_path.is_file():
        return {"status": NOT_MODIFIED, "reason": "memory_md_missing"}

    raw = mem_path.read_text(encoding="utf-8")
    before_size = len(raw)

    if before_size <= max_chars * 0.6:
        # Not enough content to warrant compression
        return {
            "status": NOT_MODIFIED,
            "reason": "under_threshold",
            "before_size": before_size,
            "max_chars": max_chars,
        }

    # LLM compress
    try:
        from openai import OpenAI
    except ImportError:
        raise StepError("openai dependency is not installed")

    env: dict[str, str] = {}
    env_path = Path(config.get("env_file", "/etc/xiaodou/xiaodou.env"))
    if env_path.is_file():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")

    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise StepError("DEEPSEEK_API_KEY not configured")

    prompt = COMPRESS_PROMPT.format(max_chars=max_chars, raw_content=raw)

    client = OpenAI(
        base_url=config["deepseek_base_url"],
        api_key=api_key,
        timeout=config.get("request_timeout_seconds", 240),
    )

    response = client.chat.completions.create(
        model=config.get("deepseek_model", "deepseek-v4-pro"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=8192,
    )
    compressed = response.choices[0].message.content.strip()

    after_size = len(compressed)

    # Validate: must shrink
    if after_size > before_size * 1.1:
        return {
            "status": NOT_MODIFIED,
            "reason": "no_reduction",
            "before_size": before_size,
            "after_size": after_size,
        }

    # Validate: must keep section structure
    for heading in MEMORY_MD_SECTIONS:
        if heading not in compressed:
            return {
                "status": NOT_MODIFIED,
                "reason": f"section_missing:{heading}",
                "before_size": before_size,
                "after_size": after_size,
            }

    # Write
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    compressed = replace_section(
        compressed,
        "## 当前状态",
        f"上次压缩: {now_str}（{before_size}→{after_size} 字，{max_chars} 字上限）",
    )

    atomic_write_text(mem_path, compressed)

    return {
        "status": UPDATED,
        "before_size": before_size,
        "after_size": after_size,
        "reduction_pct": round((1 - after_size / before_size) * 100, 1),
        "max_chars": max_chars,
    }


# ============================================================================
# CLI
# ============================================================================
def main():
    global TZ
    parser = argparse.ArgumentParser(description="Update workspace MEMORY.md")
    parser.add_argument("--settings", help="Path to settings.yaml（从它构造 config）")
    parser.add_argument("--config", help="旧式 step04.json（与 --settings 二选一）")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["incremental", "prune", "compress", "attach"],
        help="Operation mode",
    )
    parser.add_argument("--date", help="Date for incremental mode (YYYY-MM-DD)")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"Days to retain daily memory files (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=MAX_MEMORY_MD_CHARS,
        help=f"Max characters for compressed MEMORY.md (default: {MAX_MEMORY_MD_CHARS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without writing",
    )

    args = parser.parse_args()
    if args.settings:
        from step04_config import load_step04_config
        config = load_step04_config(args.settings)
        _s = config.get('_settings')
        _tz = (_s.get('runtime') or {}).get('timezone') if _s is not None else None
        if _tz:
            TZ = make_tz(_tz)
    elif args.config:
        config = load_json(Path(args.config))
        _tz = (config.get('runtime') or {}).get('timezone')
        if _tz:
            TZ = make_tz(_tz)
    else:
        print(json.dumps({"error": "--settings 或 --config 必填"}))
        sys.exit(2)

    if args.mode == "incremental":
        if not args.date:
            print(json.dumps({"error": "--date required for incremental mode"}))
            sys.exit(1)
        if args.dry_run:
            daily_path = daily_md_path(config, args.date)
            candidates = extract_candidates(daily_path)
            memory_md = load_memory_md(config)
            new_entries = dedup_candidates(memory_md, candidates)
            print(json.dumps({
                "mode": "incremental",
                "dry_run": True,
                "date": args.date,
                "candidates_in_daily": len(candidates),
                "new_entries_after_dedup": len(new_entries),
                "memory_md_size": len(memory_md),
                "would_write": len(new_entries) > 0,
            }, ensure_ascii=False))
        else:
            result = run_incremental(config, args.date)
            write_run_log(config, "incremental", result)
            print(json.dumps(result, ensure_ascii=False))

    elif args.mode == "prune":
        if args.dry_run:
            daily_dir = workspace_dir(config) / "daily"
            cutoff = datetime.now(TZ).date() - timedelta(days=args.retention_days)
            cutoff_str = cutoff.isoformat()
            to_remove = []
            if daily_dir.is_dir():
                for child in sorted(daily_dir.iterdir()):
                    m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", child.name)
                    if m and m.group(1) < cutoff_str:
                        to_remove.append(m.group(1))
            print(json.dumps({
                "mode": "prune",
                "dry_run": True,
                "cutoff_date": cutoff_str,
                "retention_days": args.retention_days,
                "would_remove": to_remove,
                "count": len(to_remove),
            }, ensure_ascii=False))
        else:
            result = run_prune(config, args.retention_days)
            write_run_log(config, "prune", result)
            print(json.dumps(result, ensure_ascii=False))

    elif args.mode == "compress":
        if args.dry_run:
            mem_path = memory_md_path(config)
            size = len(mem_path.read_text(encoding="utf-8")) if mem_path.is_file() else 0
            print(json.dumps({
                "mode": "compress",
                "dry_run": True,
                "memory_md_size": size,
                "max_chars": args.max_chars,
                "would_compress": size > args.max_chars * 0.6,
            }, ensure_ascii=False))
        else:
            result = run_compress(config, args.max_chars)
            write_run_log(config, "compress", result)
            print(json.dumps(result, ensure_ascii=False))

    elif args.mode == "attach":
        if not args.date:
            print(json.dumps({"error": "--date required for attach mode"}))
            sys.exit(1)
        if args.dry_run:
            daily_path = daily_md_path(config, args.date)
            has_daily = daily_path.is_file()
            mem_path = memory_md_path(config)
            mem_size = len(mem_path.read_text(encoding="utf-8")) if mem_path.is_file() else 0
            print(json.dumps({
                "mode": "attach",
                "dry_run": True,
                "date": args.date,
                "daily_exists": has_daily,
                "memory_md_size": mem_size,
            }, ensure_ascii=False))
        else:
            result = run_attach(config, args.date, args.retention_days)
            write_run_log(config, "attach", result)
            print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
