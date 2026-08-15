#!/usr/bin/env python3
"""Telegram 投递 provider —— 用 bot_token + chat_id 直接调 Telegram Bot API。

等价迁移说明：
    线上 /opt/xiaodou/step03 通过 `openclaw message send --channel telegram` 发送（依赖
    OpenClaw 内置 Telegram 账号）。framework 为了让框架在独立实例里也能发 Telegram，
    提供更简单的路径：直接用 settings.delivery 里的 bot_token_env / chat_id_env 指向的
    .env 凭据，走 api.telegram.org 的 sendMessage / sendPhoto（urllib 默认读取
    HTTPS_PROXY/HTTP_PROXY，可穿透国内代理）。

    设计对齐 providers/base.DeliveryProvider 抽象： send(text, image) -> {message_id}。
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from providers.base import DeliveryProvider, DeliveryError, register_delivery


def _env_value(conf: dict[str, Any], key: str, default: str | None = None) -> str:
    env_name = conf.get(key) or default
    if not env_name:
        raise DeliveryError(f"{key} 未配置", ambiguous=False)
    value = os.environ.get(env_name, "")
    if not value:
        raise DeliveryError(f"环境变量 {env_name} 未设置（{key}）", ambiguous=False)
    return value


def _message_id(parsed: Any) -> str | None:
    if isinstance(parsed, dict):
        for key in ("message_id", "messageId"):
            found = parsed.get(key)
            if isinstance(found, (str, int)) and str(found):
                return str(found)
        for child in parsed.values():
            found = _message_id(child)
            if found:
                return found
    elif isinstance(parsed, list):
        for child in parsed:
            found = _message_id(child)
            if found:
                return found
    return None


@register_delivery("telegram")
class TelegramDeliveryProvider(DeliveryProvider):
    """经 api.telegram.org 发送文字/图片到指定 chat。

    settings.delivery 支持两种形态（二选一）：
        delivery:
          type: telegram
          bot_token_env: TELEGRAM_BOT_TOKEN
          chat_id_env: TELEGRAM_CHAT_ID
    或（nested，与 settings.example.yaml / guided_setup 写的一致）：
        delivery:
          channel:
            type: telegram
            options:
              bot_token_env: TELEGRAM_BOT_TOKEN
              chat_id_env: TELEGRAM_CHAT_ID
    """

    def __init__(self, settings: Any, conf: dict[str, Any]):
        self.settings = settings
        # 兼容扁平 / channel.options 两种布局
        opts: dict[str, Any] = dict(conf or {})
        channel = opts.pop("channel", None)
        if isinstance(channel, dict):
            opts.setdefault("type", channel.get("type") or opts.get("type"))
            sub = channel.get("options") or {}
            for k, v in sub.items():
                opts.setdefault(k, v)
        self.conf = opts
        self.timeout = int(opts.get("timeout_seconds", 60))
        self.env = self._resolve_env()

    def _resolve_env(self) -> dict[str, str]:
        token = _env_value(self.conf, "bot_token_env", "TELEGRAM_BOT_TOKEN")
        chat_id = _env_value(self.conf, "chat_id_env", "TELEGRAM_CHAT_ID")
        return {"token": token, "chat_id": chat_id}

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.env['token']}/{method}"

    def _post(self, method: str, fields: dict[str, Any], files: dict[str, Any] | None = None) -> Any:
        import urllib.parse as up
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(self._api_url(method), data=data, method="POST")
        if files:
            # multipart 上传（图片）
            boundary = "----XiaodouBinary" + os.urandom(8).hex()
            body = b""
            for fname, fpath in files.items():
                raw = fpath.read_bytes()
                body += (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{fname}"; filename="{fpath.name}"\r\n'
                    f"Content-Type: image/png\r\n\r\n"
                ).encode("utf-8") + raw + b"\r\n"
            body += f"--{boundary}--\r\n".encode("utf-8")
            req = urllib.request.Request(
                self._api_url(method), data=body, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise DeliveryError(
                f"Telegram {method} HTTP {exc.code}: {detail}", ambiguous=True
            ) from exc
        except urllib.error.URLError as exc:
            raise DeliveryError(f"Telegram {method} 网络错误: {exc}", ambiguous=True) from exc
        try:
            return json.loads(resp.read().decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DeliveryError(f"Telegram {method} 返回无效 JSON", ambiguous=True) from exc

    def send(self, text: str, image: Path | None = None) -> dict[str, Any]:
        if image is not None:
            parsed = self._post(
                "sendPhoto",
                {"chat_id": self.env["chat_id"], "caption": text},
                files={"photo": image},
            )
        else:
            parsed = self._post(
                "sendMessage",
                {"chat_id": self.env["chat_id"], "text": text},
            )
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise DeliveryError(
                f"Telegram 返回失败: {json.dumps(parsed, ensure_ascii=False)[:300]}", ambiguous=True
            )
        mid = _message_id(parsed.get("result"))
        return {"message_id": str(mid) if mid else None, "response": parsed}
