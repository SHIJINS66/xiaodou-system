# xiaodou-system

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux-blue" alt="platform"/>
  <img src="https://img.shields.io/github/v/release/SHIJINS66/xiaodou-system?color=green&label=release" alt="release"/>
  <img src="https://img.shields.io/badge/channel-Telegram-lightgrey" alt="channel"/>
  <a href="README.en.md"><img src="https://img.shields.io/badge/lang-English-blue" alt="lang-en"/></a>
</p>

## 摘要

**xiaodou-system** 是一个构建在 [OpenClaw](https://docs.openclaw.ai) 之上的、面向长期陪伴场景的角色无关运行框架。系统以自然日为基本运行周期，将角色定义、每日规划、定时事件执行、主动消息、日终记忆与 Session 切换组织为一条完整流水线：

```text
角色与历史状态
    ↓
每日规划
    ↓
定时事件与主动交互
    ↓
完整当日会话与事件记录
    ↓
日终记忆整理
    ↓
Session rollover
    ↓
下一自然日
```

本项目不扩展语言模型本身的推理能力，而是处理长期陪伴智能体中的几个工程问题：角色如何在没有即时用户输入时继续拥有自己的日程与状态；前一日的重要经历如何自然影响下一日；主动消息如何与实际发送结果、会话历史和长期记忆保持一致；以及如何避免多个独立的摘要机制同时改写同一段历史。

系统采用生成逻辑与控制逻辑分离的设计。DeepSeek 与 Seedream 等模型负责生成计划、文本和图像；调度、JSON Schema 校验、幂等、文件锁、事务状态、持久化和 Session 切换由确定性脚本控制。本文所称“确定性”仅指控制流程与状态迁移具有明确规则，不表示模型输出可以逐次复现。

当前实现以 **Linux + OpenClaw + Telegram** 为验证环境，默认使用 DeepSeek 作为文本模型、Seedream 作为图像生成服务。

---

## 一、背景与设计目标

### 1.1 长期陪伴式智能体的发展

人与计算机建立持续社会互动的设想并不是大语言模型时代才出现的。早期聊天系统以 ELIZA 为代表，主要依靠规则匹配和文本变换维持局部对话；它并不具备今天意义上的长期记忆、人格状态或自主行为，但已经说明自然语言界面本身足以使用户产生明显的社会性解释。此后很长一段时间，对话系统的研究重点仍集中在“当前一句如何回复得更合理”，任务型系统关注任务完成，开放域聊天系统则关注流畅性与相关性。[1]

2010 年代中后期，研究开始更加明确地区分**任务型助手**与**社会型对话系统**。Microsoft XiaoIce 将长期 engagement、情绪理解和社会关系作为主要目标，而不仅是完成一次查询或任务；其系统设计已经把长期交互视为独立优化对象。[2] 与此同时，PERSONA-CHAT 等工作把显式 persona 引入开放域对话，使角色身份与表达一致性从隐式语言风格问题转变为可以单独建模的输入条件。[3] 这一阶段形成了陪伴式系统的两个基础要求：角色应当具有相对稳定的身份与表达方式，并且系统评价不能只看单轮回答质量。

随着 Transformer 和大规模预训练语言模型的发展，开放域对话的生成能力明显提高，但上下文窗口仍然限制了长期交互。BlenderBot 3 已经把 long-term memory 与开放域对话、互联网访问结合在同一系统中，代表了一种典型思路：当前上下文之外维护额外存储，需要时再把历史信息取回。[4] 这使“长期记忆”逐渐从聊天记录保存问题转变为**存储、更新和检索策略**问题。

2023 年以后，大语言模型推动这一方向进一步向长期 Agent 演化。MemoryBank 面向长期 AI companion 场景设计了可持续更新的外部记忆，使模型能够从过去对话中提取用户信息、强化重要内容并在后续交互中重新调用。[5] Generative Agents 则把观察、记忆、反思和规划组合起来，使 Agent 不仅能够回忆过去，还能够根据历史经验安排未来活动、形成日程并主动与其他角色发生交互。[6] 这一变化很重要：长期系统开始从“能够记住过去的聊天机器人”转向“具有持续内部状态并能据此行动的 Agent”。

随后出现的问题不再只是“能否存下更多历史”，而是**历史能否在正确时间被正确使用**。LoCoMo 将长期对话评测扩展到最多 35 个 session，并把问题扩展到长期问答、事件总结和跨模态对话，结果表明即使模型拥有较长上下文或检索机制，跨 session 的时间、因果与事件关系仍然困难。[7] LD-Agent 因此进一步区分当前 session 与历史 session 的事件记忆，并动态维护用户与 Agent 的 persona，使长期对话不再只依赖单一的相似度检索。[8]

到 2025—2026 年，长期记忆研究继续从“事实召回”向更复杂的状态使用问题推进。LoCoMo-Plus 特别构造了**当前触发语义与历史约束并不直接相似**的场景，用于测试模型能否应用隐含的长期约束，而不是只回答“过去发生过什么”。[9] 这与真实陪伴交互非常接近：用户经常不会明确要求系统回忆某件事，但此前发生的事件、承诺和关系状态仍然应当改变当前回应。

因此，长期陪伴式智能体的发展可以概括为：

```text
单轮对话
    ↓
开放域社会聊天
    ↓
稳定 Persona
    ↓
跨 Session 长期记忆
    ↓
记忆驱动的规划与主动行为
    ↓
时间、事件与关系状态的持续承接
```

xiaodou-system 位于这一演化链的后半段。它并不提出新的通用记忆检索算法，而是关注一个更具体的工程问题：如何把角色每天的计划、实际发生的事件、主动消息、用户互动和长期记忆组织成连续的时间过程，使前一天的状态不只是“可以被搜索的历史”，而能稳定进入下一天的运行。

### 1.2 设计目标

xiaodou-system 主要解决以下五个问题：

1. **主动行为**：系统能够根据当天计划，在没有即时用户输入时产生未来事件，并在指定时间判断是否需要主动触达。主动行为应当来源于当天已经存在的角色状态，而不是每次定时器触发后临时让模型决定“现在做什么”。

2. **跨日连续性**：昨日的重要状态能够直接影响今日规划与对话，而不是只有在显式调用历史搜索后才重新出现。对于陪伴场景，“历史仍然保存”与“角色能够自然承接历史”是不同要求。

3. **可控执行**：事件是否执行、是否重复发送、消息是否已经写入 Session、失败后应该重试哪一步，必须由明确的程序状态决定。

4. **记忆一致性**：当天完整会话、生活计划和实际事件由同一套日终流程整理为跨日记忆，避免不同摘要机制分别解释同一段历史并在后续运行中产生冲突。

5. **角色与系统分离**：身份、性格、生活规律和用户关系由 Markdown 文件描述，执行逻辑不绑定具体角色。角色变化不应要求重写调度、事件执行和记忆流水线。

## 二、系统设计

### 2.1 角色文件

角色由 workspace 中的 7 个核心 Markdown 文件定义：

| 文件 | 职责 |
|---|---|
| `AGENTS.md` | 运行规则与行为约束 |
| `IDENTITY.md` | 身份与稳定背景 |
| `SOUL.md` | 性格、表达方式与互动边界 |
| `LIFE.md` | 生活规律与 Planner 的主要依据 |
| `USER.md` | 陪伴对象及长期信息 |
| `TOOLS.md` | 工具与运行环境说明 |
| `MEMORY.md` | 经整理后的长期记忆 |

角色内容不写死在业务脚本中。更换角色时，原则上只需要替换角色文件和相关资源配置。

### 2.2 自然日状态

系统将一个自然日作为主要运行单元：

```text
Day N
├── DailyPlan
├── Scheduled Events
├── Executed Events
├── Conversation
├── Event Journal
└── Daily Memory
```

`DailyPlan` 不是单纯的定时任务列表。它描述角色在这一天中预计处于什么活动、地点和情绪状态，以及哪些时间窗口允许主动交互。调度器只负责在指定时间启动事件，真正决定事件语义的是 DailyPlan。

这一设计使角色的主动行为具有前后关系。例如某个傍晚事件并不是一个孤立的“发送消息”任务，而是当天生活轨迹中的一个节点；事件执行时可以读取此前已经发生的活动、当前状态以及用户最近的交互，再决定是否发送、发送什么，以及是否需要图片。

DailyPlan 同时也是 step03 与 step04 之间的重要连接。step03 根据它执行事件；step04 则把“原计划”与“实际执行结果”同时纳入日终整理，从而区分计划、真实发生的事件以及被取消或抑制的事件。

### 2.3 当日会话与跨日记忆

当前 Session 主要保存当天完整交互；`Daily Memory` 与 `MEMORY.md` 保存已经过日终整理的跨日信息。

二者之间的转换路径固定为：

```text
完整当日 Session
+ DailyPlan
+ Event Journal
+ 主动消息与用户回应
        ↓
step04
        ↓
Daily Memory / MEMORY.md
```

这种划分首先解决信息来源问题。当天对话仍然保留原始上下文，主动事件也通过 Event Journal 和 Session 注入留下证据；到了日终，系统再从这些完整材料中生成跨日记忆，而不是在一天中反复把不同局部摘要写回同一个长期文件。

其次，它使“近期连续性”和“远期检索”可以分开处理。前一天仍会影响今天的状态，因此需要直接进入新一天的规划与上下文；更久以前、当前并不一定相关的信息，则可以继续通过长期记忆或搜索机制按需获取。

旧 transcript、日志和备份仍然保留，但主要用于审计、故障定位和恢复，不作为正常对话中的主要记忆入口。

### 2.4 生成与控制分离

模型负责：

- 生成 DailyPlan；
- 生成主动消息；
- 生成日终记忆文本；
- 可选生成图像。

确定性脚本负责：

- 定时触发；
- Schema 校验；
- 幂等判断；
- `flock` 并发控制；
- 发送与注入状态记录；
- 文件持久化；
- Session rollover；
- 异常恢复。

---

## 三、总体架构

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system 架构图" width="880"/>
  <br/>
  <em>图 1：系统总体架构 —— 三层业务流水线与贯穿始终的持久层</em>
</p>

系统主要由四部分组成：

- 角色文件；
- step02 / step03 / step04 三条流水线；
- `providers/` 外部依赖层；
- Linux 调度、文件持久化、日志和备份。

### 3.1 providers

外部服务统一封装于 `providers/`：

```text
providers/
├── llm/
│   ├── deepseek_urllib.py
│   └── openai_compatible.py
├── image/
│   └── seedream.py
├── openclaw_gateway.py
├── gateway_history.py
├── gateway_sessions.py
├── telegram.py
└── base.py
```

业务脚本依赖 provider 接口，而不是直接依赖某个供应商的具体 SDK。这样可以在不修改 DailyPlan、事件执行和记忆流程的前提下替换模型或消息通道。

### 3.2 持久化对象

主要运行数据包括：

```text
workspace/
MEMORY.md
daily/
daily/YYYY-MM-DD.json
chatlog/
daily_selfies/
event journal
transaction state
settings.yaml
backups/
```

关键状态优先落盘，而不是只存在于当前模型上下文中。

---

## 四、自然日运行流程

### 4.1 step02：每日规划

**默认触发时间：06:00**

主要脚本：

- `build_daily_plan.py`
- `validate_daily_plan.py`
- `initialize_daily_file.py`
- `schema_engine.py`
- `schema_validator.py`
- `weather_provider.py`

输入包括：

```text
角色核心文件
+ 当日天气
+ 最近 7 天记忆
+ 昨日执行结果
```

输出为：

```text
daily/YYYY-MM-DD.json
```

DailyPlan 描述当天活动、地点、情绪、可触达窗口和相关事件。生成结果必须通过 JSON Schema 校验后才能进入后续阶段。Schema 只验证结构契约，不保证模型内容本身完全正确。

### 4.2 step03：调度与事件执行

**晨间调度时间：06:20**

主要脚本：

- `run_morning_pipeline_step03.py`
- `schedule_step03_events.py`
- `at_adapter.py`
- `execute_daily_event.py`
- `event_decision.py`
- `message_generation.py`
- `event_transaction.py`
- `deterministic_gate.py`
- `context_snapshot.py`
- `event_journal.py`

晨间阶段读取 DailyPlan，将需要执行的 non-silent 事件提交给系统 `at`。

单个事件执行流程：

```text
读取 DailyPlan 与上下文
    ↓
幂等检查 / flock
    ↓
判断事件当前是否仍应执行
    ↓
生成消息
    ↓
可选生成图像
    ↓
Telegram 发送
    ↓
写入 OpenClaw Session
    ↓
状态与 Event Journal 回写
```

发送消息和写入 Session 是两个独立副作用，因此系统分别记录生成、发送、注入和最终提交状态。若消息已经成功发送但 Session 注入失败，恢复时只补做注入，避免重复发送。

### 4.3 step04：日终记忆

默认时序：

```text
00:20 / 00:50  finalize
02:00          incremental
02:03          attach
04:00          session_rollover
周日 04:30     weekly compress
```

主要脚本：

- `finalize_yesterday.py`
- `build_daily_memory.py`
- `update_memory_md.py`
- `session_rollover.py`
- `render_chatlog.py`
- `normalize_chatlog.py`
- `memory_quality.py`
- `rollover_artifacts.py`

step04 综合以下数据：

```text
完整当日 Session
+ DailyPlan
+ 实际事件状态
+ Event Journal
+ 主动消息
+ 用户回应
```

它首先重建当天实际发生的过程：哪些计划按时执行，哪些事件被取消或 suppress，角色主动发送过什么，用户对此如何回应，以及哪些事项仍然处于未完成状态。在此基础上生成 Daily Memory，并把需要长期保留的信息更新到 `MEMORY.md`。

因此 Daily Memory 不是简单的“聊天摘要”。它承担两个职责：一是保存当天 episode 的主要事实与关系变化，二是给下一自然日提供足够的近期状态。周度压缩再负责对长期 `MEMORY.md` 做去重和收敛，而不是在每个对话轮次持续改写长期记忆。

### 4.4 Session rollover

每日 rollover 在主要记忆处理完成之后执行：

```text
Day N 完整 Session
        ↓
Daily Memory
        ↓
MEMORY 更新
        ↓
session_rollover
        ↓
Day N+1
```

这样做既能尽量保留当天完整会话，也能保证进入下一天前，前一日信息已经进入项目自己的跨日记忆。

---

## 五、OpenClaw 的能力边界与适配问题

OpenClaw 已经提供 Agent workspace、Session、Memory、Memory Search、Active Memory、Compaction、Dreaming、Telegram 和 Automations 等通用能力 [10–18]。xiaodou-system 并不重新实现这些基础设施，而是对其中几项机制增加适合陪伴场景的约束。

### 5.1 记忆检索不等于跨日连续性

OpenClaw 可以持久化 daily memory，并通过 `memory_search` / `memory_get` 检索；Active Memory 也可以在交互过程中召回历史 [13][15]。

对通用 Agent 而言，按需检索可以控制上下文长度并减少无关信息。但陪伴式交互中，很多短句的正确理解依赖前一日状态，而当前消息本身未必包含足够明确的检索线索。

因此，仅保证历史“可搜索”并不足够。xiaodou-system 将昨日的重要状态直接作为下一日 Planner 与交互上下文的一部分；更远期的信息再交给 memory search 补充。

这里需要区分三件事：

- 数据仍然存在；
- 系统能够搜索到它；
- 角色会在当前对话中自然使用它。

本项目关注的是第三项。

### 5.2 Session 切换不能早于记忆整理

旧 Session 即使仍保留在 transcript、SQLite 或归档中，如果其中的重要信息尚未进入跨日记忆，新 Session 仍可能表现为“忘记了昨天”。

因此，xiaodou-system 把 transcript 视为审计和恢复数据，而不是正常跨日记忆的替代品。正常情况下，Session rollover 必须位于日终记忆处理之后。

### 5.3 为什么避免依赖长期 Session compaction

OpenClaw 的 Compaction 会把较早对话总结为摘要，并以“摘要 + 近期消息”的形式继续当前 Session [18][16]。这对于通用 Agent 是合理的上下文管理方式。

xiaodou-system 本身已经会在日终根据完整 Session、DailyPlan、Event Journal 和主动消息生成 Daily Memory。如果同一段历史先经过 OpenClaw Compaction，再经过项目自己的记忆整理，就可能同时存在两份来源不同的摘要。

两份摘要即使都没有明显错误，也可能在细节、时间顺序、未完成事项或重要性判断上不同。对于需要长期保持关系状态的陪伴角色，没有必要引入这种额外的不一致来源。

因此，当前设计更倾向于：

```text
单日尽量保留完整 Session
        ↓
step04 统一整理
        ↓
rollover
```

而不是让一个 Session 长期增长并反复依赖 Compaction。

### 5.4 为什么仍保留独立 step04

OpenClaw 已经具备 Memory、Active Memory、Dreaming 与 consolidation [13–15]。step04 仍然保留，是因为它处理的不只是“哪些聊天内容值得长期记住”，还需要同时对齐当天计划、实际事件、主动消息、用户回应和下一日仍需保留的状态。

因此它承担的是自然日状态整理，而不是另一个 Memory Search 实现。

### 5.5 为什么当前仍使用 `cron + at`

OpenClaw Automations 已经可以执行一次性和周期性任务 [18]。

当前版本仍使用 Linux `cron + at`，主要因为现有运行链已经围绕独立 Python 进程、文件状态、`flock`、Event Journal 和 Transaction State 完成验证，并且即使 Gateway 暂时不可用，也能记录事件本应执行以及失败原因。

`cron + at` 只是当前调度后端。未来可以替换为 OpenClaw Automations，只要 DailyPlan、事件状态和 step04 的数据契约保持不变。

---

## 六、可靠性设计

陪伴系统会长期无人值守运行，且主动消息属于对用户可见的外部副作用，因此不能只依赖“脚本运行成功”判断系统状态。xiaodou-system 把生成、调度、发送、Session 注入和持久化拆成可以分别确认的步骤。

### 6.1 JSON Schema

项目使用 19 个 JSON Schema 对跨阶段数据进行结构校验，用于检测必需字段缺失、类型错误、枚举值非法和不符合协议的模型输出。

Schema 的作用是保证 step02 / step03 / step04 之间交换的数据满足协议。它不负责判断生成文本的事实正确性，也不保证相同输入得到完全相同的输出。

### 6.2 幂等与并发

step03 使用稳定事件标识、状态检查和 `flock`，避免 cron 重入、`at` 重复触发或人工补跑导致同一事件重复发送。

只有“当前事件尚未完成且当前进程获得执行权”时，执行链才会继续。这样可以把操作系统级调度的重复触发与用户实际收到的消息次数分离。

### 6.3 部分失败与状态回写

事件执行分别记录：

```text
generated
delivered
injected
committed
```

这一区分对主动消息尤其重要。例如 Telegram 已发送成功而 Session 注入失败时，整个事件不能从头重试，否则用户可能收到第二条相同消息。系统应保留已经完成的外部副作用，只补做缺失步骤。

Event Journal 与 transaction state 因此既用于审计，也用于恢复时确定正确的重试边界。

### 6.4 备份

项目提供：

- `backup_openclaw.py`
- `raw_backup.py`
- `rollover_artifacts.py`

备份覆盖运行配置、长期记忆、聊天记录和事件产物，用于审计与异常恢复。备份可以帮助找回底层数据，但不能替代正常的跨日记忆流程；正常运行仍要求前一日的重要状态在 rollover 前完成整理。

## 七、运行验证

当前结果仅说明 Linux 环境中的主要运行链路已经贯通，不构成长期 SLA、聊天质量或跨平台性能证明。

### 7.1 Telegram

<p align="center">
  <img src="docs/screenshot-telegram.jpg" alt="正常运行状态下的 Telegram 对话效果" width="800"/>
  <br/>
  <em>图 2-a：角色在 Telegram 上的日常对话与自拍</em>
</p>

当前实现能够根据 DailyPlan 触发主动事件，在策略允许时生成文本和可选图像并通过 Telegram 发送。

### 7.2 文件持久化

<p align="center">
  <img src="docs/filesystem-directories.jpg" alt="框架自动沉淀的 daily / chatlog / daily_selfies 目录" width="800"/>
  <br/>
  <em>图 2-b：框架自动沉淀的 daily / chatlog / daily_selfies 数据目录</em>
</p>

运行过程中：

- `daily/` 保存计划与执行状态；
- `chatlog/` 保存聊天记录；
- `daily_selfies/` 保存图像产物；
- `MEMORY.md` 保存整理后的长期状态。

---

## 八、部署

### 8.1 环境要求

当前验证环境为 **Linux**：

- Python 3.10+
- `at` / `atd`
- `cron`
- `flock`
- 默认时区：`Asia/Shanghai`
- 已安装并初始化 OpenClaw
- Python 依赖：

```bash
pip install -r requirements.txt
```

### 8.2 OpenClaw 初始化

```bash
openclaw configure
openclaw health
```

需要完成模型/provider、Gateway、Telegram channel 和 workspace / session 对应关系配置。

如果默认端口被占用，可修改 Gateway 端口，例如：

```bash
openclaw config set gateway.port 19203
```

### 8.3 xiaodou-system 初始化

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` 负责环境检查、角色文件准备、`settings.yaml` 生成、目录创建、脚本校验和可选 cron 安装。

默认实例目录：

```text
~/.openclaw/workspace
```

可使用 `--instance-dir` 指定其他位置。

### 8.4 部署向导

```bash
python3 scripts/guided_setup.py
```

向导处理环境检查、核心文件、API key、Telegram 绑定、Gateway Session、记忆相关配置和最终校验。

可用模式：

```text
--non-interactive
--verify-only
```

### 8.5 定时任务

```bash
bash init.sh --instance-dir ./instance --install-cron
```

默认调度：

```text
06:00           step02-morning
06:20           step03-morning
00:20 / 00:50   finalize
02:00           incremental
02:03           attach
04:00           session_rollover
周日 04:30      weekly compress
```

step03 依赖 `atd`。

### 8.6 配置文件

运行配置集中在 `settings.yaml`，主要包括：

```text
system
runtime
character
companion
interaction_policy
selfie
models
delivery
```

---

## 九、目录结构

```text
├── init.sh
├── AGENTS.md
├── IDENTITY.md
├── SOUL.md
├── LIFE.md
├── USER.md
├── TOOLS.md
├── MEMORY.md
├── scripts/                   # planner / validate / execute / memory
├── providers/                 # LLM / image / gateway / delivery
├── prompts/                   # 各阶段模型提示词
├── schemas/                   # JSON Schema
├── cron/                      # step02 / step03 / step04
├── docs/                      # 架构图与运行截图
└── settings.example.yaml
```

运行实例还会产生 `daily/`、`chatlog/`、图像、事件日志、事务状态和备份。

---

## 十、当前限制

### 10.1 验证范围

当前仅在 Linux 环境完成端到端验证。尚未建立多发行版兼容矩阵、Windows / macOS 调度适配、长期 SLA、主动消息质量评测和系统化的多角色迁移测试。

### 10.2 单日上下文长度

系统希望在一个自然日内尽量保留完整 Session，但模型上下文仍存在硬上限。如果单日消息量或工具输出异常密集，仍可能在 rollover 前触发 Compaction。

因此，高交互量场景仍需要进一步设计单日上下文预算和受控的中间处理机制。

### 10.3 平台与通道

当前控制面依赖 `cron`、`at` 和 `flock`，正式验证的消息通道为 Telegram。其他平台和消息通道需要单独适配。

### 10.4 外部服务

DeepSeek 与 Seedream 属于外部依赖。模型名称、API、价格、速率限制和服务可用性可能变化，应通过 provider 配置管理。

---

## 十一、总结

xiaodou-system 将长期陪伴智能体组织为一个以自然日为主要运行边界的系统：

```text
历史状态
    ↓
DailyPlan
    ↓
事件与主动交互
    ↓
完整当日 Session
    ↓
Daily Memory / MEMORY.md
    ↓
Session rollover
    ↓
下一自然日
```

OpenClaw 提供 Session、Memory、消息通道和调度等通用 Agent Runtime 能力；xiaodou-system 在其上增加角色生活规划、事件执行、主动消息状态管理和日终记忆整理，使这些原本独立的能力形成连续的每日运行过程。

当前实现的主要特点可以归纳为四点：

- 角色内容由 Markdown 文件定义，与调度和执行代码分离；
- DailyPlan 同时描述当天生活状态和未来事件，而不只是定时任务；
- 当前 Session 尽量保留当天完整对话，跨日记忆由 step04 统一整理；
- 主动消息、Session 注入与状态回写均留下明确运行记录，以支持幂等与异常恢复。

当前版本已经在 Linux + OpenClaw + Telegram 环境完成主要链路验证，但仍受单日上下文长度、平台相关调度机制和单一消息通道验证范围限制。后续工作主要包括跨平台调度适配、更长周期稳定性验证、更多消息通道以及高交互量场景下的上下文管理。

## 参考资料

1. Shum, H.-Y., He, X., & Li, D. **From Eliza to XiaoIce: Challenges and Opportunities with Social Chatbots**. arXiv:1801.01957, 2018.  
   https://arxiv.org/abs/1801.01957

2. Zhou, L., Gao, J., Li, D., & Shum, H.-Y. **The Design and Implementation of XiaoIce, an Empathetic Social Chatbot**. arXiv:1812.08989, 2018.  
   https://arxiv.org/abs/1812.08989

3. Zhang, S., Dinan, E., Urbanek, J., Szlam, A., Kiela, D., & Weston, J. **Personalizing Dialogue Agents: I have a dog, do you have pets too?** arXiv:1801.07243, 2018.  
   https://arxiv.org/abs/1801.07243

4. Shuster, K., Xu, J., Komeili, M., et al. **BlenderBot 3: a deployed conversational agent that continually learns to responsibly engage**. arXiv:2208.03188, 2022.  
   https://arxiv.org/abs/2208.03188

5. Zhong, W., Guo, L., Gao, Q., Ye, H., & Wang, Y. **MemoryBank: Enhancing Large Language Models with Long-Term Memory**. arXiv:2305.10250, 2023.  
   https://arxiv.org/abs/2305.10250

6. Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. **Generative Agents: Interactive Simulacra of Human Behavior**. arXiv:2304.03442, 2023.  
   https://arxiv.org/abs/2304.03442

7. Maharana, A., Lee, D., Tulyakov, S., Bansal, M., Barbieri, F., & Fang, Y. **Evaluating Very Long-Term Conversational Memory of LLM Agents**. arXiv:2402.17753, 2024.  
   https://arxiv.org/abs/2402.17753

8. Li, H., Yang, C., Zhang, A., Deng, Y., Wang, X., & Chua, T.-S. **Hello Again! LLM-powered Personalized Agent for Long-term Dialogue**. arXiv:2406.05925, 2024.  
   https://arxiv.org/abs/2406.05925

9. Li, Y., et al. **LoCoMo-Plus: Beyond-Factual Cognitive Memory Evaluation for Long-Term Conversational Agents**. arXiv:2602.10715, 2026.  
   https://arxiv.org/abs/2602.10715

10. OpenClaw Documentation. **Agent workspace / Context**.  
    https://docs.openclaw.ai/concepts/agent-workspace  
    https://docs.openclaw.ai/concepts/context

11. OpenClaw Documentation. **The main session / Session management**.  
    https://docs.openclaw.ai/concepts/main-session  
    https://docs.openclaw.ai/concepts/session

12. OpenClaw Documentation. **Session management & compaction**.  
    https://docs.openclaw.ai/reference/session-management-compaction

13. OpenClaw Documentation. **Memory**.  
    https://docs.openclaw.ai/concepts/memory

14. OpenClaw Documentation. **Dreaming**.  
    https://docs.openclaw.ai/concepts/dreaming

15. OpenClaw Documentation. **Active Memory**.  
    https://docs.openclaw.ai/concepts/active-memory

16. OpenClaw Documentation. **Context / Compaction**.  
    https://docs.openclaw.ai/concepts/context

17. OpenClaw Documentation. **Telegram channel**.  
    https://docs.openclaw.ai/channels/telegram

18. OpenClaw Documentation. **Automations**.  
    https://docs.openclaw.ai/automation/cron-jobs

> OpenClaw 相关能力边界按 **2026-08-16** 的官方文档核查。后续版本可能调整默认 Session、Memory、Compaction 或调度行为，部署时应以所安装版本的官方文档为准。

---

## License

MIT © xiaodou-system
