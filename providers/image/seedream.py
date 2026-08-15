#!/usr/bin/env python3
"""Seedream 图像 provider（从旧 step03/bin/providers/seedream_client.py 迁入）。

改动点（对应 HARDCODE_AUDIT §5）：
1. 路径不再写死，从 settings 读 `character.appearance.reference_dir`、
   `runtime.dirs.selfies`。
2. 性别/人称不再写死「女生/她」，改读 `character.pronoun_*/person_noun`。
3. base_url / model / size 从 `selfie.image_provider.options` 读。
4. api_key 从 `options.api_key_env` 声明的环境变量读（不再硬编码 ARK_API_KEY 名，
   但其实仍指向 ARK_API_KEY，只是名字可配）。
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from providers.base import ImageResult, ImageProvider, register_image
from scripts.settings_loader import ConfigError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_PREFIX = b"\xff\xd8\xff"
WEBP_RIFF = b"RIFF"
WEBP_SIGNATURE = b"WEBP"

_SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reference_uris(settings: Any) -> list[str]:
    """从 settings 读参考图清单，返回 data URI。</br>旧系统从 step03.json 的
    reference_image_root + reference_images 读。"""
    root = Path(settings.get("character.appearance.reference_dir", "assets/character"))
    if not root.is_absolute():
        root = settings.root_dir / root
    root = root.resolve()

    allowlist = settings.get("character.appearance.reference_images")
    if not allowlist:
        raise ConfigError("未配置 character.appearance.reference_images 参考图清单")
    result: list[str] = []
    for item in allowlist:
        fname = item["filename"]
        path = (root / fname)
        if not path.is_file():
            raise ConfigError(f"参考图缺失：{path}")
        if _sha256(path.read_bytes()) != item["sha256"]:
            raise ConfigError(f"参考图哈希不符：{fname}")
        result.append("data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii"))
    return result


def _detect_format(payload: bytes) -> tuple[str, str, str]:
    """返回 (format_name, extension, mime_type)。"""
    if payload.startswith(PNG_SIGNATURE):
        return "png", ".png", "image/png"
    if payload.startswith(JPEG_PREFIX):
        return "jpeg", ".jpg", "image/jpeg"
    if payload.startswith(WEBP_RIFF) and payload[8:12] == WEBP_SIGNATURE:
        declared = int.from_bytes(payload[4:8], "little") + 8
        if declared > len(payload):
            raise ConfigError("Seedream 返回截断的 WebP 数据")
        return "webp", ".webp", "image/webp"
    raise ConfigError("Seedream 返回了不支持的图片格式")


def _build_prompt(selfie_spec: dict[str, Any], segment: dict[str, Any], character: dict[str, Any]) -> str:
    """生图 prompt（通用化：性别/人称从 character 读，不再写死「女生/她」）。"""
    framing = selfie_spec["framing"]
    pronoun = character.get("pronoun_object") or character.get("pronoun_subject") or "她"
    person = character.get("person_noun") or "角色"

    if framing == "mirror_selfie":
        camera_desc = f"对镜自拍视角：{pronoun}面对全身镜，镜中完整映出{pronoun}的全身和面容，{pronoun}的一只手举着手机与镜面成角度，手臂弯曲的轮廓在镜中可见。"
    elif framing == "full_body":
        camera_desc = f"俯拍自拍视角：从{pronoun}手臂向上举起的略高处向下俯视，{pronoun}全身呈现在画面下方，脸微微仰起望向上方。画面中只看到{pronoun}的身体，看不到举着设备的手。"
    elif framing == "half_body":
        camera_desc = f"前置自拍视角：从{pronoun}面前很近的距离看向{pronoun}的半身，略微俯角，如同{pronoun}抬起手臂正对自己。{pronoun}的视线自然平视前方。画面中只看到{pronoun}的脸和上半身，看不到手臂和手机。"
    else:
        camera_desc = f"前置自拍视角：从很近的距离正对{pronoun}的脸和肩膀，略微俯角。{pronoun}的视线平视或微向上看向前方。画面中只看到{pronoun}的面部和肩部，看不到手臂和手机。"

    hard_negative = (
        "第三人称视角，他人拍摄，他拍，旁观者视角，远处拍摄，"
        f"画面中出现另一个拿着相机或手机的人，画面中出现手机或相机设备本身，"
        "手机屏幕界面，自拍UI界面，"
        f"画面边缘出现不属于{pronoun}的手臂或手，"
        "从背后拍摄，从侧面远处拍摄，透过窗户或门缝偷看，"
        f"画面中包含举着手机自拍的手臂，手机边框，自拍杆"
    )
    existing = list(selfie_spec.get("negative_constraints", []))
    if hard_negative not in "、".join(existing):
        existing.append(hard_negative)
    negative = "、".join(existing) or "无"

    return (
        "保持参考图角色的脸型、年龄感、发色、发长、五官和整体气质一致，只改变本次场景、服装和动作。"
        f"{camera_desc}"
        f"画面中只出现这一个{person}，没有第二个人。"
        f"构图：{framing}；地点：{selfie_spec['location']}；"
        f"服装：{selfie_spec['outfit']}；动作：{selfie_spec['pose']}；表情：{selfie_spec['expression']}；"
        f"光线：{selfie_spec['lighting']}；背景：{selfie_spec['background']}；"
        f"当前活动：{segment['activity']}；当前着装：{segment['attire']}；负面限制：{negative}。"
    )


@register_image("seedream")
class SeedreamProvider(ImageProvider):
    def __init__(self, settings: Any):
        self.settings = settings
        conf = settings.get("selfie.image_provider.options") or {}
        self.base_url = conf.get("base_url")
        self.model = conf.get("model", "doubao-seedream-5-0-260128")
        self.size = conf.get("size", "4K")
        self.timeout = int(conf.get("timeout_seconds", 120))
        self.api_key_env = conf.get("api_key_env", "ARK_API_KEY")
        self.max_bytes = int(conf.get("max_image_bytes", 25 * 1024 * 1024))

    def generate(self, prompt: str, output_path: Path) -> ImageResult:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise ConfigError(f"{self.api_key_env} 未配置")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigError("缺少 openai 依赖；请运行 pip install -r requirements.txt") from exc

        references = _reference_uris(self.settings)
        client = OpenAI(base_url=self.base_url, api_key=api_key, timeout=self.timeout)
        resp = client.images.generate(
            model=self.model,
            prompt=prompt,
            size=self.size,
            response_format="b64_json",
            extra_body={
                "image": references,
                "watermark": False,
                "sequential_image_generation": "disabled",
            },
        )
        if not resp.data or not resp.data[0].b64_json:
            raise ConfigError("Seedream 未返回图片")
        payload = base64.b64decode(resp.data[0].b64_json, validate=True)
        if len(payload) > self.max_bytes:
            raise ConfigError("Seedream 输出超过尺寸上限")

        fmt, ext, mime = _detect_format(payload)
        destination = self._resolve_dest(output_path, ext)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

        return ImageResult(
            path=str(destination),
            sha256=_sha256(payload),
            size_bytes=len(payload),
            mime_type=mime,
            image_format=fmt,
            usage=str(getattr(resp, "usage", None)) if getattr(resp, "usage", None) else None,
        )

    def _resolve_dest(self, output_path: Path, ext: str) -> Path:
        requested = output_path
        suffix = requested.suffix.lower()
        if suffix in _SUPPORTED_SUFFIXES:
            requested = requested.with_suffix("")
        elif suffix:
            raise ConfigError("输出路径必须是文件名 stem 或支持的图片后缀")
        out_root = Path(self.settings.get("runtime.dirs.selfies", "daily_selfies"))
        if not out_root.is_absolute():
            out_root = self.settings.root_dir / out_root
        dest = requested.with_suffix(ext)
        if dest.is_absolute():
            return dest
        return out_root / dest


# 保留旧函数签名，方便迁移期调用（后续可移除）
def build_prompt(selfie_spec: dict[str, Any], segment: dict[str, Any], character: dict[str, Any]) -> str:
    return _build_prompt(selfie_spec, segment, character)
