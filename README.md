# 🧸 xiaodou-system

> 一套「陪伴式 AI 角色」的**可打包、可复用运行框架** —— 把自动生活规划、主动消息、日终自记忆,
> 提炼成 **角色无关** 的确定性流水线,基于 [OpenClaw](https://docs.openclaw.ai) 运行。

---

## 这是什么

把你想象中「会自己生活、会主动找你聊天、会记得你们之间的点点滴滴」的 AI 角色,**真正跑起来**:

- 🌅 **晨间** —— 自动生成一整天的生活轨迹(情绪 / 地点 / 活动 / 状态)
- 📨 **随时** —— 在合理窗口主动给你发消息、发自拍,工作忙时自动降低打扰
- 🌙 **日终** —— 把一天的聊天整理成融合式记忆,长期保存到 `MEMORY.md`,凌晨自动重置会话
- 🔄 **完整闭环** —— 生成 → 校验 → 调度(`at`)→ 执行发送 → 记忆,全是确定性脚本,不靠手工

角色本身由 OpenClaw workspace 根目录的 `AGENTS.md` / `IDENTITY.md` / `SOUL.md` / `LIFE.md` /
`USER.md` / `TOOLS.md` / `MEMORY.md` 定义——**换一套文件就是另一个角色**,框架完全不认死。

## 为什么会有它

它源自一个线上长期运行的「小豆」陪伴系统。这个仓库把这个系统里**绑定具体角色与场景**的部分
全部剥离,留下**通用、可复用、可搬家**的三层流水线(step02 / 03 / 04),任何人都能拿它搭自己的角色。

> 只保证在**与你线上相同的 Linux + OpenClaw** 环境可迁移,不追求跨平台万能。

## 架构总览

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  step02      │ →  │  step03      │ →  │  step03      │ →  │  step04      │
│  晨间 Planner │    │  校验+调度    │    │  执行+发送    │    │  日终记忆    │
│  (生成计划)   │    │  (at 提交)   │    │  (消息/自拍)  │    │  (记忆+重置) │
└─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
      ① 每天 06:00      ② 每天 06:20        ③ 全天随机触发      ④ 每天 00:20–04:30
```

- **核心文件**:与 OpenClaw **共用同一份** workspace 根 prompt 文件,不另建重复目录。
- **调用模型的 prompt**:planner / decision / message / memory 四份高度通用,已内置。
- **确定性工程**:每步有 JSON schema 校验、幂等门、flock 锁、日志与状态回写,不靠模型硬撑。

## 快速开始（新环境部署）

前置依赖（与线上一致的环境）：

- Linux，Python 3.10+
- `at` + `atd` 服务（`systemctl enable --now atd`）
- 时区 `Asia/Shanghai`
- 已安装 [OpenClaw](https://docs.openclaw.ai)（`openclaw` CLI 在 PATH）
- `pip install -r requirements.txt`（PyYAML、openai）

### 1. 先配好 OpenClaw 本身

framework 的主动消息/记忆都依赖 OpenClaw，所以先让 OpenClaw 自己跑起来：

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
- `update_memory_md` — 长期记忆四种模式：`incremental` / `attach`（保留最近 7 天）/ `compress`（超长压缩）/ `prune`
- `session_rollover` — 凌晨会话重置 + carryover（带 quiescence 等待与一致性校验）
- `normalize_chatlog` / `render_chatlog` — 从 OpenClaw gateway 会话历史还原、剥内部事件 marker，渲染成 `chatlog/YYYY-MM-DD.md`
- `reconcile_daily_state` — 当日 daily JSON 与实际发送记录调和
- 备份链 `backup_openclaw` / `raw_backup` / `mirror_openclaw_memory` — 会话 / 记忆 / 原始证据归档与校验
- 编排使用 `step04_config` 从 `settings.yaml` 构造线上同构 config，零硬编码路径

对应 cron：`finalize (00:20/00:50)` → `incremental (02:00)` → `attach (02:03)` → `rollover (04:00)` → `compress (周日 04:30)`

### 待办

- 触发式 cron 采用普通用户 atd 时的调度优化
- 更多角色 prompt 内置样例
- Lint / CI 接入

## License

MIT © xiaodou-system
