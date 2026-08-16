# xiaodou-system

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux-blue" alt="platform"/>
  <img src="https://img.shields.io/github/v/release/SHIJINS66/xiaodou-system?color=green&label=release" alt="release"/>
  <img src="https://img.shields.io/badge/channel-Telegram-lightgrey" alt="channel"/>
  <img src="https://img.shields.io/badge/docs-English-blue?label=English" alt="docs-en"/>
  <a href="README.md"><img src="https://img.shields.io/badge/lang-简体中文-blue" alt="lang-zh"/></a>
  <img src="https://img.shields.io/badge/lang-English-brightgreen" alt="lang-en"/>
</p>

## Abstract

**xiaodou-system** is a persona-agnostic runtime framework built on [OpenClaw](https://docs.openclaw.ai) for long-term companionship scenarios. It takes the natural day as its basic operational cycle and organizes persona definition, daily planning, scheduled event execution, proactive messages, day-end memory, and session switching into one complete pipeline:

```text
Persona and historical state
  ↓
Daily planning
  ↓
Scheduled events and proactive interaction
  ↓
Complete daily session and event records
  ↓
Day-end memory consolidation
  ↓
Session rollover
  ↓
Next natural day
```

This project does not extend the reasoning ability of language models themselves, but addresses several engineering problems in long-term companion agents: how a persona continues to have its own schedule and state without immediate user input; how the previous day's important experiences naturally influence the next day; how proactive messages stay consistent with actual send results, session history, and long-term memory; and how to avoid multiple independent summarization mechanisms rewriting the same history simultaneously.

The system adopts a design that separates generative logic from control logic. Models such as DeepSeek and Seedream are responsible for generating plans, text, and images; scheduling, JSON Schema validation, idempotency, file locking, transaction state, persistence, and session switching are controlled by deterministic scripts. The "determinism" referred to in this document only means that control flow and state transitions follow explicit rules; it does not mean that model outputs can be reproduced identically on each run.

The current implementation uses **Linux + OpenClaw + Telegram** as its validation environment, with DeepSeek by default as the text model and Seedream as the image generation service.

---

## I. Background and Design Goals

### 1.1 The Evolution of Long-Term Companion Agents

The idea of humans establishing sustained social interaction with computers did not first appear in the era of large language models. Early chatbot systems, represented by ELIZA, relied mainly on rule matching and text transformation to sustain local dialogue; they did not have long-term memory, persona state, or autonomous behavior in today's sense, but they already showed that a natural-language interface alone is enough to elicit strong social interpretation from users. For a long time afterward, dialogue-system research still focused on "how to reply to the current utterance more reasonably": task-oriented systems focused on task completion, while open-domain chat systems focused on fluency and relevance. [1]

From the mid-to-late 2010s, research began to distinguish more explicitly between **task-oriented assistants** and **social dialogue systems**. Microsoft XiaoIce treated long-term engagement, emotional understanding, and social relationships as primary goals rather than merely completing a query or task; its system design already treated long-term interaction as an independent optimization target. [2] At the same time, work such as PERSONA-CHAT introduced explicit personas into open-domain dialogue, turning identity and expression consistency from an implicit language-style problem into an input condition that can be modeled separately. [3] This stage formed two basic requirements for companionship systems: a persona should have a relatively stable identity and expression style, and system evaluation cannot look only at single-turn response quality.

With the development of Transformers and large-scale pretrained language models, the generation capability of open-domain dialogue improved significantly, but the context window still limited long-term interaction. BlenderBot 3 combined long-term memory with open-domain dialogue and internet access in a single system, representing a typical approach: maintain additional storage outside the current context and retrieve historical information when needed. [4] This gradually shifted "long-term memory" from a problem of saving chat records to a problem of **storage, update, and retrieval strategy**.

After 2023, large language models pushed this direction further toward long-term agents. MemoryBank designed a continually updated external memory for long-term AI companion scenarios, enabling models to extract user information from past conversations, reinforce important content, and recall it in later interaction. [5] Generative Agents combined observation, memory, reflection, and planning so that an agent can not only recall the past but also arrange future activities, form schedules, and proactively interact with other characters based on historical experience. [6] This change is significant: long-term systems began to move from "chatbots that can remember the past" to "agents with persistent internal state that act on it."

The problem that then emerged was no longer just "can more history be stored," but **whether history can be used correctly at the right time**. LoCoMo extended long-dialogue evaluation to up to 35 sessions and expanded the problems to long-term question answering, event summarization, and cross-modal dialogue; results showed that even with longer contexts or retrieval mechanisms, temporal, causal, and event relationships across sessions remain difficult. [7] LD-Agent therefore further distinguished event memory in the current session from that in historical sessions, and dynamically maintained the persona of both the user and the agent, so that long-term dialogue no longer relies only on a single similarity-based retrieval. [8]

By 2025–2026, long-term memory research continued to move from "fact recall" toward more complex state-use problems. LoCoMo-Plus specifically constructed scenarios where **the current triggering semantics are not directly similar to the historical constraint**, to test whether a model can apply implicit long-term constraints rather than merely answer "what happened in the past." [9] This is very close to real companionship interaction: users often do not explicitly ask the system to recall something, yet events, commitments, and relationship state that occurred earlier should still change the current response.

The evolution of long-term companion agents can therefore be summarized as:

```text
Single-turn dialogue
  ↓
Open-domain social chat
  ↓
Stable Persona
  ↓
Cross-session long-term memory
  ↓
Memory-driven planning and proactive behavior
  ↓
Sustained carryover of time, events, and relationship state
```

xiaodou-system sits in the second half of this evolution. It does not propose a new generic memory-retrieval algorithm, but focuses on a more concrete engineering problem: how to organize a persona's daily plan, actual events, proactive messages, user interactions, and long-term memory into a continuous temporal process, so that the previous day's state is not merely "searchable history" but reliably enters the next day's operation.

### 1.2 Design Goals

xiaodou-system mainly solves the following five problems:

1. **Proactive behavior**: the system can generate future events from the day's plan without immediate user input, and decide at a specified time whether to proactively reach out. Proactive behavior should originate from persona state that already exists that day, rather than asking the model on the spot after each timer fires "what should we do now."

2. **Cross-day continuity**: yesterday's important state can directly influence today's planning and dialogue, rather than reappearing only after an explicit historical search. In companionship scenarios, "history is still saved" and "the persona can naturally carry history forward" are different requirements.

3. **Controlled execution**: whether an event executes, whether it is sent twice, whether a message has already been written into the Session, and which step should be retried after failure must all be decided by explicit program state.

4. **Memory consistency**: the day's complete session, life plan, and actual events are consolidated into cross-day memory by the same day-end process, avoiding different summarization mechanisms each explaining the same history and conflicting in later operation.

5. **Separation of persona and system**: identity, personality, life routines, and user relationships are described by Markdown files, and execution logic is not bound to a specific persona. A persona change should not require rewriting the scheduling, event execution, or memory pipeline.

## II. System Design

### 2.1 Persona Files

The persona is defined by 7 core Markdown files in the workspace:

| File | Responsibility |
|---|---|
| `AGENTS.md` | Operating rules and behavioral constraints |
| `IDENTITY.md` | Identity and stable background |
| `SOUL.md` | Personality, expression style, and interaction boundaries |
| `LIFE.md` | Life routines and the Planner's primary basis |
| `USER.md` | The companion target and long-term information |
| `TOOLS.md` | Tools and runtime environment description |
| `MEMORY.md` | Consolidated long-term memory |

Persona content is not hardcoded into business scripts. When changing personas, in principle only the persona files and related resource configuration need to be replaced.

### 2.2 Natural-Day State

The system treats a natural day as its primary operational unit:

```text
Day N
├── DailyPlan
├── Scheduled Events
├── Executed Events
├── Conversation
├── Event Journal
└── Daily Memory
```

`DailyPlan` is not merely a list of scheduled tasks. It describes what activity, location, and emotional state the persona is expected to be in during the day, and which time windows allow proactive interaction. The scheduler only starts events at a specified time; what actually gives an event its semantics is the DailyPlan.

This design gives the persona's proactive behavior a before-and-after context. For example, an evening event is not an isolated "send a message" task but a node in that day's life trajectory; when the event executes, it can read the activities that have already occurred, the current state, and the user's most recent interaction, and then decide whether to send, what to send, and whether an image is needed.

The DailyPlan is also an important connection between step03 and step04. step03 uses it to execute events; step04 incorporates both the "original plan" and "actual execution results" into day-end consolidation, thereby distinguishing planned events, events that actually occurred, and events that were cancelled or suppressed.

### 2.3 The Day's Session and Cross-Day Memory

The current Session mainly preserves the day's complete interactions; `Daily Memory` and `MEMORY.md` preserve cross-day information that has completed day-end consolidation.

The conversion path between the two is fixed:

```text
Complete daily Session
+ DailyPlan
+ Event Journal
+ Proactive messages and user replies
  ↓
step04
  ↓
Daily Memory / MEMORY.md
```

This division first solves the problem of information sources. The day's dialogue retains its original context, and proactive events leave evidence through the Event Journal and Session injection; at day end, the system generates cross-day memory from these complete materials, rather than repeatedly writing different partial summaries into the same long-term file throughout the day.

Second, it allows "recent continuity" and "distant retrieval" to be handled separately. The previous day still affects today's state, so it needs to enter the new day's planning and context directly; information from longer ago that is not necessarily relevant now can continue to be fetched on demand through long-term memory or search mechanisms.

Old transcripts, logs, and backups are still retained, but mainly for audit, fault diagnosis, and recovery, and do not serve as the primary memory entrance in normal dialogue.

### 2.4 Generation and Control Separation

Models are responsible for:

- generating the DailyPlan;
- generating proactive messages;
- generating day-end memory text;
- optionally generating images.

Deterministic scripts are responsible for:

- scheduled triggering;
- schema validation;
- idempotency judgment;
- `flock` concurrency control;
- send and injection state recording;
- file persistence;
- Session rollover;
- exception recovery.

---

## III. Overall Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system architecture" width="880"/>
  <br/>
  <em>Fig. 1: Overall system architecture — three business pipelines with a persistent layer running throughout</em>
</p>

The system mainly consists of four parts:

- the persona files;
- the three pipelines step02 / step03 / step04;
- the `providers/` external dependency layer;
- Linux scheduling, file persistence, logs, and backups.

### 3.1 providers

External services are uniformly encapsulated in `providers/`:

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

Business scripts depend on provider interfaces rather than directly on a specific vendor's SDK. This allows replacing a model or message channel without modifying the DailyPlan, event execution, or memory processes.

### 3.2 Persistent Objects

Primary runtime data includes:

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

Key state is written to disk by preference, rather than existing only in the current model context.

---

## IV. Natural-Day Runtime Flow

### 4.1 step02: Daily Planning

**Default trigger time: 06:00**

Primary scripts:

- `build_daily_plan.py`
- `validate_daily_plan.py`
- `initialize_daily_file.py`
- `schema_engine.py`
- `schema_validator.py`
- `weather_provider.py`

Input includes:

```text
Persona core files
+ the day's weather
+ the last 7 days of memory
+ yesterday's execution results
```

Output:

```text
daily/YYYY-MM-DD.json
```

The DailyPlan describes that day's activities, locations, emotions, outreach windows, and related events. Generated output must pass JSON Schema validation before proceeding to the next stage. The schema only verifies the structural contract; it does not guarantee that the model content itself is fully correct.

### 4.2 step03: Scheduling and Event Execution

**Morning scheduling time: 06:20**

Primary scripts:

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

The morning stage reads the DailyPlan and submits the non-silent events that need to be executed to the system `at`.

The execution flow of a single event:

```text
Read DailyPlan and context
  ↓
Idempotency check / flock
  ↓
Decide whether the event should still execute now
  ↓
Generate message
  ↓
Optionally generate image
  ↓
Send Telegram
  ↓
Write into the OpenClaw Session
  ↓
State and Event Journal write-back
```

Sending a message and writing into the Session are two independent side effects, so the system records the generated, sent, injected, and final committed states separately. If a message has been sent successfully but Session injection failed, recovery only redos the injection, avoiding duplicate sends.

### 4.3 step04: Day-End Memory

Default schedule:

```text
00:20 / 00:50 finalize
02:00 incremental
02:03 attach
04:00 session_rollover
Sun 04:30 weekly compress
```

Primary scripts:

- `finalize_yesterday.py`
- `build_daily_memory.py`
- `update_memory_md.py`
- `session_rollover.py`
- `render_chatlog.py`
- `normalize_chatlog.py`
- `memory_quality.py`
- `rollover_artifacts.py`

step04 synthesizes the following data:

```text
Complete daily Session
+ DailyPlan
+ Actual event state
+ Event Journal
+ Proactive messages
+ User replies
```

It first reconstructs the actual process of the day: which plans executed on time, which events were cancelled or suppressed, what the persona proactively sent, how the user responded, and which items remain incomplete. On this basis it generates Daily Memory and updates the information that needs to be retained long term into `MEMORY.md`.

Daily Memory is therefore not a simple "chat summary." It has two responsibilities: first, to save the main facts and relationship changes of the day's episode; second, to give the next natural day enough recent state. Weekly compression then handles deduplication and convergence of the long-term `MEMORY.md`, rather than continuously rewriting long-term memory every conversation turn.

### 4.4 Session Rollover

The daily rollover executes after the main memory processing completes:

```text
Day N Complete Session
  ↓
Daily Memory
  ↓
MEMORY update
  ↓
session_rollover
  ↓
Day N+1
```

This both preserves the day's complete session as much as possible and ensures that, before entering the next day, the previous day's information has entered the project's own cross-day memory.

---

## V. OpenClaw Capability Boundaries and Adaptation

### 5.1 What OpenClaw Already Provides

As of the official documentation [10], OpenClaw already provides a complete agent runtime foundation, including workspace, Gateway-managed sessions, Markdown memory, memory search, Active Memory, compaction, Dreaming, message channels such as Telegram, and an Automations scheduler [13][15][18]. Xiaodou-system therefore does not re-implement generic agent runtime; its work sits at the application level that defines what a natural day means for a companion persona.

### 5.2 Native Memory Cannot Fully Guarantee Cross-Day Continuity

OpenClaw's memory is by default read on demand during ordinary turns [15]. Its design assumption is that only relevant historical information should be loaded into the model context; irrelevant information should not occupy context. This is a reasonable posture for general task-oriented agents, but it has specific implications for companionship scenarios: without explicit retrieval signals, the system does not automatically consider "yesterday's state" as part of today's required context. In everyday language, whether "I've arrived," "I'm out now," or "it's over" should be understood in relation to yesterday's arrangements often depends on prior context, yet such short sentences do not provide a strong enough retrieval cue.

Companionship scenarios are different from task-oriented ones. Even if the user does not ask "what did we do yesterday," the relationship state still needs to persist across days. Therefore the recent continuity of companions cannot rely solely on keyword or semantic search; it needs the state to directly enter the next day's planning and context.

### 5.3 Compaction and the Problem of Multiple Summaries

OpenClaw's compaction summarizes earlier transcripts into a persistent summary to control context length. When compaction coexists with a project's own day-end memory, the same history may be summarized by two systems at different points in time: one from the compaction summary, one from the project's own memory. Even if both summaries are "basically correct," they may differ in handling of event details, time ordering, causality, uncompleted items, and information priority. For a companion where relationship state is stable and important, this uncertainty is not desirable.

xiaodou-system therefore keeps only one formal cross-day memory compiler, and treats the day's complete session as raw working memory:

```text
Complete daily Session
  ↓
step04
  ↓
Daily Memory / MEMORY.md
```

This does not deny the value of OpenClaw compaction in other scenarios; it only clarifies that compaction should not be the canonical path for a companion's cross-day memory.

### 5.4 The Necessity of an Explicit Day Boundary

An unlimited persistent session has a risk: over weeks of accumulation, the context window will eventually be insufficient, and the session will be forced to enter compaction or lose earlier detail. If reset happens too early, information that has not yet entered consolidated memory is lost from the current working context.

xiaodou-system therefore makes "the natural day" an explicit boundary: the day's session is kept as complete as possible, memory is consolidated during the night window, and then rollover happens. This makes both "preserving the day's original content" and "transferring to the next day" explicit and controllable, rather than relying on unknown compaction timing.

### 5.5 The Relationship Between Automations and DailyPlan

OpenClaw's Automations can execute periodic and one-shot tasks, and can serve as an alternative scheduling mechanism. But a scheduler only solves "when to run a task," not "what an event means in the persona's life." The DailyPlan and Event Model additionally need activity, location, mood, outreach windows, silent/non-silent types, and possible media behaviors. The current implementation uses Linux `cron` and `at` because its scheduling and locking chain is already validated around `flock` + file state + event journal; the future can add an Automations adapter on top, as long as the DailyPlan and Event Model semantics remain unchanged.

---

## VI. Reliability Design

### 6.1 Idempotency and Concurrency

Event execution uses stable identity, idempotency checks, and `flock` to avoid duplicate execution caused by scheduler re-entry, duplicate triggers, or manual reruns.

### 6.2 Data Contracts

JSON Schema constrains cross-stage data. Schemas are used to detect missing required fields, type errors, out-of-range enum values, and model outputs that do not conform to the protocol. Schemas do not guarantee that the generated content itself is factually correct.

### 6.3 Send and Injection Separated

Sending a Telegram message and writing into the OpenClaw Session are independent side effects, recorded separately as generated / delivered / injected / committed. If a message has been sent but injection failed, recovery only redos injection, avoiding repeated sends.

### 6.4 Backup and Audit

Backup scripts (`backup_openclaw.py`, `raw_backup.py`, `rollover_artifacts.py`) cover runtime configuration, long-term memory, chat records, and related event artifacts. Backup is used to preserve evidence and enable disaster recovery; it is not a substitute for normal memory continuity.

---

## VII. Runtime Validation

The current validation conclusion only indicates that the main runtime chain in a Linux environment is wired through; it does not constitute proof of chat quality, long-term SLA, or cross-platform performance.

### 7.1 Telegram Interaction

<p align="center">
  <img src="docs/screenshot-telegram.jpg" alt="Telegram conversations in normal operation" width="800"/>
  <br/>
  <em>Fig. 2-a: Day-to-day Telegram conversations and selfies by the persona</em>
</p>

The current implementation can trigger proactive events from the DailyPlan, generate text and optional images when policy allows, and send them via Telegram.

### 7.2 File Persistence

<p align="center">
  <img src="docs/filesystem-directories.jpg" alt="auto-persisted daily / chatlog / daily_selfies directories" width="800"/>
  <br/>
  <em>Fig. 2-b: The auto-persisted daily / chatlog / daily_selfies data directories</em>
</p>

During operation, `daily/`, `chatlog/`, `daily_selfies/`, and `MEMORY.md` respectively store plan/event state, chat records, image artifacts, and consolidated long-term memory. These files together form the primary input for both day-end memory and fault diagnosis.

---

## VIII. Deployment

### 8.1 Environment Requirements

The current validation environment is **Linux**:

- Python 3.10+
- `at` / `atd`
- `cron`
- `flock`
- default timezone: `Asia/Shanghai`
- [OpenClaw](https://docs.openclaw.ai) installed and initialized

### 8.2 OpenClaw Setup

```bash
openclaw configure
openclaw health
```

At minimum, complete a usable model/provider configuration, Gateway startup and health check, and the Telegram channel.

### 8.3 xiaodou-system Initialization

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` performs environment checks, persona file preparation, `settings.yaml` generation, runtime directory creation, script and configuration validation, and optional cron installation. The default instance directory is `~/.openclaw/workspace`; another can be specified with `--instance-dir`.

Existing persona files are not overwritten; templates or skeletons are created only for missing files.

### 8.4 Setup Wizard

```bash
python3 scripts/guided_setup.py
```

The wizard handles Python / atd / OpenClaw / timezone checks, core file checks, API key configuration, Telegram token and chat id binding, Gateway Session binding, memory write-contract configuration, final validation, and Gateway configuration activation. Available modes include `--non-interactive` and `--verify-only`.

### 8.5 Scheduled Tasks

```bash
bash init.sh --instance-dir ./instance --install-cron
```

When `ALLOW_CRON_APPLY=1`, the root user writes to `/etc/crontab`; a non-root user writes to the user-level crontab.

Default schedule:

```text
06:00 step02-morning
06:20 step03-morning
00:20 / 00:50 finalize
02:00 incremental
02:03 attach
04:00 session_rollover
Sun 04:30 weekly compress
```

step03 depends on `atd`.

### 8.6 Configuration

Runtime configuration is centralized in `settings.yaml`, with domains such as `system`, `runtime`, `character`, `companion`, `interaction_policy`, `selfie`, `models`, and `delivery`. Persona personality and life content remain in Markdown files rather than in Python control logic.

---

## IX. Directory Structure

```text
├── init.sh
├── AGENTS.md
├── IDENTITY.md
├── SOUL.md
├── LIFE.md
├── USER.md
├── TOOLS.md
├── MEMORY.md
├── scripts/ # planner / validate / execute / memory
├── providers/ # LLM / image / gateway / delivery
├── prompts/ # per-stage model prompts
├── schemas/ # JSON Schema
├── cron/ # step02 / step03 / step04
├── docs/ # architecture diagram and runtime screenshots
└── settings.example.yaml
```

A running instance also produces `daily/`, `chatlog/`, `daily_selfies/`, event journal, transaction state, and backup files.

---

## X. Current Limitations

### 10.1 Validation Scope

The current system has only been validated end-to-end on Linux. Not yet established: a multi-distribution compatibility matrix, Windows / macOS scheduling adaptation, long-cycle failure-rate and SLA data, a proactive-message quality benchmark, and systematic migration tests across different personas.

### 10.2 Single-Day Context Budget

The system hopes to keep the complete, uncompacted Session within a natural day as far as possible, but the model context window still has a hard ceiling. If the volume of messages, tool outputs, or proactive events within a day is unusually high, compaction may still be triggered before rollover. High-interaction-volume scenarios will later need single-day context budget monitoring, controlled day-internal checkpoints, or a dedicated context adapter.

### 10.3 Platform Dependencies

The current control plane depends on `cron`, `at` / `atd`, and `flock`. Windows and macOS require replacing the platform-specific scheduler / lock adapter while keeping the DailyPlan, Event Model, and Memory Pipeline data contracts unchanged.

### 10.4 Message Channels

The currently validated channel is Telegram. Other message channels need separate validation of text sending, media sending, identity binding, Session mapping, and proactive-message write-back.

### 10.5 External Models and Services

DeepSeek and Seedream are external services. Model names, APIs, pricing, rate limits, and availability may change, so production deployments should manage them via provider configuration.

---

## XI. Summary

xiaodou-system converts the long-term companionship problem into a day-bounded operating process. Its core state transition is:

```text
Persona and historical state
  ↓
DailyPlan
  ↓
Scheduled / executed events
  ↓
Complete daily Session
  ↓
Daily Memory
  ↓
Consolidated Memory
  ↓
Session rollover
  ↓
Next natural day
```

Its main engineering contribution is to make explicit several constraints that generic agent infrastructure does not automatically provide for companions: the natural day as a state boundary; the DailyPlan as the world state of the day; consistency between proactive behavior and actual send results; a single canonical cross-day memory compilation path; and memory commit before rollover. For a companion, what needs to be maintained is not merely that "historical data still exists," but that yesterday can naturally enter today without relying on the user's explicit reminder.

---

## References

1. Weizenbaum, J. **ELIZA — A Computer Program for the Study of Natural Language Communication between Man and Machine**. Communications of the ACM, 1966.  
   https://en.wikipedia.org/wiki/ELIZA

2. Zhou, L., Gao, J., Li, D., & Shum, H.-Y. **The Design and Implementation of XiaoIce, an Empathetic Social Chatbot**. Computational Linguistics, 2020.  
   https://aclanthology.org/2020.cl-1.4/

3. Zhang, S., Dinan, E., Urbanek, J., Szlam, A., Kiela, D., & Weston, J. **Personalizing Dialogue Agents: I have a dog, do you have pets too?** ACL, 2018.  
   https://aclanthology.org/P18-1205/

4. Shuster, K., et al. **BlenderBot 3: A Deployed Conversational Agent that Continually Learns to Responsibly Engage**. arXiv:2208.03188, 2022.  
   https://arxiv.org/abs/2208.03188

5. Zhong, W., Guo, L., Gao, Q., Ye, H., & Wang, Y. **MemoryBank: Enhancing Large Language Models with Long-Term Memory**. AAAI 2024.  
   https://arxiv.org/abs/2305.10250

6. Park, J. S., O'Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. **Generative Agents: Interactive Simulacra of Human Behavior**. arXiv:2304.03442, 2023.  
   https://arxiv.org/abs/2304.03442

7. Maharana, A., et al. **Evaluating Very Long-Term Conversational Memory of LLM Agents**. arXiv:2402.17753, 2024.  
   https://arxiv.org/abs/2402.17753

8. Li, H., Yang, C., Zhang, A., Deng, Y., Wang, X., & Chua, T.-S. **Hello Again! LLM-powered Personalized Agent for Long-term Dialogue**. arXiv:2406.05925, 2024.  
   https://arxiv.org/abs/2406.05925

9. Maharana, A., et al. **LoCoMo-Plus: A Long-Term Multi-Memory Benchmark for Evaluating the Challenge of Implicit Semantic Constraints in LLM Agents**. 2025.  
   https://arxiv.org/abs/2505.23928

10. OpenClaw Documentation. **Concepts / Configuration / CLI**.  
    https://docs.openclaw.ai/concepts  
    https://docs.openclaw.ai/configuration  
    https://docs.openclaw.ai/cli

11. Park, J. S., et al. **Generative Agents: Interactive Simulacra of Human Behavior**. arXiv:2304.03442, 2023.  
    https://arxiv.org/abs/2304.03442

12. Zhong, W., et al. **MemoryBank**. arXiv:2305.10250, 2024.  
    https://arxiv.org/abs/2305.10250

13. OpenClaw Documentation. **Agent workspace / Context**.  
    https://docs.openclaw.ai/concepts/agent-workspace  
    https://docs.openclaw.ai/concepts/context

14. OpenClaw Documentation. **Session management & compaction**.  
    https://docs.openclaw.ai/reference/session-management-compaction

15. OpenClaw Documentation. **Memory / memory search**.  
    https://docs.openclaw.ai/concepts/memory

16. OpenClaw Documentation. **Dreaming**.  
    https://docs.openclaw.ai/concepts/dreaming

17. OpenClaw Documentation. **Active Memory**.  
    https://docs.openclaw.ai/concepts/active-memory

18. OpenClaw Documentation. **Automations / Telegram**.  
    https://docs.openclaw.ai/automation/cron-jobs  
    https://docs.openclaw.ai/channels/telegram

> OpenClaw capability boundaries were verified against official documentation on 2026-08-16. Later versions may adjust default Session, Memory, Compaction, or scheduling behavior; at deployment, follow the official documentation of the installed version.

---

## License

MIT © xiaodou-system
