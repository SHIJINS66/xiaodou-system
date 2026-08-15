# xiaodou-system 系统完整描述（供绘制架构图使用）

> 本文档客观描述 xiaodou-system 这一「角色无关的陪伴式智能体运行框架」的真实架构，
> 供绘制系统架构图 / 拓扑图时参考。所有模块、脚本、调度、数据流均与代码实现一致。

---

## 一、一句话定位

xiaodou-system 是一个基于 **OpenClaw** 的、**角色无关**的陪伴式智能体运行框架。
它让一个「由 Markdown 文件定义人格的 AI 角色」，在无人值守的 **Linux** 服务器上，
每天自动完成三件事：**生成一天的生活轨迹 → 在恰当时间主动触达用户 → 日终把对话沉淀为长期记忆**。

---

## 二、技术栈与环境

| 层面 | 技术 / 组件 |
|---|---|
| 操作系统 | Linux（含 `atd`、`flock`、`cron`） |
| 智能体运行时 | OpenClaw（网关、session、Telegram bot、消息注入） |
| 语言模型（对话/规划/记忆） | DeepSeek（OpenAI 兼容接口） |
| 图像生成（自拍） | 豆包 Seedream |
| 编排 | `at` 定时器 + `cron` + 多个 Python 确定性脚本 |
| 数据契约 | JSON Schema（19 个）、YAML 运行时配置（settings.yaml） |
| 长期记忆 | workspace 内 Markdown（MEMORY.md + daily memory） |
| 消息通道 | Telegram |

---

## 三、核心概念：角色与系统解耦

- **角色的"人格"**不写死在代码里，而是由 workspace 内 **7 个核心 Markdown 文件**定义：
  `AGENTS.md`（运行规则）、`IDENTITY.md`（固定身份）、`SOUL.md`（性格/情绪/说话方式）、
  `LIFE.md`（生活规律）、`USER.md`（陪伴对象）、`TOOLS.md`（工具说明）、`MEMORY.md`（长期记忆）。
- **系统**只负责确定性运维：调度、执行、记忆、消息发送、状态回写。
- 换一组核心文件 → 得到另一个角色，框架不必改动。

---

## 四、外部依赖抽象层（providers/）

所有对外交互统一封装，使外部服务可替换、不因具体供应商而结构性耦合。

| provider | 作用 | 落点 |
|---|---|---|
| `providers/llm/deepseek_urllib.py` | DeepSeek 对接（urllib 版） | 模型调用 |
| `providers/llm/openai_compatible.py` | OpenAI 兼容接口通用封装 | 模型调用 |
| `providers/image/seedream.py` | 豆包 Seedream 生图 | 自拍生成 |
| `providers/openclaw_gateway.py` | OpenClaw 网关：读 session 历史、解析 sessionId、`chat.inject` 注入 | 会话与主动消息 |
| `providers/gateway_history.py` / `gateway_sessions.py` | 会话历史读取、session 管理 | 会话 |
| `providers/telegram.py` | 消息发送 | 触达用户 |
| `providers/base.py` | provider 基类 | 抽象 |

---

## 五、三条业务流水线（step02 / step03 / step04）

### step02 · 每日规划（生成一天的生活轨迹）
- 核心脚本：`build_daily_plan.py`、`validate_daily_plan.py`、`initialize_daily_file.py`、`schedule_daily_events.py`、`schema_engine.py`、`schema_validator.py`
- 输入：7 个核心文件全文 + 当日天气（`weather_provider.py`）+ 最近 7 天记忆 + 昨天计划执行结果
- 输出：`daily/YYYY-MM-DD.json`（当天完整生活轨迹：活动、地点、情绪、可触达窗口）
- 关键点：输出经 **JSON Schema 校验**，不合格则重试，杜绝生成偶然性
- 触发时机：每天 **06:00**（cron step02-morning）

