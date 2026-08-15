# xiaodou-system

## 摘要

本文档提出并实现一套**角色无关**的「陪伴式智能体」运行框架。它将以下能力组织为一条确定性的每日流水线：自动生成角色的生活轨迹、在合理时机主动向用户推送消息与照片、以及在日终将一天的对话整理为长期记忆。框架不依赖预置的对话脚本，而是由用户在 workspace 中定义的一组核心文件驱动，因此**更换一组文件即可得到完全不同的角色**，可被自由复用与二次开发。

框架的运行建立在 [OpenClaw](https://docs.openclaw.ai) 之上，对外部模型服务（DeepSeek、Seedream）的调用、消息发送与网关交互均通过 `providers/` 抽象层解耦；其余步骤由确定性脚本完成，并通过 JSON Schema 校验、幂等门、文件锁与状态回写保证可复现与可恢复。

框架源自一个已长期线上运行的同名陪伴系统。本仓库在保留其完整功能的同时，剥离了其中与特定角色、特定场景及特定平台绑定的部分，使其可作为**通用基础设施**在相同环境（Linux + OpenClaw）下被任意复用。

## 研究动机与意义

将「陪伴式智能体」从一次性的演示推向可长期、稳定、自动运行的服务，面临三个基本问题：一是**主动性**——系统需要在没有用户触发的情况下，自主决定何时、以何种方式与用户交互；二是**连续性**——交互产生的信息需要被沉淀、组织并形成可持续增长的长期记忆，而不是随会话结束而丢失；三是**确定性**——对于一个需要长期无人值守运行的系统，行为必须可预期、可校验、可恢复，而不能依赖模型每次的偶发输出。

本框架对这三个问题的处理如下：主动性由「晨间规划 + 定时调度」提供，连续性由「日终记忆 + 会话重置」提供，确定性则由贯穿全流程的校验与状态机保证。下文将逐一说明其架构与部署方式。

下图展示了本框架在**正常运行状态下**的实际运行效果，角色基于自己的生活轨迹主动发起日常对话、并在合适时机分享照片与自拍：

<p align="center">
  <img src="docs/screenshot-telegram.jpg" alt="正常运行状态下的 Telegram 对话效果" width="800"/>
  <br/>
  <em>图 1：正常运行状态下，角色自主生成并发送的 Telegram 日常对话与自拍</em>
</p>

## 系统架构

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system 架构图" width="880"/>
</p>

框架整体由四条并行与串联的流水线构成，分别对应三个阶段与一个持久层：

- **step02 · 每日规划**：读取一日的核心文件与天气信息，为角色生成当天的完整生活轨迹，并通过 Schema 校验后落盘。
- **step03 · 调度与执行**：将当天需要主动触达用户的非静默事件提交至 `at` 定时器，到点执行——生成文案、调用图像服务（如需自拍）、通过 Telegram 发送，并回写当日状态。
- **step04 · 日终记忆**：在当天结束后，汇总聊天记录生成融合式每日记忆，并入长期记忆文件（保留最近七日）；并在每周日由 LLM 对长期记忆正文去重、合并、压缩后重写最上方正文，防止篇幅无限增长；凌晨完成会话重置。
- **持久层**：所有阶段的配置、状态、日志、备份及长期记忆均落盘于工作目录，并定期归档校验。

上述阶段所需的外部依赖，均通过 `providers/` 抽象层接入，包括语言模型、图像生成、消息发送与 OpenClaw 网关。

## 运行形态：一个完整周期

以一个自然日为例，系统按下列时间线自动运转（角色语气与话题由核心文件与当天生活轨迹决定，框架仅负责在正确的时间以正确的方式触达）：

```text
06:00  step02 · 每日规划
        └─ 读取核心文件与天气 → 生成当天生活轨迹（活动 / 地点 / 情绪 / 交互窗口）
           ↓  通过 JSON Schema 校验后落盘，不合格则重试

06:20  step03 · 调度
        └─ 将当天“非静默”事件提交给 at 定时器

（示例，具体时间点由计划决定）  step03 · 执行与触达
        └─ 到点：判断用户忙碌状态 → 生成文案 → 必要时代理图像服务生成自拍
           → 通过 Telegram 发送 → 回写当日档案

00:20 / 00:50  step04 · 日终记忆 finalize（两次，容错重试）
        └─ 汇总当日聊天与事件，生成融合式每日记忆

02:00        step04 · 长期记忆更新 incremental
        └─ 将值得长期保留的候选并入 MEMORY.md

02:03        step04 · 长期记忆融合 attach
        └─ 将当日完整记忆正文合并，保留最近七天

04:00        step04 · 会话重置 rollover
        └─ 生成会话承接信息并重置会话，开启新的一天

周日 04:30   step04 · 每周正文整理 compress
        └─ 由 LLM 对 MEMORY.md 的全部 section 去重、合并与压缩，重写最上方的长期记忆正文
           （篇幅收敛到上限，保留稳定事实与长期偏好；同日增量不再重复追加）
```

每一步均由确定性脚本实现，配合 JSON Schema 校验、幂等门、文件锁与状态回写，不依赖模型偶发输出。

---

## 快速开始（新环境部署）

前置依赖（与线上一致的环境）：

- Linux，Python 3.10+
- `at` + `atd` 服务（`systemctl enable --now atd`）
- 时区 `Asia/Shanghai`
- 已安装 [OpenClaw](https://docs.openclaw.ai)（`openclaw` CLI 在 PATH）
- `pip install -r requirements.txt`（PyYAML、openai）

### 1. 先配好 OpenClaw 本身

framework 的主动消息与记忆功能均依赖 OpenClaw，因此需先完成 OpenClaw 自身的初始化配置：

```bash
# 首次安装后，OpenClaw 的交互式引导（gateway / 主模型 / Telegram bot）：
openclaw configure
```

这一步会带你：
- 启动 gateway（监听端口，默认 18789）
- 配置 agent 主模型（framework 用 DeepSeek，配 `deepseek/deepseek-v4-flash` + API key）
- 绑定 Telegram bot（@BotFather 创建后粘贴 token）

> **若你已有 OpenClaw 实例在默认端口** 占着（比如 18789），给这套实例换个端口避免冲突：
> `openclaw config set gateway.port 19203`（改完重跑 `openclaw gateway start`）
>
> 验证 OpenClaw 健康：`openclaw health`

### 2. 初始化 instance

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` 默认把**实例目录设为 `~/.openclaw/workspace`**（可 `--instance-dir` 覆盖），会：
校验环境 → 把核心文件模板复制到 workspace（**已存在的跳过、缺失的如 LIFE/MEMORY 才新建**）→
生成 `settings.yaml` → 建目录 → 全量编译校验。

### 3. 运行部署向导

```bash
python3 scripts/guided_setup.py
```

向导按顺序带你完成整个链条：

1. **环境检测**（python / atd / openclaw / 时区）
2. **准备核心配置 md**——这 7 个文件就在 workspace 根（OpenClaw 自动读取）。
   已存在则直接编辑/上传同名文件覆盖；缺失的由 init 建好骨架，你在 workspace 上传
   同名文件替换正文即可，不用在终端改。
3. **配置 API key** 写入 `.env`（模型商自选）
4. **Telegram 权限绑定**（@BotFather 创建 → 粘贴 token → 验证）
   - 批准**配对**（pairing）：按提示给 bot 发第一条消息，向导自动批准你自己接入
   - **chat id 不需要你手动填** —— 批准配对时向导会自动把你的 Telegram 账号 id
     记录为 `TELEGRAM_CHAT_ID`，你只管给 bot 发消息，不用懂 chat id 是什么
5. **Gateway session 绑定**（填 `scheduling.gateway.session_key`，自动解析 session id）
6. **OpenClaw 记忆契约就位**（自动禁用 OpenClaw 原生 memory writer，避免双重写入冲突）
7. **最终校验**（全绿才通过）
8. **重启 gateway 生效**（最后一步，配置齐全后才执行）

> **运维提示**：不要用 `timeout <秒>` 包裹注入类命令（如 `execute_daily_event.py`、
> cron / at 触发的脚本）。生成与注入可能超过几十秒，`timeout` 会中途杀掉进程，
> 导致消息已生成但没注入/没发送。framework 的 cron/at 模板自带 flock 锁、
> 不加 timeout，脚本自己会在超时/异常时安全退出。

> 用 `--non-interactive` 走默认值（CI/脚本化），`--verify-only` 只跑最终校验。

### 4. 安装定时任务（可选）

```bash
bash init.sh --instance-dir ./instance --install-cron
```

会在 `ALLOW_CRON_APPLY=1` 时写入（否则只打印、不写）：

- **root 用户** → 系统 crontab `/etc/crontab`（命令前带 `root` user 字段）
- **普通用户** → 用户级 crontab（`crontab -`，命令前无 user 字段，`crontab -l` 可看）

安装的是三条流水线：
- `step02-morning` 每天 06:00 生成当日计划
- `step03-morning` 每天 06:20 把非 silent 事件提交到 `at`（真实调度）
- `step04-nightly` 夜间 finalize / incremental / attach / session_rollover / 每周 compress

> 普通用户需系统里已启用 `atd`（至少 root 装好 `at` + `atd`），step03 的 `at` 调度才能跑。

---

## 核心文件怎么填（在 workspace 根）

这 7 个文件放在 OpenClaw workspace 根目录（默认 `~/.openclaw/workspace`），
OpenClaw 启动时自动读取它们。init.sh 已把模板复制到位，你只需在 workspace
**上传同名文件替换正文**即可：

- `AGENTS.md` — 运行规则（怎么主动发消息/自拍/记忆/回复节奏）
- `IDENTITY.md` — 固定身份（我是谁/外貌/家庭/关系）
- `SOUL.md` — 性格/情绪/说话方式/相处边界
- `LIFE.md` — 生活规律与作息（Planner 生成的依据）
- `USER.md` — 陪伴对象（对方是谁/偏好/边界）
- `TOOLS.md` — 工具与脚本说明（由体系提供）
- `MEMORY.md` — 长期记忆（由每日记忆任务维护）

> 已存在的文件（OpenClaw 自带的 AGENTS/IDENTITY/SOUL/USER/TOOLS，或你已填的）
> init 会跳过、绝不覆盖；缺失的（如 LIFE/MEMORY）才会新建骨架。你要自定义任何文件，
> 都是直接在 workspace 上传同名文件替换。

---

## 目录结构

```
├── init.sh                    # 安装器（校验→复制核心模板到 workspace→生成 settings→编译→cron）
├── AGENTS.md                  # 核心文件模板（同下，init 复制到 workspace）
├── IDENTITY.md
├── SOUL.md
├── LIFE.md
├── USER.md
├── TOOLS.md
├── MEMORY.md
├── scripts/                   # 确定性执行脚本（planner/validate/schedule/execute/memory）
├── providers/                 # 外部依赖抽象（LLM/生图/gateway/delivery）
├── prompts/                   # 各阶段的 model prompt（高度通用，已内置）
├── schemas/                   # 各阶段 JSON schema（v1）
├── cron/                      # cron 模板（step02/03/04）
└── settings.example.yaml      # 数据契约（运行时配置）
```

## 发展状态

### 已真实环境验证(fw-test 纯普通用户端到端)

- [x] **step02** 晨间 planner 骨架（planner → initialize daily）
- [x] **step03** at 调度 + 执行器（validate → schedule → execute → inject → delivery → 真实 Telegram 发送）
- [x] **step04** 夜间记忆闭环（finalize → 长期记忆更新 → 会话 rollover → 记忆压缩 → 真实 DeepSeek 生成 + backup 归档）
- [x] P1 去痕迹 + cron 模板（step02/03/04）+ init.sh + 部署向导 `guided_setup.py`
- [x] 运行规则模板层（AGENTS.md，含主动发图片/自拍/记忆/搜索机制）
- [x] 隔离环境逻辑验证（独立 venv 零本机依赖：编译 / import / cron / marker 解析 / reconcile）
- [x] 普通用户 cron 双形态（root 系统 crontab / 普通用户 crontab）
- [x] 部署向导配对自动获取 chat id（普通用户无需手动填）

### step04 夜间记忆包含什么

- `finalize_day` / `finalize_yesterday` — 生成昨天融合式每日记忆（schedule 校验 → schema 校验 → chatlog render → LLM 生成 → MEMORY／daily memory 落盘，`--apply` 落地 + `--ack FINALIZE_DAY` 门）
- `update_memory_md` — 长期记忆维护，四种模式：
  - `incremental`：将值得长期保留的候选并入 `MEMORY.md`
  - `attach`：将当日完整记忆正文合并，保留最近 7 天
  - `compress`（每周一次，周日）：由 LLM 对 `MEMORY.md` **全部 section 去重、合并并压缩**，重写文件最上方的长期记忆正文，控制篇幅不无限增长
  - `prune`：按保留天数清理过期片段
- `session_rollover` — 凌晨会话重置 + carryover（带 quiescence 等待与一致性校验）
- `normalize_chatlog` / `render_chatlog` — 从 OpenClaw gateway 会话历史还原、剥内部事件 marker，渲染成 `chatlog/YYYY-MM-DD.md`
- `reconcile_daily_state` — 当日 daily JSON 与实际发送记录调和
- 备份链 `backup_openclaw` / `raw_backup` / `mirror_openclaw_memory` — 会话 / 记忆 / 原始证据归档与校验
- 编排使用 `step04_config` 从 `settings.yaml` 构造线上同构 config，零硬编码路径

对应 cron：`finalize (00:20/00:50)` → `incremental (02:00)` → `attach (02:03)` → `rollover (04:00)` → `compress（周日 04:30，重写最上方正文）`

### 当前限制与后续路线

本框架目前仍处于**单一环境验证**阶段，以下限制为已知项，均列入后续迭代计划：

**平台支持**：

- **已支持**：Linux。全部流水线、cron 双形态（root 系统 crontab / 普通用户 crontab）与 `at` 调度，均已在 Linux 真实环境端到端验证。
- **计划中**：Windows 与 macOS。依赖的 `at` / `flock` 调度机制需针对不同平台改造，后续版本化推进。

**消息通道**：

- **已支持**：Telegram 插件。当前消息触达与发送通过 Telegram bot 完成。
- **计划中**：微信等更多通道适配。`providers/` 的 delivery 抽象层已为多通道预留，但微信插件尚未接入。

**开发期事项**：

- 普通用户 atd 下的触发式 cron 调度优化
- 更多角色 prompt 内置样例
- Lint / CI 接入

## License

MIT © xiaodou-system
