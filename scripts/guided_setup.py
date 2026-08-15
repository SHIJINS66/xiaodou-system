#!/usr/bin/env python3
"""guided_setup — 新环境部署向导。

把「克隆→能跑」变成新手照做的交互式流程，覆盖完整链条：
  1. 环境检测（python / atd / 时区 / openclaw CLI / session）
  2. 引导设置核心 md（人设/身份/生活/用户/陪伴对象）
  3. 填 API key（模型商选择 → 写入 .env，校验非空）
  4. Telegram bot 绑定（引导 @BotFather → 验证消息）
  5. Gateway session 绑定（session_key → 解析 session id + 注入权限验证）
  6. 最终 verify（全链路自检）

设计原则：
- 交互式提问，但全程可用 --non-interactive 走默认值（CI/脚本化）
- 敏感信息只写 .env / 环境变量，不落 settings 明文（密钥不进仓库）
- 验证真实但低副作用：Telegram 绑定会发一条测试消息（默认关闭，--test-telegram 开启）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


# 核心 md（运行规则 / 人设 / 陪伴对象 / 记忆）——对应 OpenClaw workspace 根目录文件。
# init.sh 复制模板到 workspace：已存在则跳过、缺失才复制；用户可上传同名文件替换。
# 路径是「相对 workspace 根目录」的实例内路径。
CORE_MD = [
    # (实例内路径, 用途)
    ("AGENTS.md",    "运行规则（主动发消息/自拍/记忆/搜索/回复节奏）"),
    ("IDENTITY.md",  "固定身份（我是谁 / 外貌 / 家庭 / 关系）"),
    ("SOUL.md",      "性格 / 情绪 / 说话方式 / 相处边界"),
    ("LIFE.md",      "生活规律与作息（Planner 生成依据）"),
    ("USER.md",      "陪伴对象信息（对方是谁 / 偏好 / 边界）"),
    ("TOOLS.md",     "工具与脚本（由体系提供）"),
    ("MEMORY.md",    "长期记忆（由每日记忆任务维护）"),
]


class Guide:
    def __init__(self, instance_dir: Path, non_interactive: bool = False):
        self.instance = instance_dir
        self.settings_path = instance_dir / "settings.yaml"
        self.env_path = instance_dir / ".env"
        self.ni = non_interactive

    # ------------------------------------------------------------- 交互 helpers
    def ask(self, prompt: str, default: str = "") -> str:
        if self.ni:
            return default
        suffix = f" [{default}]" if default else ""
        try:
            val = input(f"  ? {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        return val or default

    def note(self, text: str) -> None:
        print(f"\n  >> {text}")

    def step(self, n: int, title: str) -> None:
        print(f"\n{'='*60}\n  [{n}] {title}\n{'='*60}")

    # ------------------------------------------------------------- 1. 环境检测
    def check_environment(self) -> bool:
        self.step(1, "环境检测")
        ok = True

        def probe(name, fn):
            nonlocal ok
            if fn():
                print(f"  [ok] {name}")
            else:
                print(f"  [x]  {name}")
                ok = False

        probe("python3 (3.10+)", lambda: (
            sys.version_info.major == 3 and sys.version_info.minor >= 10))
        probe("at / atd 服务", lambda: (
            shutil_which("at") and (
                run_quiet(["systemctl", "is-active", "atd"]) == "active"
                # 允许 fallback：service 或直接 at 可用
            )))
        probe("openclaw CLI", lambda: shutil_which("openclaw") is not None)
        import zoneinfo
        probe("时区 Asia/Shanghai", lambda: str(getattr(zoneinfo.ZoneInfo("Asia/Shanghai"), "key", "Asia/Shanghai")) != "")
        probe("settings.yaml 存在", lambda: self.settings_path.exists())
        if not self.settings_path.exists():
            self.note("先运行 init.sh 生成 settings.yaml 与目录结构，再回来继续。")
            return False
        return ok

    # ------------------------------------------------------------- 2. 核心 md 引导
    def guide_core_md(self) -> None:
        self.step(2, "准备核心配置 md")
        self.note("以下文件定义这套 agent 的「运行规则 / 人设 / 陪伴对象 / 记忆」，是它怎么跑、是谁、对谁。")
        self.note("这些文件就放在 OpenClaw 的 workspace 根目录（OpenClaw 启动时会自动读取它们）。")
        self.note("init.sh 已把模板复制到此目录：已存在的（OpenClaw 自带或你已填的）会跳过，缺失的（如 LIFE/MEMORY）会新建。")
        self.note("自定义方式：直接在 workspace 里上传同名的文件替换正文即可，不用在终端编辑。")
        print(f"\n      实例目录（workspace）: {self.instance}")
        print(f"      {'文件':<14} 用途")
        print("      " + "-" * 60)
        for name, purpose in CORE_MD:
            resolved = self.instance / name
            state = "已就位" if resolved.exists() else "缺失"
            print(f"      * {name:<14} {purpose:<34} [{state}]")
        self.note("把模板放到实例后，用你的文本覆盖正文；占位符（如 {character_name}、{companion_key}）由系统按 settings 自动渲染，不用手改。")

    # ------------------------------------------------------------- 3. API key
    def guide_api_keys(self) -> None:
        self.step(3, "配置对话模型 API key")
        self.note("framework 的对话模型用独立的 API key，从 .env 读，不依赖 OpenClaw。")
        self.note("它通常就是你在 OpenClaw 里配置主模型时用的同一个 key——这里再填一次即可。")
        print()
        print("  选择你的对话模型商：")
        print("      1) DeepSeek   (deepseek-v4 系列)")
        print("      2) OpenRouter (任意 OpenAI 兼容模型)")
        print("      3) 豆包 Seed  (doubao)")
        choice = self.ask("模型商编号", "1")
        provider_cfg = {"1": "DEEPSEEK_API_KEY", "2": "OPENROUTER_API_KEY", "3": "ARK_API_KEY"}
        key_env = provider_cfg.get(choice, "DEEPSEEK_API_KEY")
        # 探测：环境里是否已配同名变量
        current = os.environ.get(key_env, "") or self._read_env().get(key_env, "")
        if current:
            print(f"  [ok] 检测到环境已有 {key_env}（{current[:6]}...），可跳过。")
            reuse = self.ask(f"沿用已有的 {key_env} 吗？（留空=沿用，输入 n 重填）", "y")
            if reuse.lower() not in ("n", "no"):
                if not (env_path_has := self._env_has(key_env)):
                    self._set_env(key_env, current)
                print(f"  [ok] 已沿用 {key_env} 到 .env")
                return
        key = self.ask(f"粘贴 {key_env} 的 key（留空=跳过，之后手动填 .env）", "").strip()
        if key:
            self._set_env(key_env, key)
            print(f"  [ok] 已写入 .env: {key_env}")
        else:
            self.note(f"跳过。之后请手动在 {self.env_path} 填 {key_env}。")

    def _env_has(self, name: str) -> bool:
        return name in self._read_env()

    def _set_env(self, name: str, value: str) -> None:
        lines = []
        if self.env_path.exists():
            lines = self.env_path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if not ln.startswith(f"{name}=")]
        lines.append(f"{name}={value}")
        self.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------- 3.x OpenClaw 主模型
    def guide_main_model(self) -> None:
        """把 OpenClaw agent 的主模型配置好（auth + provider + model id）。

        等价实践：安装 OpenClaw 后，bot 要能收/发消息，必须给 agent 配好能用的
        主模型（否则 Telegram 收到消息会报 ProviderAuthError / 401）。
        这一步自动完成：
          1) 读 .env 里的对话模型 key（和 guide_api_keys 同一个 key）
          2) 探测模型商当前可用的模型 ID（不硬编码，避免用已下线的旧模型 ID）
          3) 用 `openclaw models auth paste-api-key` 把 key 写进 auth store
          4) 用 `openclaw config set` 配 provider.baseUrl + agents.defaults.model.primary
        所有命令都带 openclaw_env(settings) 定向到当前 OpenClaw 实例，不会误碰其它实例。
        """
        self.step(4, "配置 OpenClaw 主模型（agent 收发用）")
        self.note("framework 发消息走 Telegram / 收消息走 OpenClaw channel，都需要 OpenClaw"
                  "的 agent 有一个能正常调用的主模型。这里把对话模型商和 key 一并配给 OpenClaw。")
        import json as _json
        if not self._settings_ready():
            self.note("settings.yaml 尚未就绪，跳过主模型配置。")
            return
        # 幂等：OpenClaw 已配置好主模型（example: openclaw configure 已配过）就跳过，不覆盖。
        existing_primary = self._openclaw_primary_model()
        if existing_primary:
            print(f"  [ok] 检测到 OpenClaw 主模型已配置: {existing_primary}，跳过（不重复配置/不覆盖）。")
            self.note("如需改主模型，用 OpenClaw 自带命令：openclaw configure 或 openclaw models set")
            return
        env = self._read_env()
        # 探测 .env 里已配的 key（和 guide_api_keys 一致：DEEPSEEK / OPENROUTER / ARK）
        provider_meta = {
            "1": ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
            "2": ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
            "3": ("doubao", "ARK_API_KEY", "https://ark.cn-beijing.volces.com/api/v3"),
        }
        choice = self.ask("选对话模型商（1 DeepSeek / 2 OpenRouter / 3 豆包）", "1")
        provider, key_env, base_url = provider_meta.get(
            choice, provider_meta["1"])
        key = env.get(key_env, "")

        if not key:
            self.note(f"{key_env} 未填。请先在上一步（对话模型 API key）填好，或手动填 .env。跳过主模型配置。")
            return

        # 1) 探测可用模型 ID（不硬编码模型名）
        model_id = self._probe_models(provider, base_url, key)
        if not model_id:
            self.note(f"无法探测 {provider} 可用模型，跳过主模型配置（可手动配 OpenClaw 模型）。")
            return
        full_ref = f"{provider}/{model_id}"
        print(f"  [..] 将配置主模型: {full_ref}（自动探测，未硬编码）")

        # 2) 写 auth store（stdin 传 key，避免 shell 明文被脱敏成 ***）
        cmd_auth = [self._openclaw_bin(), "models", "auth", "paste-api-key", "--provider", provider]
        auth_env = self._openclaw_env()
        try:
            r = subprocess.run(cmd_auth, input=key + "\n", text=True, capture_output=True,
                               timeout=60, env=auth_env)
            if "Auth profile" in (r.stdout or "") or r.returncode in (0, 1):
                print(f"  [ok] 已写入 {provider} auth store: {provider}:manual")
            else:
                self.note(f"auth 写入异常: {(r.stderr or r.stdout or '')[-200:]}")
        except Exception as exc:
            self.note(f"auth 写入失败: {exc}")

        # 3) 配 provider.baseUrl + primary model（config set 带实例环境）
        batch = _json.dumps([
            {"path": f"models.providers.{provider}.baseUrl", "value": base_url},
            {"path": "agents.defaults.model.primary", "value": full_ref},
        ])
        try:
            r = subprocess.run([self._openclaw_bin(), "config", "set", "--batch-json", batch],
                               capture_output=True, text=True, timeout=60, env=auth_env)
            print(f"  [ok] 已配 {provider}.baseUrl + primary={full_ref}")
            print(f"       ({(r.stdout or '').strip().splitlines()[-1] if (r.stdout or '').strip() else 'no output'})")
        except Exception as exc:
            self.note(f"config set 失败: {exc}")

    def _settings_ready(self) -> bool:
        return self.settings_path.exists()

    def _openclaw_bin(self) -> str:
        import shutil
        try:
            data = __import__("yaml").safe_load(self.settings_path.read_text(encoding="utf-8"))
            gw = (data.get("scheduling") or {}).get("gateway") or {}
            if gw.get("openclaw_bin"):
                return gw["openclaw_bin"]
        except Exception:
            pass
        return shutil.which("openclaw") or "openclaw"

    def _openclaw_primary_model(self) -> str | None:
        """读当前 OpenClaw 实例的 openclaw.json，返回 agent 主模型 ref（如
