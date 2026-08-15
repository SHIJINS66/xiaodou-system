#!/usr/bin/env python3
"""即时自拍（通用版）。

从 /opt/xiaodou/tools/on_demand_selfie.py 迁入，改动点（对应 HARDCODE_AUDIT §11）：
1. 去掉 `_STEP03 = Path("/opt/xiaodou/step03/bin")`，改用本框架的 settings + providers。
2. 去掉写死的 `step03_config_path`、`env_file`，改由 settings_loader 统一加载。
3. prompt 里写死的角色名语气 → `character.display_name` 占位；
   写死的「女生/女孩」→ `character.person_noun` 占位。
4. 生图 / spec 生成分别走 providers 抽象，按 settings 的 type 选择供应商。

用法：
    python3 scripts/on_demand_selfie.py \
        --settings settings.yaml \
        --daily var/daily/YYYY-MM-DD.json \
        --user-request "发张自拍"

输出：{success, image_path, caption, ...}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from providers.base import ChatMessage, create_image_provider, create_llm_provider
from providers.image.seedream import build_prompt
from scripts.settings_loader import ConfigError, load_settings

SELFIE_SPEC_PROMPT = """你是一个轻量自拍场景生成器。根据当前上下文，输出一个自拍规格 JSON。

当前上下文：
- 时间：{current_time}
- 活动：{activity}
- 地点：{location}
- 着装：{attire}
- 情绪：{mood}
- 用户请求：{user_request}

输出严格 JSON，只包含以下字段（全部必填，字符串）：
{{
  "camera": 固定为 "front_camera",
  "framing": "head_and_shoulders" 或 "half_body" 或 "full_body" 或 "mirror_selfie",
  "location": "当前地点（中文）",
  "outfit": "当前服装描述（中文）",
  "pose": "动作描述（中文）",
  "expression": "表情描述（中文）",
  "lighting": "光线描述（中文）",
  "background": "背景描述（中文）",
  "caption": "一句话配文，口语自然，不超过40字，像{display_name}本人的语气"
}}

规则：
- camera 必须固定为 front_camera，永远不用 back_camera
- framing 优先用 head_and_shoulders 或 half_body，只在场地有全身镜且需要全身自拍时才用 mirror_selfie
- 所有描述从第一人称主观视角出发：你就是画面中的{person_noun}，你正在看前置摄像头里的自己。
- pose 描述{person_noun}身体的姿态（歪头、托腮、撩头发、比耶、单手托脸等），必须是身体本身的动作，绝对不能描述拍摄动作（如"举手机""按快门""拿手机自拍"）
- expression 描述{person_noun}面部的表情
- background、lighting、location 从{person_noun}的眼睛高度描述周围环境
- 画面中只能出现这一个{person_noun}，没有第二个人
- mirror_selfie 时描述镜子中映出的{person_noun}的全身，镜中能看到{person_noun}举起的手臂轮廓
- 绝对不能出现以下词汇："前置自拍""举着手机""自拍""前置摄像头""拿手机""拍摄""入镜""镜头""照片"
- caption 必须口语化、自然，像{display_name}随手拍照说的话
- 表情和动作要匹配：开心就微笑，累就略疲惫感，困就迷蒙感
- 不要编造上下文里没有的服装或地点
- 只输出 JSON，不要任何其他文字"""


def _current_context(daily: dict, user_request: str, tz: str) -> dict:
    now = datetime.now(ZoneInfo(tz))
    current_iso = now.isoformat()
    current_time = now.strftime("%H:%M")

    activity = "未知"
    location = "上海"
    mood_label = "自然"
    attire = "日常便装"

    plan = daily.get("plan", {})
    timeline = plan.get("timeline", [])
    events = plan.get("events", [])

    for seg in timeline:
        if seg.get("start_at", "") <= current_iso < seg.get("end_at", ""):
            activity = seg.get("activity", activity)
            location = seg.get("location_label", location)
            mood = seg.get("mood", {})
            if isinstance(mood, dict):
                mood_label = mood.get("label", mood_label)
            attire = seg.get("attire", attire)
            break

    if attire == "日常便装":
        for e in events:
            if e.get("type") == "selfie":
                spec = e.get("selfie_spec", {})
                if spec.get("outfit"):
                    attire = spec["outfit"]
                    break

    runtime = daily.get("runtime", {})
    state = runtime.get("current_state", {})
    if state.get("activity"):
        activity = state["activity"]
    if state.get("location"):
        location = state["location"]

    return {
        "current_time": current_time,
        "activity": activity,
        "location": location,
        "attire": attire,
        "mood": mood_label,
        "user_request": user_request,
    }


def _generate_spec(ctx: dict, settings, llm) -> dict:
    char = settings.character
    prompt = SELFIE_SPEC_PROMPT.format(
        display_name=char.get("display_name") or char.get("name"),
        person_noun=char.get("person_noun") or "角色",
        **ctx,
    )
    max_tokens = int(settings.get("selfie.spec_max_tokens", 800))
    max_attempts = int(settings.get("selfie.spec_retry_attempts", 2)) + 1

    last: ConfigError | Exception | None = None
    for _ in range(max_attempts):
        try:
            raw = llm.chat(
                [ChatMessage(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=max_tokens,
                json_mode=True,
            )
            return json.loads(raw)
        except (json.JSONDecodeError, ConfigError) as exc:
            last = exc
    raise ConfigError(f"自拍 spec JSON 无法生成：{last}")


def main() -> int:
    parser = argparse.ArgumentParser(description="即时自拍")
    parser.add_argument("--settings", default=None)
    parser.add_argument("--daily", required=True)
    parser.add_argument("--user-request", default="发张自拍")
    args = parser.parse_args()

    try:
        settings = load_settings(args.settings)
    except ConfigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}), ensure_ascii=False)
        return 2

    daily_path = Path(args.daily)
    if not daily_path.is_file():
        print(json.dumps({"success": False, "error": f"daily 文件不存在：{daily_path}"}), ensure_ascii=False)
        return 1

    try:
        import json as _json
        daily = _json.loads(daily_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"success": False, "error": f"daily 解析失败：{exc}"}), ensure_ascii=False)
        return 1

    tz = settings.get("runtime.timezone", "Asia/Shanghai")
    char = settings.character

    try:
        ctx = _current_context(daily, args.user_request, tz)
        llm = create_llm_provider(settings, which="spec_provider")
        spec = _generate_spec(ctx, settings, llm)

        mock_segment = {"activity": ctx["activity"], "attire": ctx["attire"]}
        image_prompt = build_prompt(spec, mock_segment, char)

        image = create_image_provider(settings)
        stamp = datetime.now(ZoneInfo(tz)).strftime("%Y%m%d-%H%M%S")
        out_name = settings.get("selfie.output.name_template", "{time}")
        out_stem = out_name.format(time=stamp)
        result = image.generate(image_prompt, Path(out_stem))

    except ConfigError as exc:
        print(json.dumps({"success": False, "error": str(exc)}), ensure_ascii=False)
        return 2
    except Exception as exc:  # 生成链路偶发错误，不暴露堆栈
        print(json.dumps({"success": False, "error": f"生成失败：{type(exc).__name__}"}), ensure_ascii=False)
        return 2

    print(json.dumps({
        "success": True,
        "image_path": result.path,
        "image_sha256": result.sha256,
        "image_size_bytes": result.size_bytes,
        "image_mime": result.mime_type,
        "caption": spec.get("caption", ""),
        "spec": spec,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
