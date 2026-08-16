# xiaodou-system

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux-blue" alt="platform"/>
  <img src="https://img.shields.io/github/v/release/SHIJINS66/xiaodou-system?color=green&label=release" alt="release"/>
  <img src="https://img.shields.io/badge/channel-Telegram-lightgrey" alt="channel"/>
  <a href="README.en.md"><img src="https://img.shields.io/badge/lang-English-blue" alt="lang-en"/></a>
</p>

## 摘要

**xiaodou-system** 是一个构建在 [OpenClaw](https://docs.openclaw.ai) 之上的、面向长期陪伴场景的角色无关运行框架。系统以“自然日”为基本运行周期，将角色定义、每日生活规划、定时事件执行、主动消息发送、会话状态、日终记忆整理与跨日状态承接组织为一个闭环。其目标不是扩展语言模型的通用推理能力，而是为陪伴式智能体建立一套可审计、可恢复、可迁移的长期运行机制。

与典型的响应式对话系统不同，xiaodou-system 不把用户当前消息视为唯一的行为起点。角色在每日开始时根据身份、生活规律、近期记忆、天气与前一日状态生成当天生活轨迹；随后由确定性调度系统在未来时间点执行相应事件，并在满足交互条件时主动触达用户；日终再将完整会话、计划、事件执行结果与主动消息记录统一整理为下一自然日可直接使用的跨日状态。由此形成：

```text
角色定义与历史状态
        ↓
每日规划
        ↓
定时事件与主动交互
        ↓
完整当日会话与事件证据
        ↓
日终记忆编译
        ↓
受控 Session Rollover
        ↓
下一自然日
```

本项目采用“生成面与控制面分离”的设计。DeepSeek 与 Seedream 等生成模型负责计划、文本和图像内容；调度、Schema 校验、事件幂等、并发锁、事务状态、持久化与 Session 边界由 Python 脚本及 Linux 系统设施确定性控制。本文所称“确定性”仅指控制流程与状态迁移具有明确规则，并不意味着语言模型或图像模型的输出能够逐次复现。

长期记忆方面，系统区分**持久化（Persistence）**、**检索（Retrieval）**与**连续性（Continuity）**。一条历史记录被写入磁盘，并不意味着它一定会在下一次自然交互中被模型使用；即使历史可通过搜索重新获取，也不等价于角色能够在无需用户提醒的情况下自然承接前一日状态。为此，xiaodou-system 将当前自然日的完整 Session 作为高保真工作记忆，并规定跨日语义只通过项目自身的日终记忆流水线生成，从而避免同一历史同时被多套独立摘要机制解释。

现有研究已经分别验证了记忆、反思、规划与长期对话状态对持续型智能体的重要性 [1][2][3]。xiaodou-system 不提出新的模型算法，而是把这些要求落实为一个可部署的工程系统。当前实现以 **Linux + OpenClaw + Telegram** 为验证环境，默认使用 DeepSeek 作为文本模型、Seedream 作为图像生成服务。

**关键词：** 长期陪伴智能体；主动交互；跨日记忆；Session 生命周期；事件状态机；OpenClaw；确定性编排

---

## 一、研究背景与问题定义

### 1.1 从响应式对话到持续运行

大型语言模型已经能够生成连贯的多轮对话，但“能够对话”与“能够长期作为一个角色持续存在”属于不同的问题层级。

典型对话系统可以抽象为：

```text
User Message
    ↓
Model
    ↓
Response
```

这一结构默认每一轮行为都由用户消息触发，模型只需要解释当前上下文并产生回复。长期陪伴场景则额外要求系统处理时间、跨会话状态、主动行为、长期记忆以及角色自身的生活轨迹。用户没有发送消息时，角色仍然需要拥有当前时间、地点、活动和情绪等内部状态；用户第二天再次出现时，前一日的重要事件也应继续影响对话，而不是退化为只有在显式检索时才能恢复的历史档案。

Generative Agents 通过 memory、reflection 与 planning 组合展示了持续行为模拟的可行性 [1]；MemGPT 将长程交互中的 memory management 作为独立问题处理 [2]；LD-Agent 则进一步研究了长期对话中的 event memory 与动态 persona [3]。这些工作共同说明，长期智能体不能仅依赖当前对话窗口，而需要显式的时间与记忆结构。

### 1.2 陪伴场景中的五个工程约束

xiaodou-system 将长期陪伴运行问题归纳为以下五个约束。

#### 时间主动性（Temporal Proactivity）

系统必须能够在没有即时用户输入的情况下，根据当天计划产生未来事件，并在指定时间判断是否应主动触达。主动行为应来源于角色当天已经存在的生活状态，而不是每次到点后临时询问模型“现在要不要说点什么”。

#### 跨日连续性（Cross-day Continuity）

昨日的重要经历必须能够直接影响下一自然日的规划与交互。对于“我到了”“出来了”“结束了”一类语义极短、但高度依赖前序事件的消息，系统不能要求用户显式提出“去翻一下昨天发生了什么”以后才恢复正确上下文。

#### 单一记忆权威（Single Memory Authority）

同一段历史不应同时由多个相互独立的摘要系统长期解释。若 Session compaction、memory flush、日终记忆模型分别对同一段对话形成不同摘要，就可能在时间、因果、未完成事项、关系状态或信息优先级上产生差异。xiaodou-system 因此要求跨日语义只有一条正式转换路径。

#### 运行可控性（Operational Determinism）

事件是否重复执行、消息是否已经发送、Session 是否可以 rollover、记忆文件是否写入成功等问题必须由程序状态决定，而不能交由模型临场判断。

#### 角色可移植性（Role Portability）

人格、身份、生活规律和用户关系应以可读、可编辑的角色文件表达，而不是写死在执行脚本中。框架应在更换角色时保持核心运行协议不变。

---

## 二、系统模型与设计原则

### 2.1 角色模型

xiaodou-system 使用 workspace 中的 7 个核心 Markdown 文件定义角色：

| 文件 | 职责 |
|---|---|
| `AGENTS.md` | 运行规则与角色侧行为约束 |
| `IDENTITY.md` | 身份与稳定背景信息 |
| `SOUL.md` | 性格、表达方式、情绪与互动边界 |
| `LIFE.md` | 生活规律与 Planner 的日程生成依据 |
| `USER.md` | 陪伴对象及相关长期信息 |
| `TOOLS.md` | 工具与运行环境说明 |
| `MEMORY.md` | 经整理后的长期记忆 |

代码不根据角色姓名、经历或表达风格建立业务分支。角色差异进入生成阶段，调度、状态迁移和持久化逻辑保持一致。

其中，`LIFE.md` 描述稳定生活规律，`MEMORY.md` 描述跨日长期状态；二者共同参与新的 DailyPlan 构造。换一组角色文件即可得到不同角色，而无需修改主要流水线。

### 2.2 时间模型

系统把一个自然日视为一个明确的运行单元：

```text
Day N
├── DailyPlan
├── Scheduled Events
├── Executed Events
├── User Interactions
├── Working Session
└── Daily Memory
```

这意味着“时间”不是单纯的 cron 时间戳，而是角色状态的一部分。某个 17:45 事件并不只是“17:45 执行一个任务”，而是：

```text
今天的角色
    ↓
已经经历当天前序活动
    ↓
到达 17:45 对应生活事件
    ↓
根据当前状态决定是否与用户交互
```

DailyPlan 因此承担“当天世界状态”的角色，而不是单纯的任务清单。

### 2.3 记忆模型

系统区分两类记忆。

#### 工作记忆（Working Memory）

```text
当前自然日的完整 OpenClaw Session
```

其特点是：

- 保留当天原始交互；
- 保留主动触达及用户后续回应；
- 在日终前尽量不进行跨日语义压缩；
- 作为当天事实的高保真证据来源。

#### 跨日记忆（Consolidated Memory）

```text
Daily Memory + MEMORY.md
```

其特点是：

- 由 step04 统一生成；
- 表示已完成日终整理的历史状态；
- 直接参与下一自然日 Planner 与交互上下文；
- 周期性去重、合并与压缩。

两者之间只有一条正式转换路径：

```text
完整 Working Session
+ DailyPlan
+ Event Journal
+ 主动消息与用户互动
        ↓
step04
        ↓
Daily Memory / MEMORY.md
```

### 2.4 Persistence、Retrieval 与 Continuity

长期陪伴系统不能把“数据还在”直接等价为“角色还记得”。

```text
Persistence
= 信息是否仍被物理保存

Retrieval
= 当前运行是否能够重新找到该信息

Continuity
= 角色是否无需用户提醒即可自然使用该信息
```

这三个层级必须分别处理。

例如：

```text
昨日：
“我明天下午去考试。”

今日：
“我出门了。”
```

第二句话几乎不包含足够强的语义检索线索，但其合理解释依赖昨天的考试安排。若系统只有在用户追加“你去翻一下昨天我们说了什么”以后才恢复历史，那么 Persistence 与 Retrieval 可能都没有失败，但 Continuity 已经失败。

因此，xiaodou-system 把“昨日重要状态”作为下一自然日直接输入，而不是仅作为可搜索档案。

### 2.5 生成面与控制面

系统将生成能力与运行控制明确分开。

**生成面：**

- DailyPlan 内容生成；
- 消息文案生成；
- Daily Memory 内容生成；
- Seedream 图像生成。

**控制面：**

- 定时触发；
- Schema 校验；
- 幂等判断；
- `flock` 并发锁；
- 事务状态；
- 文件持久化；
- Session Rollover；
- 失败恢复。

其设计原则可概括为：

> **Generative intelligence inside deterministic boundaries.**

模型负责“生成什么”，程序负责“什么时候生成、是否允许执行、是否已经执行、结果写到哪里、失败后如何恢复”。

---

## 三、总体架构

### 3.1 架构组成

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system 架构图" width="880"/>
  <br/>
  <em>图 1：系统总体架构 —— 三层业务流水线与贯穿始终的持久层</em>
</p>

系统由四个层面构成：

- **角色契约层**：7 个 Markdown 文件；
- **业务流水线层**：step02、step03、step04；
- **外部依赖层**：LLM、图像服务、OpenClaw Gateway、消息通道；
- **基础设施与持久层**：Linux、`cron`、`at`、`flock`、daily、chatlog、MEMORY、日志和备份。

### 3.2 外部依赖抽象

第三方服务统一封装于 `providers/`：

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

业务逻辑依赖 provider 接口，而不直接依赖某个模型供应商或消息服务的具体 SDK。这样可以在保持 DailyPlan、Event Model 和 Memory Pipeline 不变的情况下替换外部实现。

### 3.3 持久化状态

主要持久化对象包括：

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

系统优先将关键状态写入结构化文件或日志，而不是只保存在当前模型上下文中。

---

## 四、自然日运行机制

### 4.1 step02：每日规划

**默认触发时间：06:00**

主要脚本：

- `build_daily_plan.py`
- `validate_daily_plan.py`
- `initialize_daily_file.py`
- `schema_engine.py`
- `schema_validator.py`
- `weather_provider.py`

输入：

```text
角色核心文件
+ 当日天气
+ 最近 7 天记忆
+ 昨日执行结果
```

输出：

```text
daily/YYYY-MM-DD.json
```

DailyPlan 描述：

- 当天活动；
- 地点；
- 情绪状态；
- 可触达窗口；
- silent / non-silent 事件；
- 可能需要的自拍等媒体行为。

模型生成结果必须通过 JSON Schema 校验后才能进入后续阶段。Schema 保证的是结构契约有效，不保证文本语义完全正确，也不保证相同输入必然生成相同计划。

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

单个事件到达触发时间后按以下路径运行：

```text
读取 DailyPlan 与上下文
        ↓
幂等检查
        ↓
获取 flock
        ↓
构造 Context Snapshot
        ↓
判断当前事件是否仍应执行
        ↓
生成消息
        ↓
可选：Seedream 生成图像
        ↓
发送 Telegram
        ↓
写入 OpenClaw Session
        ↓
Event Journal / Transaction 回写
```

事件不是一个单一的“成功 / 失败”布尔值。生成、发送、注入和最终提交分别记录状态，以支持部分失败恢复。

例如：

```text
delivery = succeeded
injection = failed
transaction = partial
```

此时恢复逻辑只能补做 Session 注入，而不能重新发送 Telegram 消息，否则可能产生重复触达。

### 4.3 step04：日终记忆编译

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

日终输入包括：

```text
完整当日 Session
+ DailyPlan
+ 实际事件执行状态
+ Event Journal
+ 主动消息
+ 用户回应
+ 当日产物
```

其输出并不是简单聊天摘要，而是一个 episode-level 的跨日状态表示。系统需要回答：

- 今天计划了什么；
- 哪些事件实际发生；
- 哪些事件被 suppress；
- 角色主动说了什么；
- 用户如何回应；
- 哪些状态仍然影响下一天；
- 哪些信息应该进入长期 MEMORY。

### 4.4 Session Rollover

xiaodou-system 将每日 Session 视为当天高保真工作记忆。

Rollover 必须位于主要记忆处理之后：

```text
Day N 完整 Session
        ↓
Daily Memory
        ↓
MEMORY 更新
        ↓
主要持久化完成
        ↓
Session Rollover
        ↓
Day N+1
```

其关键约束是：

> **Memory processing precedes rollover.**

Rollover 的目的不是遗忘，也不是因为 Session “过期”，而是建立明确的自然日边界，并在长 Session 进入不可控的历史摘要之前，把当天完整证据交给唯一的跨日 Memory Pipeline。

### 4.5 跨日闭环

```text
昨日 Consolidated Memory
        ↓
今日 Planner
        ↓
DailyPlan
        ↓
Scheduled Events
        ↓
Actual Events + Conversation
        ↓
Working Session
        ↓
Daily Memory Compiler
        ↓
新的 Consolidated Memory
        ↓
Rollover
        ↓
下一自然日
```

Daily Memory 因此不仅用于保存过去，更用于构造未来状态。

---

## 五、OpenClaw 能力边界与适配性分析

### 5.1 OpenClaw 的基础能力

截至 2026-08-16，OpenClaw 已经具备与本项目高度相关的通用基础设施，包括：

- Agent workspace 与上下文文件 [4]；
- Gateway-owned Session 与 transcript [5][6]；
- Markdown memory、memory search 与 memory-core [7]；
- Dreaming 与 consolidation [8]；
- Active Memory [9]；
- Compaction 与 automatic memory flush [7][10]；
- Telegram 等消息通道 [11]；
- Automations 调度器 [12]。

因此，xiaodou-system 不重新实现通用 Agent Runtime。其新增部分位于更高的应用语义层：

```text
OpenClaw
提供 Agent Runtime 原语

        ↓

xiaodou-system
定义陪伴角色的自然日生命周期
```

两者的关系不是“OpenClaw 缺少功能，因此重新造一套功能”，而是“OpenClaw 的通用机制无法自动推导出陪伴角色所需的时间、事件与跨日状态约束”。

### 5.2 原生 Memory 与跨日连续性

OpenClaw 的 memory files 可以持久化并被 `memory_search` / `memory_get` 检索；Active Memory 也能在符合条件的交互中主动召回历史 [7][9]。

这一模型对于通用 Agent 是合理的：只有当前任务与历史相关时才检索，可以减少上下文占用与无关信息干扰。

陪伴场景的约束不同。

大量自然语言承接并不包含明确的 recall intent。例如：

```text
昨日：
“我明天下午要去考试。”

今日：
“我出门了。”
```

当前消息并没有显式要求系统回忆昨天，也不一定形成高质量 semantic query；但对陪伴角色而言，昨天的考试安排已经属于当前关系状态的一部分。

因此，xiaodou-system 不把“memory search 最终可以找到昨天”视为跨日连续性的充分条件。近期关键状态必须在用户当前消息到来之前已经进入当日工作状态。

形式上：

```text
OpenClaw 通用检索路径：

Current Query
    ↓
判断是否需要历史
    ↓
Search
    ↓
Past Memory


xiaodou-system 近期连续性路径：

Past Episode
    ↓
Current State
    ↓
Interpret Current Query
```

二者并不冲突。远期历史仍可使用 OpenClaw memory search；但昨日的重要事件不依赖临时检索才能成立。

### 5.3 Session Reset 与尚未完成的记忆提交

Session transcript 的物理存在不等价于角色当前能够使用这些内容。

即使旧历史仍保存在 transcript、SQLite 或其他归档中，如果新 Session 中没有相应状态，用户仍可能看到：

```text
昨天：
用户已经说明今天要去医院。

今天：
用户：“我到了。”

角色：
“到了哪里？”
```

从底层存储角度，历史可能没有丢失；从陪伴交互角度，连续性已经中断。

因此，xiaodou-system 不把“旧 transcript 可恢复”作为正常记忆机制，而只把它视为审计与灾难恢复能力。正常跨日状态必须在 Session 边界之前完成显式提交。

### 5.4 Compaction 与双重摘要权威

OpenClaw 的 compaction 用于控制长 Session 的上下文规模。较早的对话会被总结为持久化 compaction entry，随后以：

```text
Compaction Summary
+
近期未压缩消息
```

继续当前 Session [6][10]。

在通用 Agent 中，这是一种必要的上下文管理机制。

xiaodou-system 已经存在另一条跨日摘要链：

```text
完整 Session
+ DailyPlan
+ Event Journal
+ 主动消息
+ 用户回应
        ↓
step04
        ↓
Daily Memory / MEMORY.md
```

如果允许同一段历史先经过 OpenClaw compaction，再经过 xiaodou-system Memory Compiler，则后续模型可能同时读取：

```text
OpenClaw Compaction Summary
            +
xiaodou Daily Memory
```

二者即使都没有明显事实错误，也可能在细节保留、时间顺序、事件因果、未完成事项、关系状态和重要性判断上产生不同取舍。

这种问题不是传统意义上的“数据丢失”，而是**语义权威分裂**：

```text
同一历史
    ├── Summary A
    └── Summary B

两份摘要同时成为未来推理依据
```

陪伴系统需要稳定的人际关系状态，因此这种不确定性不可接受。

xiaodou-system 采用 **Single Memory Authority** 原则：

> 当天完整 Session 作为原始工作证据；跨日 consolidated memory 只由 step04 正式生成。

这并不意味着 OpenClaw compaction 本身错误，而是其职责与本项目的跨日 Memory Compiler 重叠。如果两者同时承担 canonical history，就会形成不必要的语义冲突。

### 5.5 Daily Rollover 的必要性

当前 OpenClaw 可以维持持续 Session，也支持可配置的 daily / idle reset [5][6]。

xiaodou-system 仍主动执行 daily rollover，是为了在以下两个目标之间建立确定边界：

1. **当天尽量保留完整原始 Session；**
2. **跨日后只使用已经经过项目自身 Memory Pipeline 的状态。**

如果 Session 无限持续：

```text
Day 1
+ Day 2
+ Day 3
+ Day 4
...
```

最终必然受到 context window 约束，并进入 compaction。

如果每天在记忆完成前直接 reset，则又可能导致当天尚未进入 consolidated memory 的信息退出当前工作上下文。

因此采用：

```text
完整单日 Session
        ↓
Memory Commit
        ↓
Rollover
```

Rollover 实际上构成一个 **Memory Commit Boundary**。

### 5.6 Automations 与 Daily Event Model

OpenClaw Automations 已经能够执行一次性和周期性任务 [12]。

但调度器解决的是：

```text
何时运行某个任务
```

而 xiaodou-system 需要定义的是：

```text
这个事件在角色今天的生活里意味着什么
```

例如同样是 17:45：

```text
Scheduler:
17:45 execute job A
```

而 Daily Event Model 还包含：

```text
activity
location
mood
interaction_window
silent
selfie
previous_state
next_state
relationship_context
```

因此 Automations 可以替换当前部分 `cron + at` 后端，但无法替代 DailyPlan 与 Event Model。

当前版本使用 `cron + at`，是因为其运行链已经围绕：

```text
Linux process
+ file state
+ flock
+ event journal
+ transaction
```

完成验证，并且调度状态可以在 Gateway 之外独立检查。

未来可以增加 OpenClaw Automations adapter，只要 DailyPlan、Event Model 和 Memory Pipeline 的语义保持不变。

### 5.7 Active Memory 与近期工作状态

Active Memory 改善了传统纯按需检索的问题，但它仍属于“当前交互发生以后，根据当前输入决定召回什么”的机制 [9]。

xiaodou-system 的近期记忆目标更严格：

> 用户消息到来以前，昨日关键状态已经成为今天的运行状态。

因此两者适用于不同层级：

```text
xiaodou recent-state continuity
负责：昨日 / 今日关键状态

OpenClaw Active Memory / memory search
负责：更远期、按主题、按语义补充召回
```

这是一种互补关系，而不是替代关系。

### 5.8 适配结论

OpenClaw 已经解决了 Session、Memory、Search、Compaction、Channel、Automation 等通用 Agent Runtime 问题，但这些能力默认并不包含以下应用层约束：

```text
自然日作为状态边界
DailyPlan 作为当天世界状态
事件属于角色生活而非单纯后台任务
昨日关键状态必须直接参与今日
单日 Session 尽量保留高保真原文
跨日历史只允许一个 canonical compiler
Memory Commit 必须先于 Rollover
```

xiaodou-system 的必要性由这些约束共同构成，而不是由 OpenClaw 某一个具体功能缺失决定。

---

## 六、可靠性与状态一致性

### 6.1 数据契约

项目使用 JSON Schema 对跨阶段数据进行结构约束，目前包含 19 个 Schema。

Schema 用于检测：

- 必需字段缺失；
- 类型错误；
- 枚举值越界；
- 不符合协议的模型输出。

Schema 不能保证自然语言事实正确，也不能保证不同模型调用生成相同内容。

### 6.2 幂等与并发控制

step03 通过：

- 稳定事件标识；
- 幂等状态检查；
- `flock`；
- Event Journal；
- Transaction State；

避免 cron 重入、`at` 重复触发或人工补跑导致重复发送。

### 6.3 外部副作用事务

消息发送与 Session 注入属于两个独立副作用。

若：

```text
Telegram send = succeeded
chat.inject = failed
```

事件不能整体重试，否则可能重复发送。

系统因此分别记录：

```text
generated
delivered
injected
committed
```

恢复逻辑根据已完成阶段决定补偿操作。

### 6.4 备份与审计

项目提供：

- `backup_openclaw.py`
- `raw_backup.py`
- `rollover_artifacts.py`

备份覆盖运行配置、长期记忆、聊天记录和相关事件产物。

备份的作用是保留证据与灾难恢复材料，而不是替代正常的记忆连续性机制。

---

## 七、运行验证

当前验证结论仅表示 Linux 环境中的主要运行链路已经贯通，不构成聊天质量、长期 SLA 或跨平台性能证明。

### 7.1 Telegram 交互

<p align="center">
  <img src="docs/screenshot-telegram.jpg" alt="正常运行状态下的 Telegram 对话效果" width="800"/>
  <br/>
  <em>图 2-a：角色在 Telegram 上的日常对话与自拍</em>
</p>

当前实现能够根据 DailyPlan 触发主动事件，在策略允许时生成文本及可选图像，并通过 Telegram 发送。

### 7.2 文件持久化

<p align="center">
  <img src="docs/filesystem-directories.jpg" alt="框架自动沉淀的 daily / chatlog / daily_selfies 目录" width="800"/>
  <br/>
  <em>图 2-b：框架自动沉淀的 daily / chatlog / daily_selfies 数据目录</em>
</p>

运行过程中：

```text
daily/
```

保存计划与事件状态；

```text
chatlog/
```

保存聊天记录；

```text
daily_selfies/
```

保存图像产物；

```text
MEMORY.md
```

保存经过整理的长期状态。

这些文件同时构成日终记忆与故障排查的主要输入。

---

## 八、部署

### 8.1 环境要求

当前验证环境为 **Linux**：

- Python 3.10+
- `at` / `atd`
- `cron`
- `flock`
- 默认时区：`Asia/Shanghai`
- 已安装并完成初始化的 [OpenClaw](https://docs.openclaw.ai)
- Python 依赖：

```bash
pip install -r requirements.txt
```

### 8.2 OpenClaw 初始化

```bash
openclaw configure
openclaw health
```

至少需要完成：

- 可用模型/provider 配置；
- Gateway 启动与健康检查；
- Telegram channel；
- workspace / session 对应关系。

如果默认端口被占用，可配置其他 Gateway 端口，例如：

```bash
openclaw config set gateway.port 19203
```

### 8.3 xiaodou-system 初始化

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` 执行：

1. 环境检查；
2. 核心角色文件准备；
3. `settings.yaml` 生成；
4. 运行目录创建；
5. 脚本与配置校验；
6. 可选 cron 安装。

默认实例目录：

```text
~/.openclaw/workspace
```

可通过 `--instance-dir` 指定其他位置。

已有角色文件不会被初始化过程主动覆盖；仅缺失文件会创建模板或骨架。

### 8.4 部署向导

```bash
python3 scripts/guided_setup.py
```

向导处理：

1. Python / atd / OpenClaw / 时区检查；
2. 核心文件检查；
3. API key 配置；
4. Telegram token 与 chat id 绑定；
5. Gateway Session 绑定；
6. xiaodou-system 记忆契约配置；
7. 最终校验；
8. Gateway 配置生效。

可用模式：

```text
--non-interactive
--verify-only
```

### 8.5 定时任务

```bash
bash init.sh --instance-dir ./instance --install-cron
```

当 `ALLOW_CRON_APPLY=1` 时：

- root 用户写入 `/etc/crontab`；
- 普通用户写入用户级 crontab。

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

### 8.6 配置

运行配置集中于：

```text
settings.yaml
```

主要配置域：

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

角色人格与生活内容应保留在 Markdown 文件中，而不是写入 Python 控制逻辑。

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

运行实例还会产生：

```text
daily/
chatlog/
daily_selfies/
event journal
transaction state
backups/
```

---

## 十、局限与后续工作

### 10.1 验证范围

当前系统只在 Linux 环境完成端到端验证。

尚未建立：

- 多发行版兼容矩阵；
- Windows / macOS 调度适配；
- 长周期故障率与 SLA 数据；
- 主动消息质量评测基准；
- 不同角色之间的系统化迁移测试。

### 10.2 单日 Context Budget

系统希望在一个自然日内尽量保持完整、未 compact 的 Session，但模型上下文窗口仍存在硬上限。

若单日消息量、工具输出或主动事件异常密集，仍可能在 rollover 前触发 compaction。当前版本因此不能把“每日 rollover”解释为对 compaction 的绝对消除。

高交互量场景后续需要：

- 单日 context budget 监控；
- 受控 day-internal checkpoint；
- 或专门的 context adapter。

### 10.3 平台依赖

当前控制面依赖：

```text
cron
at / atd
flock
```

Windows 与 macOS 需要替换平台相关 scheduler / lock adapter，同时保持 DailyPlan、Event Model 与 Memory Pipeline 的数据契约不变。

### 10.4 消息通道

当前正式验证通道为 Telegram。

其他消息通道需要单独验证：

- 文本发送；
- 媒体发送；
- 身份绑定；
- Session 映射；
- 主动消息写回。

### 10.5 外部模型与服务

DeepSeek 与 Seedream 属于外部服务。模型名称、API、价格、速率限制与可用性可能变化，生产部署应通过 provider 配置管理。

---

## 十一、结论

xiaodou-system 将长期陪伴智能体建模为一个以自然日为边界的持续运行系统，而不是一个仅由用户输入驱动的对话接口。

其核心状态转换为：

```text
角色与历史状态
        ↓
DailyPlan
        ↓
Scheduled / Executed Events
        ↓
Working Session
        ↓
Daily Memory Compiler
        ↓
Consolidated Memory
        ↓
Session Rollover
        ↓
下一自然日
```

系统的主要工程贡献不在于重新实现 OpenClaw，也不在于提出新的语言模型算法，而在于明确了四个长期陪伴运行约束：

1. **角色内容与运行代码分离；**
2. **生成模型与确定性控制面分离；**
3. **Working Session 与 Consolidated Memory 分离；**
4. **同一段历史只允许一条 canonical 的跨日语义转换路径。**

OpenClaw 提供 Session、Memory、Search、Compaction、Channel 与 Automation 等通用 Agent Runtime 能力；xiaodou-system 在这些原语之上进一步定义时间模型、DailyPlan、事件状态机、主动交互事务以及跨日记忆协议。

对于陪伴式智能体而言，真正需要维持的并不是“历史数据仍然存在”，而是角色能够在不依赖用户显式提醒的情况下，将昨天自然地带入今天。

---

## 参考资料

1. Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. **Generative Agents: Interactive Simulacra of Human Behavior**. arXiv:2304.03442, 2023.  
   https://arxiv.org/abs/2304.03442

2. Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S. G., Stoica, I., & Gonzalez, J. E. **MemGPT: Towards LLMs as Operating Systems**. arXiv:2310.08560, 2023.  
   https://arxiv.org/abs/2310.08560

3. Li, H., Yang, C., Zhang, A., Deng, Y., Wang, X., & Chua, T.-S. **Hello Again! LLM-powered Personalized Agent for Long-term Dialogue**. arXiv:2406.05925, 2024.  
   https://arxiv.org/abs/2406.05925

4. OpenClaw Documentation. **Agent workspace / Context**.  
   https://docs.openclaw.ai/concepts/agent-workspace  
   https://docs.openclaw.ai/concepts/context

5. OpenClaw Documentation. **The main session / Session management**.  
   https://docs.openclaw.ai/concepts/main-session  
   https://docs.openclaw.ai/concepts/session

6. OpenClaw Documentation. **Session management & compaction**.  
   https://docs.openclaw.ai/reference/session-management-compaction

7. OpenClaw Documentation. **Memory**.  
   https://docs.openclaw.ai/concepts/memory

8. OpenClaw Documentation. **Dreaming**.  
   https://docs.openclaw.ai/concepts/dreaming

9. OpenClaw Documentation. **Active Memory**.  
   https://docs.openclaw.ai/concepts/active-memory

10. OpenClaw Documentation. **Context / Compaction**.  
    https://docs.openclaw.ai/concepts/context

11. OpenClaw Documentation. **Telegram channel**.  
    https://docs.openclaw.ai/channels/telegram

12. OpenClaw Documentation. **Automations**.  
    https://docs.openclaw.ai/automation/cron-jobs

> OpenClaw 相关能力边界按 **2026-08-16** 的官方文档核查。后续版本可能调整默认 Session、Memory、Compaction 或调度行为，部署时应以所安装版本的官方文档为准。

---

## License

MIT © xiaodou-system
