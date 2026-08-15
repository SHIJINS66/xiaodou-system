#!/usr/bin/env python3
"""providers 抽象基类与工厂。

替代旧系统里脚本直连 Seedream / DeepSeek 的写法：
    step03/bin/providers/seedream_client.py  (OpenAI SDK → Seedream)
    step03/bin/providers/deepseek_client.py  (urllib → DeepSeek)
    on_demand_selfie.py 里 import 这两个模块

框架化后，脚本只依赖这里定义的两个接口，具体供应商由 settings.yaml 的
`selfie.image_provider.type` / `spec_provider.type` 决定。

设计约束：
    - 不 import /opt/xiaodou。
    - 接口保持最小，让已有逻辑迁入时改动最小。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.settings_loader import ConfigError


# =============================================================================
# 交付/发送接口（Telegram 等）
# =============================================================================

class DeliveryError(Exception):
    """投递失败；ambiguous=True 表示结果不明需人工确认。"""
    def __init__(self, message: str, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class DeliveryProvider(ABC):
    """把最终文案（+可选图）投递到目标 channel。"""

    @abstractmethod
    def send(self, text: str, image: Path | None) -> dict[str, Any]:
        """返回 {message_id: str|None}；失败抛 DeliveryError。"""


# =============================================================================
# 图像生成接口
# =============================================================================

@dataclass
class ImageResult:
    """一张生成图的结果（对齐旧 seedream_client.generate 的返回）。"""
    path: str
    sha256: str
    size_bytes: int
    mime_type: str
    image_format: str
    usage: str | None = None


class ImageProvider(ABC):
    """图像生成接口。"""

    @abstractmethod
    def generate(self, prompt: str, output_path: Path) -> ImageResult:
        """生成一张图并写到 output_path（无扩展名，由 provider 决定后缀）。"""


# =============================================================================
# 文本/文案生成接口（对话补全）
# =============================================================================

@dataclass
class ChatMessage:
    role: str
    content: str


class LLMProvider(ABC):
    """对话补全接口。"""

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """返回模型文本输出；json_mode=True 时要求返回合法 JSON 字符串。"""


# =============================================================================
# 工厂：按 settings 里的 type 选择实现
# =============================================================================

_IMAGE_REGISTRY: dict[str, type[ImageProvider]] = {}
_LLM_REGISTRY: dict[str, type[LLMProvider]] = {}
_DELIVERY_REGISTRY: dict[str, type[DeliveryProvider]] = {}


_ACTIVE_DELIVERY = None


def register_image(name: str) -> Any:
    def deco(cls: type[ImageProvider]) -> type[ImageProvider]:
        _IMAGE_REGISTRY[name] = cls
        return cls
    return deco


def register_llm(name: str) -> Any:
    def deco(cls: type[LLMProvider]) -> type[LLMProvider]:
        _LLM_REGISTRY[name] = cls
        return cls
    return deco


def create_image_provider(settings: Any) -> ImageProvider:
    """按 settings.selfie.image_provider.type 实例化图像 provider。"""
    import importlib

    # 尝试注册内置实现（seedream 等）。懒导入，避免循环依赖。
    try:
        importlib.import_module("providers.image.seedream")
    except ImportError:
        pass

    conf = settings.get("selfie.image_provider") or {}
    ptype = conf.get("type", "seedream")
    cls = _IMAGE_REGISTRY.get(ptype)
    if cls is None:
        raise ConfigError(f"未知图像 provider 类型：{ptype!r}（可选：{sorted(_IMAGE_REGISTRY)}）")
    return cls(settings)


def create_llm_provider(settings: Any, *, which: str = "spec_provider") -> LLMProvider:
    """按 settings 下的某个 provider 块实例化文本 provider。

    which: 'spec_provider'（自拍 spec 生成）或任意 models 下的映射。
    """
    import importlib

    # 尝试注册内置实现。懒导入，避免循环依赖。
    for _mod in ("providers.llm.openai_compatible", "providers.llm.deepseek_urllib"):
        try:
            importlib.import_module(_mod)
        except ImportError:
            pass

    # which 可能指向：selfie.<which>（自拍 spec）、models.<which>（对话）、或顶层 <which>
    conf = (
        settings.get(f"selfie.{which}")
        or settings.get(f"models.{which}")
        or settings.get(which)
        or {}
    )
    ptype = conf.get("type") or conf.get("provider") or "openai_compatible"
    cls = _LLM_REGISTRY.get(ptype)
    if cls is None:
        raise ConfigError(f"未知 LLM provider 类型：{ptype!r}（可选：{sorted(_LLM_REGISTRY)}）")
    return cls(settings, conf)


def register_delivery(name: str) -> Any:
    def deco(cls: type[DeliveryProvider]) -> type[DeliveryProvider]:
        _DELIVERY_REGISTRY[name] = cls
        return cls
    return deco


def set_active_delivery(provider: DeliveryProvider | None) -> None:
    """测试/适配用注入点：覆盖实际投递实现（如 stub）。置 None 回到配置解析。"""
    global _ACTIVE_DELIVERY
    _ACTIVE_DELIVERY = provider


def create_delivery_provider(settings: Any) -> DeliveryProvider:
    """按 settings.delivery.type 实例化投递 provider；未配置或未注册时抛错。"""
    if _ACTIVE_DELIVERY is not None:
        return _ACTIVE_DELIVERY
    # 懒导入注册内置投递实现（telegram 等），避免循环依赖。
    try:
        import providers.telegram  # noqa: F401  # @register_delivery 注册副作用
    except ImportError:
        pass
    conf = settings.get("delivery") or {}
    # 兼容嵌套 channel 布局：delivery.channel.type / delivery.channel.options
    channel = conf.get("channel")
    if isinstance(channel, dict):
        ptype = channel.get("type") or conf.get("type") or conf.get("provider") or ""
    else:
        ptype = conf.get("type") or conf.get("provider") or ""
    cls = _DELIVERY_REGISTRY.get(ptype)
    if cls is None:
        raise ConfigError(f"未配置或未知投递 provider 类型：{ptype!r}（可选：{sorted(_DELIVERY_REGISTRY)}）")
    return cls(settings, conf)