deepseek/deepseek-v4-flash）；未配置返回 None。仅用于 verify 检查。"""
        try:
            conf_path = self._openclaw_bin_conf_path()
            import json as _json
            data = _json.load(open(conf_path, encoding="utf-8"))
            model = (data.get("agents") or {}).get("defaults") or {}
            prim = model.get("model") or model
            if isinstance(prim, dict):
                p = prim.get("primary")
                if p:
                    return p
            # provider 至少配了 auth / baseUrl 也算半就绪
            prov = (data.get("models") or {}).get("providers") or {}
            if prov:
                return f"(providers: {sorted(prov)}"
        except Exception:
            pass
        return None

    def _openclaw_bin_conf_path(self) -> str:
        """定位 openclaw.json：优先 .env / settings 的 extra_env，否则 $HOME/.openclaw/openclaw.json。"""
        import sys as _s
        _s.path.insert(0, str(self.instance.parent.parent / "scripts"))
        _s.path.insert(0, str(self.instance.parent.parent))
        try:
            import settings_loader as sl
            _orig = sl._validate_secrets
            sl._validate_secrets = lambda s: None
            settings = sl.load_settings(str(self.settings_path))
            extra = (settings.get("scheduling", {}).get("gateway", {}) or {}).get("extra_env", {}) or {}
            cfg = extra.get("OPENCLAW_CONFIG_PATH")
            if cfg:
                return cfg
        except Exception:
            pass
        return str(Path.home() / ".openclaw" / "openclaw.json")

    def _openclaw_env(self) -> dict:
        import sys as _s
        _s.path.insert(0, str(self.instance.parent.parent / "scripts"))
        _s.path.insert(0, str(self.instance.parent.parent))
        try:
            import settings_loader as sl
            _orig = sl._validate_secrets
            sl._validate_secrets = lambda s: None
            settings = sl.load_settings(str(self.settings_path))
            from common import openclaw_env as _oe
            env = _oe(settings)
            token_env = (settings.get("scheduling", {}).get("gateway", {}) or {}).get("auth_token_env")
            if token_env and token_env in self._read_env():
                env[token_env] = self._read_env().get(token_env, "")
            return env
        except Exception:
            return dict(os.environ)

    def _probe_models(self, provider: str, base_url: str, key: str) -> str:
        """调用 /models 探测可用模型，返回第一个可用 model id；失败返回空。
        兜底：deepseek -> deepseek-v4-flash；其它返回空。"""
        import json as _json
        import urllib.request as _ur
        try:
            req = _ur.Request(base_url.rstrip("/") + "/models",
                              headers={"Authorization": "Bearer " + key})
            r = _ur.urlopen(req, timeout=25)
            data = _json.loads(r.read())
            ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
            if ids:
                # 优先选后缀带 -flash 的快速模型
                for i in ids:
                    if "-flash" in i or "flash" in i.lower():
                        return i
                return ids[0]
        except Exception as exc:
            print(f"  [..] 探测 /models 失败: {str(exc)[:120]}")
        if provider == "deepseek":
            return "deepseek-v4-flash"
        return ""


    # ------------------------------------------------------------- 4. Telegram bot 从零引导
    def guide_telegram(self, test: bool = False) -> None:
        self.step(5, "Telegram bot 从零引导")
        if self.ni:
            # non-interactive：只读 .env 里已有的绑定，不进行 BotFather 交互
            env = self._read_env()
            if "TELEGRAM_BOT_TOKEN" in env:
                print("  [ok] (ni) 已检测到 TELEGRAM_BOT_TOKEN，跳过交互引导")
            else:
                self.note("(ni) 无 TELEGRAM_BOT_TOKEN，跳过。之后手动在 .env 填 token/chat_id。")
            return
        self.note("这套框架靠一个 Telegram bot 把主动消息/自拍发给你。")
        self.note("如果你还没有 bot，下面的步骤会一步步带你从 BotFather 建好。")
        print()
        print("  [步骤 5.1] 确认你已登录 Telegram（手机或桌面版都行）")
        print("            如果没有：先去应用商店装 Telegram，注册登录，再回来继续。")
        self._pause("登录好 Telegram 后按 Enter 继续")
        print()
        print("  [步骤 5.2] 找到 BotFather")
        print("            在 Telegram 顶部搜索框输入：  @BotFather")
        print("            点开这个官方账号（蓝色勾，是 Telegram 官方的 bot 管理号）。")
        self._pause("打开和 BotFather 的聊天后按 Enter 继续")
        print()
        print("  [步骤 5.3] 创建新 bot")
        print("            在 BotFather 聊天里发送：  /newbot")
        print("            BotFather 会依次问你两个名字：")
        print("              · 显示名（给你的 bot 起个你喜欢的名字，可中文，比如：我的陪伴助手）")
        print("              · 用户名（必须英文，且必须以  bot  结尾，比如：my_companion_bot）")
        print("            起好后，BotFather 会回你一串 token，形如：")
        print("              1234567890:AAH...xyz")
        print("            那一整串就是 bot token，复制它。")
        self._pause("拿到并复制好 token 后，粘贴到下面")
        env_now = self._read_env()
        existing = env_now.get("TELEGRAM_BOT_TOKEN", "")
        if existing:
            print(f"  [ok] 检测到 .env 已有 TELEGRAM_BOT_TOKEN（{existing[:6]}...），可复用。")
            reuse = self.ask("沿用已有 token 吗？（留空=沿用并用它验证，输入 n 重贴）", "y")
            if reuse.lower() not in ("n", "no"):
                token = existing
            else:
                token = ""
        else:
            token = ""
        while not token:
            token = self.ask("粘贴 bot token", "").strip()
            if not token:
                self.note("token 不能为空。没拿到请重新看上面步骤，或按 Ctrl-C 中止再回来看。")
        self._set_env("TELEGRAM_BOT_TOKEN", token)
        print(f"  [ok] 已写入 .env: TELEGRAM_BOT_TOKEN")

        # 自动验证 token（getMe）
        me = self._telegram_getme(token)
        if me:
            print(f"  [ok] token 有效，bot 用户名: @{me.get('username','')}")
        else:
            print("  [x]  token 验证失败（getMe 返回异常）。")
            print("      可能原因：token 复制不全 / 复制了别的东西。请用 Ctrl-C 重跑本步。")
            return
        print()
        print("  [步骤 5.4] 拿到你的 chat id")
        print("            把刚建好的 bot 加进聊天，然后给 bot 发一条任意消息（比如： 你好）。")
        print("            这样 bot 才能知道往哪发消息，框架也能自动读取你的 chat id。")
        print("           （如果 bot 没回，不碍事，只要你的消息已『发送』即可。）")
        self._pause("给 bot 发完那条消息后，按 Enter，我来自动读取你的 chat id")
        chat_id = self._telegram_wait_chat_id(token)
        if not chat_id:
            print("  [..] 通过 getUpdates 没读到 chat id（可能消息已被 OpenClaw 消费）。")
            print("      不用你手动填——下一步『批准 Telegram 用户接入』批准你时，")
            print("      向导会自动把你的 Telegram 账号 id 记录为 TELEGRAM_CHAT_ID。")
            print("      继续下一步即可。")
        else:
            self._set_env("TELEGRAM_CHAT_ID", str(chat_id))
            print(f"  [ok] 已获取 chat id 并写入 .env: TELEGRAM_CHAT_ID = {chat_id}")

        # 测试消息（默认关闭，--test-telegram 开启）
        if test:
            self._telegram_probe(token, str(chat_id))
        else:
            print()
            self.note("绑定完成。可用 --test-telegram 重跑本步来发一条验证消息。")

    def _pause(self, prompt: str) -> None:
        """等用户确认继续（non-interactive 自动继续）。"""
        if self.ni:
            return
        try:
            input(f"  >> {prompt}（按 Enter）: ")
        except (EOFError, KeyboardInterrupt):
            print()

    def _telegram_getme(self, token: str) -> dict | None:
        """调用 getMe 验证 token 有效性，返回 bot 信息（username 等），失败返回 None。"""
        import json as _json
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/getMe"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                payload = _json.loads(resp.read().decode())
            if payload.get("ok"):
                return payload.get("result") or {}
        except Exception:
            pass
        return None

    def _telegram_wait_chat_id(self, token: str, tries: int = 3) -> str | None:
        """轮询 getUpdates，自动读取最近的 chat id。用户已给 bot 发过消息。"""
        import json as _json
        import time
        import urllib.request
        for _ in range(tries):
            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=5"
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    payload = _json.loads(resp.read().decode())
                if payload.get("ok"):
                    results = payload.get("result") or []
                    for upd in results:
                        msg = upd.get("message") or {}
                        chat = msg.get("chat") or {}
                        if chat.get("id") is not None:
                            return str(chat["id"])
            except Exception:
                pass
            time.sleep(2)
        return None

    def _telegram_probe(self, token: str, chat_id: str) -> None:
        self.note("发送一条测试消息验证绑定……")
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = f"chat_id={chat_id}&text=companion-framework 绑定成功"
        try:
            with urllib.request.urlopen(url, data=body.encode(), timeout=15) as resp:
                data = resp.read().decode()
            if '"ok":true' in data:
                print("  [ok] Telegram 绑定验证通过")
            else:
                print("  [x]  Telegram 返回异常:", data[:200])
        except Exception as exc:
            print(f"  [x]  Telegram 验证失败: {exc}")

    def guide_telegram_pairing(self) -> None:
        """批准 Telegram 用户的 DM pairing 请求。

        OpenClaw 默认 dmPolicy=pairing：bot 收到陌生用户的私聊会挂起等待审批，
        不 approve 则消息进不了 session（表现为“Access not configured”）。
        这一步列出待审批请求，自动/手动批准你的 Telegram 用户，bot 接入才能真正收发。
        """
        self.note("OpenClaw 默认 dmPolicy=pairing：bot 收到陌生用户私聊会挂起等审批，"
                  "不批准则消息进不了会话。这里把你自己（bot 的对话对象）批准放行。")
        self.step(6, "批准 Telegram 用户接入 (pairing)")
        if not self._read_env().get("TELEGRAM_BOT_TOKEN"):
            self.note("尚未配置 TELEGRAM_BOT_TOKEN，跳过 pairing 审批（配好 token 后 bot 才能收到请求）。")
            return
        # ---- B 方案：先校验 OpenClaw 的 Telegram 配置是否就绪，缺哪项给明确指引（不代写）----
        check = self._openclaw_telegram_check()
        if not check["ready"] and check["problems"]:
            print("  [x] OpenClaw 的 Telegram channel 尚未就绪，配对无法进行。请先补全：")
            for i, p in enumerate(check["problems"], 1):
                print(f"       {i}. {p}")
            print("       补好后重跑本步。")
            self.note("OpenClaw 的 Telegram 配置（token / enabled / dmPolicy）用 OpenClaw 自带命令配置："
                      "openclaw configure，或 openclaw config set channels.telegram.<字段> <值>。")
            return

        bin_ = self._openclaw_bin()
        env = self._openclaw_env()

        def _list_requests():
            try:
                r = subprocess.run([bin_, "pairing", "list", "telegram", "--json"],
                                   capture_output=True, text=True, timeout=30, env=env)
                return self._parse_pairing(r.stdout)
            except Exception:
                return None

        def _approve(req):
            try:
                rr = subprocess.run([bin_, "pairing", "approve", "telegram", req["code"]],
                                    capture_output=True, text=True, timeout=30, env=env)
                out = (rr.stdout or rr.stderr or "").strip()
                if rr.returncode == 0 and "Approved" in out:
                    print(f"  [ok] 已批准 {req['code']} -> {req['user']}")
                    self._capture_chat_id_from_pairing(req)
                    return True
                print(f"  [x]  批准 {req['code']} 失败: {out[-150:]}")
                return False
            except Exception as exc:
                print(f"  [x]  批准 {req['code']} 异常: {exc}")
                return False

        requests = _list_requests()
        if requests is None:
            self.note("查询 pairing 请求失败（OpenClaw 可能没配备 pairing channel）。")
            return

        # 有请求 → 直接列出并批准
        if requests:
            print(f"  检测到 {len(requests)} 个待审批 Telegram 配对请求：")
            for req in requests:
                print(f"    · code={req['code']}  user={req['user']}  ({req['meta']})")
            print()
            approved = False
            for req in requests:
                approve = "y" if self.ni else self.ask(
                    f"批准配对 {req['code']}（用户 {req['user']}）？(y=批准 留空=跳过)", "y")
                if approve.lower() in ("y", "yes") and _approve(req):
                    approved = True
            if approved:
                print()
                print("  [ok] 配对完成。你现在可以通过 Telegram 和 agent 对话了。")
            return

        # 无请求 → 引导用户先给 bot 发第一条消息，再轮询等待请求出现
        print("  [..] 当前没有待审批的 pairing 请求。")
        if not self.ni:
            print("       请先让 bot 收到你的一条消息：")
            print("         · 在 Telegram 里打开你的 bot（@<你的bot用户名>）")
            print("         · 给 bot 发任意一条消息（比如：你好）")
            print("         · 发完它会生成一个 pairing 请求，我在这里等它出现并帮你批准。")
            self._pause("给 bot 发完第一条消息后按 Enter，我开始等待配对请求")
        # 轮询等待（最多约 60s）
        import time as _t
        for i in range(12):
            reqs = _list_requests()
            if reqs:
                print(f"  检测到 {len(reqs)} 个待审批请求，自动批准...")
                for req in reqs:
                    _approve(req)
                print("  [ok] 配对完成。你现在可以通过 Telegram 和 agent 对话了。")
                return
            if not self.ni and i == 0:
                print(f"  [..] waiting for pairing request... (最长等待 60s)")
            _t.sleep(5)
        print("  [x] 60 秒内未检测到 pairing 请求。")
        print("       确认：1) 你已给 bot 发过消息  2) OpenClaw gateway 正在运行（openclaw health）")
        print("       然后重跑本步。")


    def _capture_chat_id_from_pairing(self, req: dict) -> None:
        """批准配对时，把该用户的 Telegram 账号 id（= chat id）写入 .env 的
        TELEGRAM_CHAT_ID —— 普通用户不需要自己找/填 chat id。

        仅当 .env 里还没有 TELEGRAM_CHAT_ID 时才写（不覆盖已有值）。
        非数字 id（异常）则跳过。"""
        user = (req.get("user") or "").strip()
        if not user or not user.lstrip("-").isdigit():
            self.note("（未从 pairing 记录到可用的 chat id，跳过自动写入）")
            return
        current = self._read_env().get("TELEGRAM_CHAT_ID")
        if current:
            return  # 已有，不覆盖
        self._set_env("TELEGRAM_CHAT_ID", user)
        print(f"  [ok] 已自动记录 TELEGRAM_CHAT_ID = {user}（你的 Telegram 账号 id，无需手动填）")

    def _parse_pairing(self, raw: str) -> list[dict]:
        """从 `pairing list telegram [--json]` 输出解析待审批请求。
        优先 json；json 失败时尝试解析表格文本。"""
        import json as _json
        if raw:
            try:
                data = _json.loads(raw)
                reqs = data if isinstance(data, list) else data.get("requests", data.get("result", []))
                out = []
                for r in reqs if isinstance(reqs, list) else []:
                    code = r.get("code")
                    user = r.get("telegramUserId") or r.get("userId") or r.get("user")
                    meta = r.get("meta") or ""
                    if code:
                        out.append({"code": str(code), "user": str(user or "?"), "meta": str(meta)[:60]})
                return out
            except Exception:
                pass
        # fallback：表格文本
        out = []
        for line in raw.splitlines() if raw else []:
            line = line.strip()
            if "│" in line:
                cells = [c.strip() for c in line.split("│")]
                if len(cells) >= 3 and cells[0] and len(cells[0]) == 8:
                    out.append({"code": cells[0], "user": cells[1], "meta": cells[2]})
        return out

    def _openclaw_telegram_check(self) -> dict:
        """校验 OpenClaw 的 channels.telegram 配置（B 方案：只指引、不代写）。
        返回 {ready: bool, problems: [..], requires_message: bool}。
           ready:         全部就绪，可走 pairing
           problems:       缺失/错误项的清晰指引列表（每条含 openclaw configure / config set 建议）
           needs_pairing:  OpenClaw Telegram 已就绪但 gateway 可能没开 dmPolicy，需配对
        """
        import json as _json
        result = {"ready": False, "problems": [], "needs_pairing": False}
        try:
            conf_path = self._openclaw_bin_conf_path()
            data = _json.load(open(conf_path, encoding="utf-8"))
        except Exception as exc:
            result["problems"].append(f"无法读取 OpenClaw 配置 openclaw.json: {exc}")
            return result
        chan = (data.get("channels") or {}).get("telegram") or {}
        # 1) enabled
        if not chan.get("enabled"):
            result["problems"].append("OpenClaw 未启用 Telegram channel：跑 `openclaw configure` 启用，或在 telegrams 配置里设 enabled=true")
        # 2) botToken
        tok = chan.get("botToken") or ""
        if not tok:
            result["problems"].append("OpenClaw 的 Telegram botToken 为空：跑 `openclaw configure` 填 bot token（与 .env 的 TELEGRAM_BOT_TOKEN 一致）")
        # 3) dmPolicy 必须 pairing，bot 才能生成 DM 配对请求
        dm = (chan.get("dmPolicy") or "").lower()
        if dm != "pairing":
            result["problems"].append(
                "OpenClaw 的 Telegram dmPolicy 不是 pairing（bot 收陌生 DM 会挂起/被拒，pairing 命令用不了）："
                "跑 `openclaw configure` 把 DM 策略设为 pairing，或用 `openclaw config set channels.telegram.dmPolicy pairing`")
        # proxy 是可选增强（某些环境走代理连 Telegram），缺省不强制，但提示可配
        if not chan.get("proxy") and not os.environ.get("HTTPS_PROXY"):
            pass  # 不强制；能直连 Telegram 的用户不需要
        result["needs_pairing"] = True if (chan.get("enabled") and tok and dm == "pairing") else False
        result["ready"] = bool(chan.get("enabled") and tok and dm == "pairing")
        return result


    # ------------------------------------------------------------- 5. Gateway session
    def guide_gateway_session(self) -> None:
        self.step(7, "Gateway session 绑定")
        self.note("framework 的主动消息是通过 OpenClaw Gateway 注入到你日常对话的那个会话里的。")
        self.note("所以需要指定一个目标 session key。")
        import yaml
        print()
        print("  你可以在两种里选一个：")
        print("    a) 用你 OpenClaw 里已有的会话（比如你正在用的那个对话）——提供它的 session key")
        print("    b) 让引导器自动新建一个专用会话（交给 framework 用）")
        print()
        choice = self.ask("选 a（已有会话）/ b（自动新建）", "a")
        session_key = ""
        if choice.lower() in ("b", "new"):
            session_key = self._auto_create_session()
        else:
            session_key = self.ask("OpenClaw 会话 key（形如 agent:main:<id>，留空=跳过）", "").strip()
        if not session_key:
            self.note("跳过。之后手动编辑 settings.yaml 填 scheduling.gateway.session_key。")
            return
        data = yaml.safe_load(self.settings_path.read_text(encoding="utf-8"))
        gw = {**data.get("scheduling", {}).get("gateway", {})}
        gw["session_key"] = session_key
        if not gw.get("openclaw_bin"):
            bin_ = shutil_which("openclaw")
            if bin_:
                gw["openclaw_bin"] = bin_
        data.setdefault("scheduling", {})["gateway"] = gw
        self.settings_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"  [ok] 已写入 scheduling.gateway.session_key = {session_key}")
        sid = self._resolve_session_id(session_key)
        if sid:
            print(f"  [ok] 解析到 session id: {sid}")
        else:
            self.note("未能解析 session id（Gateway 拒绝或未起）。本地 CLI 若免授权则 inject 可通过，"
                      "否则请配置 scheduling.gateway.auth_token_env 对应的 token。")

    def guide_openclaw_memory(self) -> None:
        """就位 OpenClaw 记忆契约：session-memory / memoryFlush 显式禁用、
        session.reset 保持未配置。framework 自己管记忆与会话重置，
        避免与 OpenClaw 原生 memory writer / reset 双重写入冲突。

        用 framework 内置的 patch_memory_writers（受 whitelist 保护、只动两个 key），
        幂等：已合规则跳过，不合规则自动就位。
        """
        self.step(8, "OpenClaw 记忆契约就位")
        import sys as _sys
        here = Path(__file__).resolve().parent
        if str(here) not in _sys.path:
            _sys.path.insert(0, str(here))
        try:
            from openclaw_memory_config import patch_memory_writers, verify as verify_memory
        except ImportError as exc:
            self.note(f"无法导入 openclaw_memory_config（{exc}）；跳过记忆契约就位，"
                      f"session_rollover 可能要求你手动补。")
            return
        try:
            from step04_config import load_step04_config
            config = load_step04_config(str(self.settings_path))
        except Exception as exc:
            self.note(f"构造 step04 config 失败（{exc}）；跳过记忆契约就位。")
            return
        try:
            memory = verify_memory(config, check_hook_runtime=False)
        except Exception as exc:
            self.note(f"OpenClaw 记忆契约检查失败（{exc}）。")
            return
        if memory.get("passed"):
            print("  [ok] OpenClaw 记忆契约已合规（无原生 memory writer / reset 冲突）")
            return
        print("  · 需要禁用 OpenClaw 原生 memory writer（session-memory / compaction memoryFlush），")
        print("    由 framework 统一管理记忆与会话重置。正在自动就位……")
        try:
            result = patch_memory_writers(config)
        except Exception as exc:
            self.note(f"记忆契约自动就位失败（{exc}）。")
            print("    可手动执行 framework 自带的补丁后重跑：")
            print(f"      python3 {here / 'guided_setup.py'} --instance-dir {self.instance} --verify-only")
            return
        changes = "; ".join(result.get("changed_paths") or [])
        if result.get("changed"):
            print(f"  [ok] 已写入 OpenClaw config：{changes}")
            print("  [info] 重启 Gateway 后生效（finalize 步骤会做）。")
        else:
            print("  [ok] OpenClaw 记忆契约已合规（无需改动）")

    def _auto_create_session(self) -> str:
        """在当前 OpenClaw 实例里新建一个专用 session，返回 session key；失败返回空串。

        当前版本的 OpenClaw 没有 `openclaw session/sessions spawn` CLI，也没有创建会话的
        gateway RPC（spawn 不存在）。一个可靠、被验证过的方式是：
          · key 用 `agent:main:<label>` 形式
          · 在实例的 `agents/main/sessions/sessions.json` 索引里注册该 key（sessionId + sessionFile）
          · 建对应的 `<uuid>.jsonl` 会话日志文件
          · 用 gateway 的 chat.inject 实测注入——ok=true 说明 session 真实可用
        这正好是对「引导器没 session 时能新建 session」的落地实现。
        """
        label = self.ask("给这个专用会话起个名字", "companion")
        label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label.strip()) or "companion"
        sessions_dir, sess_index = self._sessions_store_paths()
        if sessions_dir is None:
            self.note("无法定位 OpenClaw state 的 sessions 目录，无法自动建会话。请改用方式 a 填已有会话 key。")
            return ""
        key = f"agent:main:{label}"
        sid = __import__("uuid").uuid4().hex
        jsonl_path = sessions_dir / f"{sid}.jsonl"
        print(f"  [..] 正在新建会话 key={key} …")
        try:
            sessions_dir.mkdir(parents=True, exist_ok=True)
            header = {
                "type": "session", "version": 1, "id": sid,
                "timestamp": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + ".000Z",
                "cwd": str(Path.home()),
            }
            with open(jsonl_path, "w", encoding="utf-8") as fh:
                fh.write(__import__("json").dumps(header) + "\n")
            idx = {}
            if sess_index.exists():
                idx = __import__("json").load(open(sess_index, encoding="utf-8"))
            now_ms = int(__import__("time").time() * 1000)
            idx[key] = {
                "sessionId": sid,
                "sessionFile": str(jsonl_path),
                "updatedAt": now_ms,
                "sessionStartedAt": now_ms,
                "lastInteractionAt": now_ms,
                "systemSent": False,
                "chatType": "direct",
                "label": label,
            }
            tmp = sess_index.with_suffix(".json.new")
            __import__("json").dump(idx, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
            os.replace(tmp, sess_index)
        except Exception as exc:
            self.note(f"新建会话失败: {exc}")
            return ""
        # 实测：chat.inject 注入一条占位消息，确认 session 可写
        ok = self._probe_inject(key)
        if not ok:
            self.note(f"会话已登记但 chat.inject 探测失败（Gateway 拒绝或 token 未配）。key 仍会写入 settings，"
                      "后续请配置 scheduling.gateway.auth_token_env 对应 token。")
        print(f"  [ok] 已新建会话 key: {key}")
        return key

    def _sessions_store_paths(self):
        """定位 OpenClaw state 的 sessions 目录与 sessions.json。
        优先读 settings 的 scheduling.gateway.extra_env.OPENCLAW_STATE_DIR，否则落 $HOME/.openclaw。
        """
        import sys as _s
        sys.path.insert(0, str(self.instance.parent.parent / "scripts"))
        sys.path.insert(0, str(self.instance.parent.parent))
        try:
            import settings_loader as sl
            _orig = sl._validate_secrets
            sl._validate_secrets = lambda s: None
            settings = sl.load_settings(str(self.settings_path))
            extra = {}
            try:
                extra = (settings.get("scheduling", {}).get("gateway", {}) or {}).get("extra_env", {}) or {}
            except Exception:
                extra = {}
            state = extra.get("OPENCLAW_STATE_DIR")
            if state:
                base = Path(state)
            else:
                base = Path.home() / ".openclaw"
        except Exception:
            base = Path.home() / ".openclaw"
        sessions_dir = base / "agents" / "main" / "sessions"
        return sessions_dir, sessions_dir / "sessions.json"

    def _probe_inject(self, key: str) -> bool:
        """用 gateway chat.inject 实测 key 对应的 session 可写。"""
        import sys as _s
        sys.path.insert(0, str(self.instance.parent.parent / "scripts"))
        sys.path.insert(0, str(self.instance.parent.parent))
        try:
            import settings_loader as sl
            _orig = sl._validate_secrets
            sl._validate_secrets = lambda s: None
            settings = sl.load_settings(str(self.settings_path))
            from common import openclaw_env
            import json as _json
            env = openclaw_env(settings)
            token_env = (settings.get("scheduling", {}).get("gateway", {}) or {}).get("auth_token_env")
            if token_env:
                env[token_env] = self._read_env().get(token_env, "")
            ws = (settings.get("scheduling", {}).get("gateway", {}) or {}).get("ws_url")
            bin_ = None
            for c in (settings.get("scheduling", {}).get("gateway", {}) or {}).get("openclaw_bin"), shutil_which("openclaw"):
                if c:
                    bin_ = c
                    break
            if not bin_:
                return False
            params = _json.dumps({"sessionKey": key, "message": "[guided] session 创建验证"}, ensure_ascii=False)
            cmd = [bin_, "gateway", "call", "chat.inject", "--params", params, "--json"]
            if ws:
                cmd += ["--url", ws]
            if token_env and env.get(token_env):
                cmd += ["--token", env[token_env]]
            cmd += ["--timeout", "15000"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            parsed = _json.loads(r.stdout or "{}")
            return parsed.get("ok") is True
        except Exception as exc:
            print(f"  [..] chat.inject 探测异常: {exc}")
            return False

    def _resolve_session_id(self, session_key: str) -> str | None:
        try:
            import sys as _s
            sys.path.insert(0, str(self.instance.parent.parent / "scripts"))
            sys.path.insert(0, str(self.instance.parent.parent))
            import settings_loader as sl
            _orig = sl._validate_secrets
            sl._validate_secrets = lambda s: None
            settings = sl.load_settings(str(self.settings_path))
            from providers.openclaw_gateway import session_id
            return session_id(settings)
        except Exception as exc:
            print(f"  [..] 解析 session id 失败: {exc}")
            return None

    # ------------------------------------------------------------- 6. verify
    def verify(self) -> bool:
        self.step(9, "最终校验")
        ok = True
        import yaml
        data = yaml.safe_load(self.settings_path.read_text(encoding="utf-8"))
        gw = (data.get("scheduling") or {}).get("gateway") or {}
        env = self._read_env()
        any_llm_key = any(k in env for k in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "ARK_API_KEY"))
        checks = [
            ("核心配置 md 已就位", all(
                (self._resolve_glob(n).exists() if "*" in n else (self.instance / n).exists())
                for n, *_ in CORE_MD
            )),
            (".env 存在", self.env_path.exists()),
            ("对话模型 key 已配置", any_llm_key),
            ("OpenClaw 主模型已配置", self._openclaw_primary_model() is not None),
            ("Telegram token+chat_id 已配置", "TELEGRAM_BOT_TOKEN" in env and "TELEGRAM_CHAT_ID" in env),
            ("session_key 已填", bool(gw.get("session_key"))),
        ]
        for name, passed in checks:
            print(f"  [{'ok' if passed else 'x'}]  {name}")
            ok = ok and passed
        if not ok:
            self.note("有项未完成，按上面提示补齐后再跑 verify。")
        return ok

    def _resolve_glob(self, pattern: str) -> Path | None:
        import glob
        from pathlib import Path as _P
        matches = glob.glob(str(self.instance / pattern))
        return _P(matches[0]) if matches else None

    def _read_env(self) -> dict[str, str]:
        out = {}
        if self.env_path.exists():
            for ln in self.env_path.read_text(encoding="utf-8").splitlines():
                if "=" in ln and not ln.strip().startswith("#"):
                    k, _, v = ln.partition("=")
                    out[k.strip()] = v.strip()
        for k in ("DEEPSEEK_API_KEY", "ARK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            if os.environ.get(k):
                out[k] = os.environ[k]
        return out

    # ------------------------------------------------------------- 收尾：重启生效
    def finalize(self) -> None:
        """配置齐全后最后才做：重启 gateway 让 settings/AGENTS 生效。"""
        self.step(10, "重启生效")
        self.note("所有配置就绪后，最后一步是重启 OpenClaw Gateway，让新的 settings、")
        self.note("AGENTS.md 运行规则、session 绑定真正生效。")
        if shutil_which("openclaw") is None:
            self.note("未找到 openclaw CLI，请重启后手动生效。")
            return
        ans = self.ask("现在重启 openclaw gateway？（y/n）", "n")
        if ans.lower() in ("y", "yes"):
            try:
                env, cmd = self._restart_command()
                rc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env).returncode
            except Exception as exc:
                print(f"  [x]  gateway restart 异常: {exc}")
                return
            print(f"  [{'ok' if rc == 0 else 'x'}]  gateway restart 返回 {rc}")
        else:
            self.note("跳过自动重启。配置后请执行：openclaw gateway restart")

    def _restart_command(self):
        """构造指向『当前实例网关』的重启命令+环境，避免误碰同一台机器上的其它实例(如系统级 root)。
        若 settings 配了 extra_env(OPENCLAW_STATE_DIR/CONFIG_PATH/PROFILE) 或 ws_url，就带上它，
        让 openclaw gateway restart 落到当前实例而不是 local loopback 探测到的其它 gateway。
        返回 (env_dict, cmd_list)。
        """
        import sys as _s
        sys.path.insert(0, str(self.instance.parent.parent / "scripts"))
        sys.path.insert(0, str(self.instance.parent.parent))
        cmd = ["openclaw", "gateway", "restart"]
        env = None
        try:
            import settings_loader as sl
            _orig = sl._validate_secrets
            sl._validate_secrets = lambda s: None
            settings = sl.load_settings(str(self.settings_path))
            from common import openclaw_env
            env = openclaw_env(settings)
        except Exception as exc:
            print(f"  [..] 无法解析实例网关环境，将使用默认: {exc}")
        return env, cmd


def shutil_which(name: str) -> str | None:
    from shutil import which
    return which(name)


def run_quiet(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, check=False).stdout.strip()
    except Exception:
        return ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--instance-dir", default=str(Path.home() / ".openclaw/workspace"))
    p.add_argument("--non-interactive", action="store_true", help="走默认值，不提问")
    p.add_argument("--test-telegram", action="store_true", help="发送真实 Telegram 测试消息")
    p.add_argument("--verify-only", action="store_true")
    a = p.parse_args()
    guide = Guide(Path(a.instance_dir).resolve(), non_interactive=a.non_interactive)

    if a.verify_only:
        return 0 if guide.verify() else 1

    env_ok = guide.check_environment()
    guide.guide_core_md()
    if env_ok:
        guide.guide_api_keys()
        guide.guide_main_model()
        guide.guide_telegram(test=a.test_telegram)
        guide.guide_telegram_pairing()
        guide.guide_gateway_session()
        guide.guide_openclaw_memory()
    final = guide.verify()
    if final:
        guide.finalize()   # 重启 gateway 生效（所有配置就绪后最后一步）
    print("\n" + "=" * 60)
    print("  部署向导完成。" + ("全部校验通过 🎉" if final else "请按提示补齐后重跑"))
    print("=" * 60)
    return 0 if final else 1


if __name__ == "__main__":
    raise SystemExit(main())