### step03 · 调度与执行（主动触达用户）
- 核心脚本：`run_morning_pipeline_step03.py`、`schedule_step03_events.py`、`at_adapter.py`、`execute_daily_event.py`、`event_decision.py`、`message_generation.py`、`event_transaction.py`、`deterministic_gate.py`、`context_snapshot.py`、`event_journal.py`
- 流程：
  1. 把当天规划中**非 silent** 的事件，经 `at_adapter.py` 提交到系统级 **`at` 定时器**
  2. 到达触发点 → `execute_daily_event.py` 执行完整触达链路
  3. 链路：判断用户忙闲（`context_snapshot.py`）→ 生成适配文案（`message_generation.py` + `prompts/message_generation_v1.md`）→ 需要时经 `providers/image/seedream.py` 合成自拍 → 经 `providers/telegram.py` 发送 → 通过 `providers/openclaw_gateway.py` 注入 session
- 关键点：幂等门（同一事件不重复发）、flock 文件锁、事务回写、事件日志（`event_journal.py`）
- 触发时机：每天 **06:20**（cron step03-morning，在 step02 完成后提交 at）

### step04 · 日终记忆（沉淀长期记忆）
- 核心脚本：`finalize_yesterday.py`、`build_daily_memory.py`、`update_memory_md.py`、`session_rollover.py`、`render_chatlog.py`、`normalize_chatlog.py`、`memory_quality.py`、`rollover_artifacts.py`
- 流程：
  1. **finalize**：把当天对话 + 事件汇总，生成融合式每日记忆（`prompts/daily_memory_v1.md`）
  2. **Incremental / Attach**：并入长期记忆文件 `MEMORY.md`（保留最近 7 天）
  3. **Session rollover**：凌晨重置会话并 carryover 承接
  4. **Weekly compress**：每周日由 LLM 对 `MEMORY.md` 最上方正文去重、合并、压缩（上限 5000 字）
- 触发时机（step04-nightly cron）：
  - `00:20 / 00:50` finalize_yesterday（两次，容错）
  - `02:00` incremental
  - `02:03` attach
  - `04:00` session_rollover
  - 周日 `04:30` compress

---

## 六、调度与节律（cron + at + 一个自然日的运转）

三个 cron 模板：`cron/step02-morning`、`cron/step03-morning`、`cron/step04-nightly`，
安装时由 init.sh 替换占位符，支持 **root（系统 crontab）** 与 **普通用户（用户 crontab）** 双形态。

一个自然日的完整自动运转：

```text
06:00  cron step02    → run_morning_pipeline 生成当日 daily（JSON Schema 校验）
06:20  cron step03    → run_morning_pipeline_step03 把非 silent 事件提交到 at
[按计划] at 触发      → execute_daily_event 执行触达链路（忙闲判断→文案→自拍→发送→注入→回写）
00:20/00:50  finalize_yesterday（生成昨日 daily memory，两次容错）
02:00       update_memory_md --incremental（并入长期记忆）
02:03       update_memory_md --attach（完整正文，保留 7 天）
04:00       session_rollover（重置会话 + carryover）
周日 04:30  update_memory_md --compress（LLM 压缩 MEMORY.md 正文，≤5000 字）
```

---

## 七、安装与部署（init.sh + guided_setup.py）

- `init.sh`：校验环境 → 复制 7 个核心模板到 workspace → 生成 settings.yaml → 建目录 → 安装 cron（可选）
- `scripts/guided_setup.py`：部署向导，逐步完成 环境检测 → 核心文件 → API key → Telegram 配对（自动获取 chat id）→ OpenClaw session 绑定 → 记忆契约就位 → 校验 → 重启
- 运行时配置：`settings.yaml`（顶层含 system / runtime / character / companion / interaction_policy / selfie / models / delivery 等）
- 备份：`backup_openclaw.py`、`raw_backup.py`、`rollover_artifacts.py`（会话/记忆/原始证据三类）

---

## 八、给绘图模型的重点提示（可随图一并提交）

- **纵轴时序**：清晨生成计划 → 日间按 at 触达 → 日终沉淀记忆，是一个自顶向下的时钟循环
- **三条流水线横向排布**：step02 → step03 → step04（或自上而下），块间用箭头表示数据流向
- **持久层**：横贯最底部，向上用虚线连到各阶段（随时读写）
- **抽象层**：最顶部用罩状/横带表示 providers，向下连到流水线
- **技术栈节点**：DeepSeek、豆包 Seedream、OpenClaw、Telegram、at、cron、Linux 可作为矩形或设备图标散落在相应层
- **风格**：简约扁平、低饱和色、无阴影渐晕、清晰箭头与标签
