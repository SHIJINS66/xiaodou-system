# xiaodou-system

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux-blue" alt="platform"/>
  <img src="https://img.shields.io/github/v/release/SHIJINS66/xiaodou-system?color=green&label=release" alt="release"/>
  <img src="https://img.shields.io/badge/channel-Telegram-lightgrey" alt="channel"/>
  <a href="README.en.md"><img src="https://img.shields.io/badge/lang-English-blue" alt="lang-en"/></a>
</p>

## 摘要

**xiaodou-system** 是一个构建在 [OpenClaw](https://docs.openclaw.ai) 之上的、面向长期陪伴场景的**角色无关运行框架**。系统将一个自然日拆分为三个可独立检查的阶段：**每日规划（step02）→ 定时调度与主动触达（step03）→ 日终记忆整理（step04）**。角色内容由 workspace 中的 Markdown 文件描述；调度、校验、状态迁移、持久化和失败恢复由 Linux 定时设施与 Python 脚本负责。

本项目关注的不是提高语言模型本身的推理能力，而是解决长期陪伴智能体的运行问题：如何让角色在没有即时用户输入时仍具有可执行的日程；如何把主动消息纳入可追踪的事件状态；如何让前一日的重要经历在下一日**无需用户主动要求检索即可参与当前交互**；以及如何避免会话压缩、长期记忆提炼和 session 生命周期分别产生互不一致的历史表示。

这里需要区分三个概念：**记忆被持久化（Persistence）并不等于记忆会被检索（Retrieval），记忆可被检索也不等于角色具有连续性（Continuity）**。OpenClaw 当前已经提供 memory files、memory search、Active Memory、session transcript、compaction、Dreaming 与 Automations 等通用能力 [4–10]；xiaodou-system 并不重新实现这些基础设施，而是在其上增加一个面向陪伴角色的**自然日生命周期与单一记忆权威（single memory authority）**：当日完整 session 作为高保真工作记忆，跨日记忆只由项目自己的 step04 生成；随后再进行受控 rollover。

现有研究已经分别展示了基于大语言模型的代理在**记忆、反思、规划和跨会话长期交互**方面的可行性：Generative Agents 将记忆、反思与规划用于持续行为模拟 [1]；MemGPT 通过分层记忆管理支持长程对话 [2]；LD-Agent 进一步研究了长期对话中的事件记忆与动态 persona 建模 [3]。xiaodou-system 不试图替代这些模型层研究，而是提供一套可部署的工程实现，将角色配置、日程生成、系统级调度、主动消息、完整会话证据与日终记忆组织为固定流水线。

当前实现以 **Linux + OpenClaw + Telegram** 为验证环境，默认使用 DeepSeek 作为文本模型、Seedream 作为图像生成服务。外部服务通过 `providers/` 统一封装；运行数据通过 JSON Schema、YAML 配置、事件日志和 Markdown 记忆文件落盘。语言模型与图像模型仍然是生成式组件，因此本文所称的“确定性”仅指**控制流程、数据契约和状态管理的确定性**，不表示模型文本或图像输出具有逐次一致性。当前结论来自单一部署环境的端到端运行验证，不代表跨平台性能、模型质量或生产级 SLA。

## 一、问题定义与设计范围

### 1.1 长期陪伴智能体的工程问题

大语言模型可以生成连贯的多轮对话，但“能够对话”与“能够长期运行”并不是同一问题。长期智能体通常还需要显式处理时间、跨会话记忆、行为计划以及外部状态。相关研究已经表明，记忆检索、反思和规划会直接影响长期代理的行为连续性 [1][2]；针对长期对话的工作也将事件记忆与 persona 管理作为独立模块进行建模 [3]。

xiaodou-system 将问题限定为五个工程目标：

1. **时间主动性（Temporal Proactivity）**  
   在没有即时用户消息的情况下，系统仍能依据当天计划产生未来事件，并在预定时间决定是否触达用户。

2. **跨日连续性（Cross-day Continuity）**  
   昨日的重要事件不应仅仅“存在于某个可搜索的文件或 transcript 中”，而应在下一自然日直接影响 Planner、事件判断和对当前短句的解释。对于陪伴式交互，`Persistence ≠ Retrieval ≠ Continuity`。

3. **单一记忆权威（Single Memory Authority）**  
   同一段历史不应同时由多个互不协调的摘要系统生成并长期参与推理。当天的完整 session 作为原始工作记忆；跨日 consolidated memory 只由 xiaodou-system 的日终流水线生成。

4. **运行可控性（Operational Determinism）**  
   调度、校验、幂等、锁、状态回写和归档应由确定性程序控制。生成模型负责生成内容，但不负责决定关键文件是否写入、同一事件是否重复执行等控制逻辑。

5. **角色可移植性（Role Portability）**  
   人格、身份、生活规律和用户关系应尽可能以 workspace 文件表示，而不是硬编码到 Python 脚本中。更换角色时，原则上只替换角色契约和资源配置。

### 1.2 与 OpenClaw 的关系

OpenClaw 已经提供完整的智能体运行基础，包括 workspace、Gateway-owned sessions、持久化 memory、memory search、Active Memory、Telegram 等消息通道，以及内建 Automations 调度器 [4–10]。因此，xiaodou-system **不是对 OpenClaw 的重新实现**。

本项目增加的是一套特定于“每日生活轨迹 + 主动触达 + 日终记忆”的运行协议：

- 用固定 step02 / step03 / step04 阶段组织一个自然日；
- 用 `daily/YYYY-MM-DD.json` 作为当天计划与执行状态的显式数据对象；
- 用 JSON Schema 对生成结果进行结构校验；
- 用 Linux `cron` 与 `at` 作为当前实现的外部调度层；
- 用幂等门、`flock`、事务回写和事件日志管理重复执行与并发；
- 把当前自然日的 session 视为高保真工作记忆；
- 用项目自定义的 step04 作为**唯一跨日记忆编译路径**；
- 在日终记忆已经完成主要持久化步骤之后，再执行受控 session rollover。

OpenClaw 当前的详细 daily notes 在普通轮次中主要通过 `memory_search` / `memory_get` 按需读取 [6]；Active Memory 虽可在交互路径中主动召回长期信息，但默认 `escalate` 模式仍以强触发命中或“当前消息具有回忆意图”为主要启动条件 [7]。这对通用 Agent 是合理的成本与延迟折衷，但无法保证陪伴场景中诸如“我到了”“出来了”“结束了”这类高度依赖昨日语境、却缺乏明确检索线索的短句一定获得所需历史。

当前 OpenClaw 的默认 AGENTS 模板要求新 session 开始时读取 today + yesterday 的 daily notes 与 `MEMORY.md` [8]，而 `/new` / `/reset` 也可以在新 session 的首轮重新提供近期 daily memory [9]。这些机制改善了 session 边界后的恢复，但它们仍依赖于相关内容**已经被正确写入 daily memory**，并不能替代对“当天完整会话何时、由谁、以何种规则被编译为跨日状态”的应用层控制。

### 1.3 非目标

当前版本不讨论以下问题：

- 不声称角色具有人类意义上的意识或真实生活；
- 不保证 LLM / 图像模型输出确定；
- 不提供跨平台兼容性保证；
- 不以聊天质量基准、情感陪伴效果或用户研究结果作为当前结论；
- 不将本项目的 7 文件角色契约等同于 OpenClaw 的默认 workspace 规范；
- 不否定 OpenClaw 原生 memory / compaction / Active Memory 的通用价值；本项目只说明它们与陪伴式角色的记忆消费策略存在不同目标。

## 二、系统设计

### 2.1 角色契约与运行框架分离

xiaodou-system 将角色定义集中在 workspace 的 7 个核心 Markdown 文件中：

| 文件 | xiaodou-system 中的职责 |
|---|---|
| `AGENTS.md` | 运行规则与角色侧行为约束 |
| `IDENTITY.md` | 身份与稳定背景信息 |
| `SOUL.md` | 性格、表达方式、情绪与互动边界 |
| `LIFE.md` | 生活规律与 Planner 的日程生成依据 |
| `USER.md` | 陪伴对象及相关长期信息 |
| `TOOLS.md` | 工具与运行环境说明 |
| `MEMORY.md` | 经整理后的长期记忆 |

这 7 个文件构成 **xiaodou-system 的角色契约**。其中部分文件同时属于 OpenClaw 的 workspace / memory 体系；`LIFE.md` 等内容则由本项目自己的 Planner 与脚本读取。OpenClaw 官方文档目前默认注入的 workspace bootstrap 文件集合与本项目的“7 文件契约”并不完全相同 [4]，因此两者不应混为一谈。

系统代码原则上不根据具体角色姓名、经历或表达风格分支。角色差异进入生成阶段，调度与持久化逻辑保持一致。

### 2.2 控制面与生成面分离

系统将运行职责分为两类：

- **生成面**：DeepSeek 负责计划、文本和记忆相关生成；Seedream 负责可选的自拍图像生成。
- **控制面**：Python 脚本、JSON Schema、`cron`、`at`、`flock`、事件日志和事务状态负责决定何时执行、输入是否合法、事件是否已执行以及结果如何落盘。

这一划分的目的不是消除生成模型的不确定性，而是把不应由模型决定的控制逻辑移出模型上下文。

### 2.3 外部依赖抽象

所有外部服务通过 `providers/` 封装。当前实现包括：

- `providers/llm/deepseek_urllib.py`
- `providers/llm/openai_compatible.py`
- `providers/image/seedream.py`
- `providers/openclaw_gateway.py`
- `providers/gateway_history.py`
- `providers/gateway_sessions.py`
- `providers/telegram.py`
- `providers/base.py`

业务脚本只依赖 provider 接口，不直接在核心流程中散布第三方 API 细节。该设计降低了替换模型、消息通道或 Gateway 对接实现时的修改范围。

### 2.4 记忆消费契约与单一记忆权威

xiaodou-system 不把“文件已经写入磁盘”视为记忆连续性的充分条件。系统区分三种状态：

```text
Persistence  记忆是否仍然存在
Retrieval    当前运行是否能够找到它
Continuity   角色是否会在无需用户提醒的情况下自然使用它
```

对于通用 Agent，按需检索通常足够；对于陪伴式角色，近期经历往往必须先于用户当前问题进入解释环境。例如用户昨日说明“明天下午考试”，今日只发送“我出门了”。当前消息本身并不构成强检索 query，但它的合理解释高度依赖昨日状态。

因此，本项目采用两层记忆职责：

```text
工作记忆（Working Memory）
= 当前自然日的完整 OpenClaw session
= 保留当天原始对话与主动消息

跨日记忆（Consolidated Memory）
= daily memory + MEMORY.md
= 仅由 xiaodou-system step04 从当天证据中生成
```

更远期、未进入近期工作集的信息仍可通过 OpenClaw memory search 等机制补充召回；但**昨日重要状态不应依赖用户显式提出“你去翻一下昨天”才能进入当前推理**。

这一设计同时规定了历史表示的权威路径：

```text
完整 session
    + DailyPlan
    + Event Journal
    + 主动消息记录
            ↓
      xiaodou step04
            ↓
   DailyMemory / MEMORY.md
            ↓
        下一自然日
```

项目不希望同一段跨日历史同时由 OpenClaw compaction summary 和 xiaodou-system Memory Compiler 作为两个长期语义来源。两个摘要即使都“基本正确”，仍可能在事件细节、未完成事项、时间、关系状态或信息优先级上产生差异，从而形成双重记忆权威。

---

## 三、系统架构

### 3.1 总体架构

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system 架构图" width="880"/>
  <br/>
  <em>图 1：系统总体架构 —— 三层业务流水线与贯穿始终的持久层</em>
</p>

系统由三条按时间串联的业务流水线构成。`providers/` 位于外部服务边界；workspace、daily、chatlog、memory、事件日志和配置文件构成持久化状态；Linux 定时设施负责触发确定性脚本。

### 3.2 step02：每日规划

**触发时间：06:00**

主要脚本：

- `build_daily_plan.py`
- `validate_daily_plan.py`
- `initialize_daily_file.py`
- `schema_engine.py`
- `schema_validator.py`
- `weather_provider.py`

输入主要包括：

- 7 个角色核心文件；
- 当日天气；
- 最近 7 天记忆；
- 昨日执行结果。

输出为：

```text
daily/YYYY-MM-DD.json
```

该文件描述当天的活动、地点、情绪状态以及可触达窗口。模型生成结果必须通过对应 JSON Schema 后才能进入后续阶段；验证失败时按既定策略重试。这里保证的是**结构契约有效**，而不是保证两次模型调用生成相同计划。

### 3.3 step03：调度与事件执行

**晨间调度触发：06:20**

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

执行过程分为两个阶段：

1. 晨间脚本读取当日计划，将需要执行的非 silent 事件经 `at_adapter.py` 提交给系统 `at`；
2. 到达目标时间后，由 `execute_daily_event.py` 执行单个事件事务。

单个事件的典型链路为：

```text
读取计划与上下文
    ↓
幂等检查 / 获取锁
    ↓
评估当前事件是否仍应发送
    ↓
生成消息文本
    ↓
可选：调用 Seedream 生成图像
    ↓
Telegram 发送
    ↓
写入 OpenClaw 会话上下文
    ↓
事务状态与事件日志回写
```

关键约束包括：

- 稳定事件标识；
- 幂等防重；
- `flock` 互斥；
- 发送结果与失败状态回写；
- 事件级日志；
- 已生成、已发送、已注入等状态分离记录。

### 3.4 step04：日终记忆与 Session Commit Boundary

step04 将一个自然日结束后的原始证据整理为下一自然日可以直接消费的长期状态。当前默认时序为：

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

其中，日终输入不只包含聊天文本，而包括：

- 当前自然日的完整 session；
- `daily/YYYY-MM-DD.json` 中的计划与执行状态；
- step03 event journal / transaction state；
- 已实际发送的主动消息；
- 用户后续回复；
- 当日图像等可关联产物。

其职责按顺序覆盖：

1. 汇总前一日对话与事件，生成 daily memory；
2. 将新增信息增量整理到长期记忆；
3. 将符合条件的结果写入 `MEMORY.md`；
4. 在前述跨日记忆处理之后进行 session rollover；
5. 周期性压缩长期记忆正文，使其维持在项目规定的容量范围内。

这里的 `session_rollover` 不应理解为普通“清理历史”操作。它承担的是 **Memory Commit Boundary**：当前 session 被视为一个自然日内的高保真工作记忆；只有在日终流水线已经完成主要记忆持久化步骤之后，才进入新的 session。这样做的目标是让“当天完整会话 → xiaodou-system Memory Compiler”成为跨日历史的唯一正式转换路径。

OpenClaw 当前默认不再自动 daily reset；daily / idle reset 属于可选策略 [9][10]。xiaodou-system 仍然保留每日 rollover，是主动的应用层选择：与其让 session 长期增长并最终依赖 compaction 摘要继续运行，本项目更倾向于在单日范围内保留完整会话，然后在明确的日边界完成自己的记忆提炼。

OpenClaw compaction 会把较早历史总结为一个持久化 `compaction` entry，后续模型看到的是该摘要与近期未压缩消息 [10][11]；compaction 前还可触发 automatic memory flush，将模型判断的重要信息写入 memory 文件 [5]。这些机制适合通用长会话，但如果同时启用 xiaodou-system 自己的 daily memory compiler，就可能形成两套相互独立的历史摘要。项目因此把**避免跨日双重摘要权威**作为 session 生命周期设计的重要约束。

### 3.5 持久化与审计

主要持久化对象包括：

```text
workspace/
MEMORY.md
daily/
daily/YYYY-MM-DD.json
daily memory
chatlog/
settings.yaml
event journal / transaction state
backups / rollover artifacts
```

系统设计要求关键状态优先以文件或结构化数据记录，而不是仅存在于当前模型上下文中。这样可以在进程退出、session rollover 或单次模型调用失败后重新判断运行状态。

---

## 四、确定性边界与故障处理

### 4.1 数据契约

项目使用 JSON Schema 约束跨阶段数据。目前包含 19 个 Schema，用于限制计划、事件与相关中间产物的结构。

Schema 校验解决的是：

- 必需字段缺失；
- 类型错误；
- 枚举值越界；
- 不符合协议的模型输出。

Schema 不负责判断文本内容是否“真实”或“自然”。

### 4.2 幂等与并发

step03 对事件执行增加稳定标识、状态检查与 `flock`。其目标是在 cron 重入、`at` 重复触发或人工补跑时，尽量避免同一逻辑事件被重复发送。

### 4.3 事务状态

发送消息属于外部副作用，因此事件执行不能简单以“脚本是否退出 0”表示成功。系统分别记录生成、发送、注入和回写等步骤，使失败后能够识别已经发生的副作用，并据此决定重试边界。

### 4.4 备份、恢复与 rollover

项目提供：

- `backup_openclaw.py`
- `raw_backup.py`
- `rollover_artifacts.py`

备份对象覆盖运行配置、长期记忆、对话记录及相关事件产物。备份与底层 transcript 的存在解决的是**物理持久化与灾难恢复**问题，不等价于角色在当前轮次能够自然使用这些信息。

因此，本项目不采用“只要旧 transcript / JSONL / SQLite 中还能找到数据，就视为角色没有遗忘”的判定标准。对于陪伴式交互，历史如果需要用户明确提醒系统“去翻昨天的记录”才能重新进入推理，就已经发生了产品意义上的连续性失败。

`session_rollover.py` 的职责也因此不仅是创建新 session，而是将 session 生命周期放到日终记忆流水线之后。项目要求部署时保持 **memory processing precedes rollover** 的顺序，并尽量在单日 session 内避免 compaction 先于 step04 改写历史表示。

## 五、OpenClaw 集成边界：为什么不能直接使用原生机制

### 5.1 OpenClaw 已经提供的能力

根据当前官方文档，OpenClaw 已提供以下与本项目直接相关的基础设施：

- **Agent workspace**：为 agent 提供独立 workspace 与上下文文件 [4]；
- **持久化 memory**：`MEMORY.md`、daily memory、memory search、memory-core 与 Dreaming [5][6][12]；
- **Active Memory**：在符合条件的交互 session 中执行主动 recall [7]；
- **Gateway-owned sessions**：session 状态、reset 与 transcript 由 Gateway 管理 [9][10]；
- **Compaction**：在上下文接近模型限制时，把较早历史压缩为持久化摘要并继续当前 session [10][11]；
- **消息通道**：Telegram 等 channel；
- **Automations**：持久化一次性与周期性任务。

因此，xiaodou-system 的主要贡献不在于重新实现 session、memory search、scheduler 或 Telegram，而在于定义这些通用原语如何共同服从一个**陪伴角色的自然日生命周期**。

### 5.2 为什么“已经保存”仍然不等于陪伴式记忆

OpenClaw 当前把 detailed daily notes 放在 `memory/*.md` 中，并明确规定这些文件不是普通轮次的默认 Project Context；普通轮次主要通过 `memory_search` / `memory_get` 按需读取 [6]。Active Memory 可以改善这一点，但默认 `escalate` 模式的 deep recall 仍然主要在当前消息表现出 recall intent 且没有强确定性命中时运行 [7]。

本项目在实际长期使用中观察到的问题是：跨天信息即使已经写入 daily memory，也不一定会在下一次自然对话中第一时间进入模型。用户若明确询问“昨天我们做了什么”，系统较容易触发 memory search；但陪伴场景大量真实消息并不会以这种形式出现。

例如：

```text
昨日：
“我明天下午去考试。”

今日：
“我出门了。”
```

第二句话没有足够强的检索关键词，却要求角色自然理解“出门”与昨日考试安排的关系。对于这类交互，按需搜索是一种补救路径，而不是充分的连续性机制。

当前 OpenClaw 默认 AGENTS 模板已经要求 session start 读取 today + yesterday 的 daily notes 与 `MEMORY.md` [8]，`/new` / `/reset` 也会保存结束会话的尾部并在新 session 的首轮重新提供近期 notes [9]。这些行为值得复用，但它们不能保证：

1. session 切换前的全部重要关系状态已经被正确写入 daily memory；
2. 长期运行中的普通轮次始终拥有昨日关键状态；
3. 无显式 recall intent 的短句一定触发所需历史；
4. 当前 day plan、主动事件和聊天历史被作为一个统一 episode 理解。

因此 xiaodou-system 将近期跨日状态从“可按需搜索的资料”提升为**下一自然日 Planner 与交互上下文的直接输入**；memory search 主要承担更远期历史的补充召回。

### 5.3 为什么避免让 OpenClaw compaction 成为第二套历史摘要

OpenClaw compaction 会把较早对话总结为持久化的 `compaction` entry，并保留近期消息；后续 turn 使用“compaction summary + 未压缩尾部”继续运行 [10][11]。在 compaction 前，OpenClaw 还可以运行 automatic memory flush，将重要上下文先写入 memory files [5]。

对于通用长会话，这是一套合理的上下文控制机制。

但 xiaodou-system 已经存在自己的：

```text
完整 session
+ DailyPlan
+ Event Journal
+ 主动消息
+ 用户交互
        ↓
step04 Memory Compiler
        ↓
DailyMemory / MEMORY.md
```

如果同一历史又先被 OpenClaw compaction 摘要，则系统中可能同时存在：

```text
OpenClaw Compaction Summary
            +
xiaodou DailyMemory / MEMORY.md
```

两个摘要可以同时基本正确，却不保证完全一致。它们可能在时间、事件因果、关系状态、未完成事项或信息优先级上出现不同取舍。对于陪伴角色，这种“多套历史解释同时参与当前人格状态”的风险高于节省部分上下文 token 的收益。

因此，本项目采用**单一记忆权威**原则：

> 当前 session 保留当天高保真原始工作记忆；跨日语义只由 xiaodou-system 的 step04 正式生成。

这并不意味着永远禁止使用 OpenClaw compaction。它意味着：**compaction 不应成为本项目的跨日 canonical memory path**。如果单日对话量本身已经足以触发模型上下文上限，则需要通过更大的 context budget、提前的受控边界或专门适配处理；当前版本并不把“当天一定不会发生 compaction”作为无条件保证。

### 5.4 为什么保留每日 session rollover

当前 OpenClaw 主 session 默认没有自动 reset；daily / idle reset 是可选配置 [9][10]。因此，xiaodou-system 的 04:00 rollover 不是为了跟随 OpenClaw 默认行为，而是主动设置的**自然日边界**。

其目的有两个：

1. **保护单一记忆权威**  
   在 session 长期增长、需要 compaction 之前，把当天完整会话交给 step04 统一提炼。

2. **保持工作记忆边界清晰**  
   新 session 主要承载新自然日的原始交互；过去日期的跨日状态由 `DailyMemory / MEMORY.md` 提供，而不是由越来越长、已经多次摘要的 session history 隐式承担。

因此：

```text
Day N 完整 Session
        ↓
step04 finalize / incremental / attach
        ↓
跨日记忆完成主要持久化
        ↓
session rollover
        ↓
Day N+1
```

rollover 在这里是 **Memory Commit Boundary**，而不是“忘掉昨天”。

历史 transcript / JSONL / SQLite 仍然具有备份、审计与异常恢复价值，但它们不是陪伴角色日常推理的主要记忆接口。对于本项目，**能够从磁盘找回历史 ≠ 角色在产品层面保持了连续性**。

### 5.5 为什么当前版本仍使用 `cron + at`

OpenClaw 的 Automations 已经能够完成持久化定时任务。xiaodou-system 当前仍使用 Linux `cron` 和 `at`，主要出于以下实现约束：

- step02、step03、step04 需要与 Linux 文件状态和独立 Python 进程直接对应；
- 每个阶段需要留下可单独检查的输入、输出和退出状态；
- step03 的未来事件天然适合一次性 `at` job；
- 当前验证环境已经围绕 `flock + cron + atd` 建立完整运维链路；
- 系统调度即使与 OpenClaw Gateway 暂时失联，也仍可留下事件应当发生、失败原因和待恢复状态。

因此，该方案应理解为当前版本的 orchestration backend，而不是对 OpenClaw scheduler 能力的否定。未来版本可以在保持 DailyPlan、Event Model 与 step04 数据契约不变的前提下增加 OpenClaw Automations adapter。

### 5.6 为什么保留独立 step04

OpenClaw 当前 memory 体系已经具有 Markdown memory、memory search、automatic memory flush、Active Memory、Dreaming 与 consolidation [5–8][12]。xiaodou-system 保留独立 step04，不是因为 OpenClaw “没有记忆系统”，而是因为两者优化目标不同。

OpenClaw 主要解决：

- 信息如何持久化；
- 如何建立索引并按需召回；
- 长会话如何 compaction；
- 哪些短期信号值得提升为 durable memory。

xiaodou-system 额外要求：

- 昨日计划、实际事件、主动消息与聊天必须构成一个统一 episode；
- 昨日关键状态必须直接参与下一日，而不是只等待 semantic recall；
- 当前 session 到跨日 memory 只能有一条 canonical 编译路径；
- session rollover 必须位于这条路径之后；
- 用户可以检查每天究竟保留、合并和压缩了什么。

因此，step04 更接近一个 **episodic day compiler**，而不是另一个通用 memory search engine。

## 六、运行证据

本节仅展示当前 Linux 验证环境中的实际运行结果，用于说明消息链路和文件持久化已经贯通。以下截图不构成聊天质量评测、用户研究或跨环境性能证明。

### 6.1 Telegram 消息链路

<p align="center">
  <img src="docs/screenshot-telegram.jpg" alt="正常运行状态下的 Telegram 对话效果" width="800"/>
  <br/>
  <em>图 2-a：角色在 Telegram 上的日常对话与自拍</em>
</p>

当前实现可以依据 daily plan 触发主动事件，并在事件策略允许时生成文本和可选图片，经 Telegram 发送。

### 6.2 文件持久化

<p align="center">
  <img src="docs/filesystem-directories.jpg" alt="框架自动沉淀的 daily / chatlog / daily_selfies 目录" width="800"/>
  <br/>
  <em>图 2-b：框架自动沉淀的 daily / chatlog / daily_selfies 数据目录</em>
</p>

运行过程中，`daily/`、`chatlog/`、`daily_selfies/` 与 `MEMORY.md` 分别保存计划/执行数据、对话记录、图片产物和长期记忆。它们同时构成故障排查与日终整理的主要输入。

---

## 七、部署

### 7.1 环境要求

当前验证目标为 **Linux**：

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

OpenClaw 官方当前仍提供 `openclaw configure`、`openclaw health` 等 CLI；Gateway 默认端口为 `18789`，但可通过配置修改 [10][11]。

### 7.2 配置 OpenClaw

```bash
openclaw configure
openclaw health
```

至少需要完成：

- 可用的模型/provider 配置；
- Gateway 启动与健康检查；
- Telegram channel 配置；
- 本实例所使用的 workspace / session 对应关系。

如果当前主机已有 Gateway 占用默认端口，可为实例配置其他端口，例如：

```bash
openclaw config set gateway.port 19203
```

修改后按当前 OpenClaw 版本的运行方式启动或重启 Gateway。

### 7.3 初始化 xiaodou-system

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` 负责：

1. 检查运行环境；
2. 准备角色核心文件模板；
3. 生成 `settings.yaml`；
4. 创建运行目录；
5. 执行脚本与配置校验；
6. 可选安装 cron。

默认实例目录为：

```text
~/.openclaw/workspace
```

也可通过 `--instance-dir` 指定其他目录。

对于已经存在的核心文件，初始化程序应保持原文件不被覆盖；仅对缺失项创建模板或骨架。

### 7.4 运行部署向导

```bash
python3 scripts/guided_setup.py
```

向导依次处理：

1. Python / atd / OpenClaw / 时区检查；
2. 角色核心文件检查；
3. API key 配置；
4. Telegram token 与 chat id 绑定；
5. OpenClaw Gateway session 绑定；
6. xiaodou-system 记忆写入契约配置；
7. 最终校验；
8. Gateway 配置生效。

可用模式：

```text
--non-interactive    使用默认值进行脚本化配置
--verify-only        仅执行最终校验
```

### 7.5 安装定时任务

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

step03 依赖系统 `atd` 服务。安装后应确认：

```bash
systemctl status atd
openclaw health
```

### 7.6 配置文件

运行配置集中在：

```text
settings.yaml
```

主要配置域包括：

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

角色内容仍应优先放在 7 个 Markdown 文件中，而不是写入 Python 逻辑。

---

## 八、目录结构

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
├── cron/                      # step02 / step03 / step04 调度模板
├── docs/                      # 架构图与运行截图
└── settings.example.yaml
```

运行实例还会产生 `daily/`、`chatlog/`、自拍产物、事件日志与备份文件；这些属于运行时数据，不应与源码目录的职责混淆。

---

## 九、当前限制

### 9.1 验证范围

当前版本仅在 Linux 环境完成端到端验证。README 中的“可运行”结论仅指该验证环境中的完整链路已执行成功。

尚未建立：

- 多机器/多发行版兼容矩阵；
- Windows / macOS 调度适配；
- 长周期故障率与 SLA 统计；
- 主动消息质量的人工或自动评测基准；
- 不同角色之间的系统化迁移测试。

### 9.2 平台

当前实现依赖：

```text
cron
at / atd
flock
```

因此 Windows 与 macOS 不能直接复用同一调度实现。未来若扩展跨平台支持，应保持 step02 / step03 / step04 的数据契约不变，仅替换 scheduler / lock adapter。

### 9.3 消息通道

当前正式验证通道为 Telegram。`providers/` 已将 delivery 与核心业务逻辑分离，但其他通道必须单独实现并测试发送、媒体、身份绑定和 session 映射。

### 9.4 单日 Session Context Budget

本项目希望在一个自然日内尽量保留完整、未 compact 的 session，再由 step04 完成跨日记忆提炼。但模型上下文窗口仍然存在硬上限；如果单日消息量、工具输出或主动事件异常密集，OpenClaw 仍可能在 rollover 前触发 compaction。

因此，当前实现需要监控单日 context budget。对于极高交互量场景，后续版本需要增加受控的 day-internal checkpoint 或专用 context adapter，而不能简单假设“每日 rollover 足以永远避免 compaction”。

### 9.5 模型与外部服务

DeepSeek 与 Seedream 均属于外部依赖。API 版本、模型名称、价格、速率限制和服务可用性可能发生变化，因此生产部署应通过 provider 配置管理，而不应依赖 README 中的固定服务状态。

---

## 十、结论

xiaodou-system 将长期陪伴智能体的运行问题拆分为一个明确的自然日控制循环：

```text
角色与跨日状态
    ↓
step02：生成并验证当日计划
    ↓
step03：调度并执行主动事件
    ↓
完整当日 Session + Event Evidence
    ↓
step04：编译 DailyMemory / 更新 MEMORY
    ↓
受控 Session Rollover
    ↓
下一自然日
```

框架的核心不是新的语言模型算法，也不是重新实现 OpenClaw，而是四项工程约束的组合：

1. **角色内容与运行代码分离**；
2. **生成模型与确定性控制面分离**；
3. **Persistence、Retrieval 与 Continuity 分开建模**；
4. **当天完整 session 与跨日 consolidated memory 之间只保留一条 canonical 转换路径**。

OpenClaw 解决的是通用 Agent 的运行、session、memory、search、compaction 与调度问题；xiaodou-system 在其上进一步定义“一个陪伴角色如何连续地过完一天，并把这一天无歧义地交给下一天”。

在当前 Linux + OpenClaw + Telegram 验证环境中，上述流水线已经形成完整闭环。后续工作的重点应是扩大平台与消息通道覆盖、建立可量化的稳定性测试、监控单日 context budget，并验证角色迁移是否能够在不修改核心调度与记忆协议的情况下完成。

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

5. OpenClaw Documentation. **Memory overview**.  
   https://docs.openclaw.ai/concepts/memory

6. OpenClaw Documentation. **System prompt / memory bootstrap behavior**.  
   https://docs.openclaw.ai/concepts/system-prompt

7. OpenClaw Documentation. **Active Memory**.  
   https://docs.openclaw.ai/concepts/active-memory

8. OpenClaw Documentation. **Default AGENTS.md**.  
   https://docs.openclaw.ai/reference/AGENTS.default

9. OpenClaw Documentation. **The main session**.  
   https://docs.openclaw.ai/concepts/main-session

10. OpenClaw Documentation. **Session management**.  
    https://docs.openclaw.ai/concepts/session

11. OpenClaw Documentation. **Session management & compaction deep dive**.  
    https://docs.openclaw.ai/reference/session-management-compaction

12. OpenClaw Documentation. **Dreaming**.  
    https://docs.openclaw.ai/concepts/dreaming

13. OpenClaw Documentation. **Automations**.  
    https://docs.openclaw.ai/automation/cron-jobs

14. OpenClaw Documentation. **Telegram channel**.  
    https://docs.openclaw.ai/channels/telegram

15. OpenClaw Documentation. **Configure / CLI**.  
    https://docs.openclaw.ai/cli/configure

16. OpenClaw Documentation. **Gateway health**.  
    https://docs.openclaw.ai/gateway/health

> 外部技术描述与 OpenClaw 能力边界于 **2026-08-16** 按官方文档核查。OpenClaw 后续版本可能调整默认 session、memory、compaction、bootstrap 或调度机制；部署时以所安装版本的官方文档为准。

## License

MIT © xiaodou-system
