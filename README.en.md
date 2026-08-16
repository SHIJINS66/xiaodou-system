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

**xiaodou-system** is a persona-agnostic runtime framework built on [OpenClaw](https://docs.openclaw.ai) for long-term companionship scenarios. It takes the "natural day" as its basic operational cycle and organizes persona definition, daily life planning, scheduled event execution, proactive message sending, session state, day-end memory consolidation, and cross-day state handover into a closed loop. Its goal is not to extend the general reasoning ability of language models, but to establish an auditable, recoverable, and portable long-term operating mechanism for companion agents.

Unlike typical reactive dialogue systems, xiaodou-system does not treat the user's current message as the only starting point of behavior. At the start of each day, the persona generates that day's life trajectory from its identity, life routines, recent memory, weather, and the previous day's state; a deterministic scheduling system then executes the corresponding events at future time points and proactively reaches out to the user when interaction conditions are met; at day end, the complete session, plan, event execution results, and proactive message records are uniformly consolidated into cross-day state that the next natural day can consume directly. This forms:

```text
Persona definition and historical state
        ↓
Daily planning
        ↓
Scheduled events and proactive interaction
        ↓
Complete daily session and event evidence
        ↓
Day-end memory compilation
        ↓
Controlled session rollover
        ↓
Next natural day
```

This project adopts a "generation plane vs. control plane separation" design. Generative models such as DeepSeek and Seedream are responsible for plan, text, and image content; scheduling, schema validation, event idempotency, concurrency locking, transaction state, persistence, and session boundaries are deterministically controlled by Python scripts and Linux system facilities. The "determinism" referred to in this document only means that control flow and state transitions follow explicit rules; it does not mean that language model or image model outputs can be reproduced identically on each run.

Regarding long-term memory, the system distinguishes **Persistence**, **Retrieval**, and **Continuity**. The fact that a history record is written to disk does not mean it will necessarily be used by the model in the next natural interaction; even if history can be re-acquired by search, that is not equivalent to the persona naturally carrying forward the previous day's state without user prompting. To this end, xiaodou-system treats the current natural day's complete Session as high-fidelity working memory and stipulates that cross-day semantics are generated only through the project's own day-end memory pipeline, thereby avoiding the same history being interpreted by multiple independent summarization mechanisms at once.

Existing research has separately validated the importance of memory, reflection, planning, and long-term dialogue state for persistent agents [1][2][3]. xiaodou-system does not propose new model algorithms; instead it implements these requirements as a deployable engineering system. The current implementation uses **Linux + OpenClaw + Telegram** as its validation environment, with DeepSeek by default as the text model and Seedream as the image generation service.

**Keywords:** long-term companion agent; proactive interaction; cross-day memory; session lifecycle; event state machine; OpenClaw; deterministic orchestration

---

## I. Research Background and Problem Definition

### 1.1 From Reactive Dialogue to Sustained Operation

Large language models can already generate coherent multi-turn dialogue, but "being able to converse" and "being able to persist over the long term as a persona" belong to different levels of problem.

A typical dialogue system can be abstracted as:

```text
User Message
    ↓
Model
    ↓
Response
```

This structure defaults to each turn of behavior being triggered by the user message; the model only needs to interpret the current context and produce a reply. Long-term companionship scenarios additionally require the system to handle time, cross-session state, proactive behavior, long-term memory, and the persona's own life trajectory. When the user has not sent a message, the persona still needs internal state such as current time, location, activity, and emotion; when the user reappears the next day, the important events of the previous day should also continue to influence the conversation, rather than degenerating into an archive that can only be restored through explicit retrieval.

Generative Agents demonstrated the feasibility of sustained behavior simulation through the combination of memory, reflection, and planning [1]; MemGPT treats memory management in long-horizon interaction as an independent problem [2]; LD-Agent further studied event memory and dynamic persona in long-term dialogue [3]. Together these works show that long-term agents cannot rely only on the current dialogue window, but need explicit time and memory structures.

### 1.2 Five Engineering Constraints in Companionship Scenarios

xiaodou-system reduces the problem of long-term companionship operation to the following five constraints.

#### Temporal Proactivity

The system must be able to generate future events from the day's plan without immediate user input, and decide at a specified time whether to proactively reach out. Proactive behavior should originate from life state that the persona already has that day, not from asking the model on the spot each time "should we say something now."

#### Cross-day Continuity

Yesterday's important experiences must be able to directly influence the next natural day's planning and interaction. For extremely short messages with high dependence on prior events — such as "I've arrived," "I'm out now," "it's over" — the system must not require the user to explicitly say "go look up what happened yesterday" before restoring correct context.

#### Single Memory Authority

The same history should not be long-term interpreted by multiple mutually independent summarization systems. If session compaction, memory flush, and the day-end memory model each form a different summary of the same conversation, they can differ in timing, causality, uncompleted items, relationship state, or information priority. xiaodou-system therefore requires exactly one formal conversion path for cross-day semantics.

#### Operational Determinism

Whether an event executes twice, whether a message has already been sent, whether a Session may roll over, whether a memory file was written successfully — these must be decided by program state, not by the model's on-the-spot judgment.

#### Role Portability

Personality, identity, life routines, and user relationships should be expressed as readable, editable persona files rather than hardcoded into execution scripts. The framework should keep the core runtime protocol unchanged when the persona is replaced.

---

## II. System Model and Design Principles

### 2.1 Persona Model

xiaodou-system defines the persona using 7 core Markdown files in the workspace:

| File | Responsibility |
|---|---|
| `AGENTS.md` | Operating rules and persona-side behavioral constraints |
| `IDENTITY.md` | Identity and stable background information |
| `SOUL.md` | Personality, expression style, emotions, and interaction boundaries |
| `LIFE.md` | Life routines and the basis for the Planner's schedule generation |
| `USER.md` | The companion target and related long-term information |
| `TOOLS.md` | Tools and runtime environment description |
| `MEMORY.md` | Consolidated long-term memory |

Code does not branch on a persona's name, experiences, or expression style. Persona differences enter the generation stage, while scheduling, state transition, and persistence logic remain consistent.

Among them, `LIFE.md` describes stable life routines and `MEMORY.md` describes cross-day long-term state; together they participate in constructing a new DailyPlan. Replacing the set of persona files yields a different persona without modifying the main pipeline.

### 2.2 Time Model

The system treats a natural day as an explicit operational unit:

```text
Day N
├── DailyPlan
├── Scheduled Events
├── Executed Events
├── User Interactions
├── Working Session
└── Daily Memory
```

This means "time" is not merely a cron timestamp, but part of the persona's state. A 17:45 event is not just "execute a task at 17:45," but:

```text
Today's persona
    ↓
has already experienced the day's preceding activities
    ↓
reaches the 17:45 life event
    ↓
decides whether to interact with the user based on current state
```

The DailyPlan therefore serves as the "world state of the day," not merely a task list.

### 2.3 Memory Model

The system distinguishes two types of memory.

#### Working Memory

```text
The complete OpenClaw Session of the current natural day
```

Its characteristics:

- retains the day's raw interactions;
- retains proactive outreach and subsequent user replies;
- avoids cross-day semantic compression before day end as far as possible;
- serves as the high-fidelity evidence source for that day's facts.

#### Consolidated Memory

```text
Daily Memory + MEMORY.md
```

Its characteristics:

- generated uniformly by step04;
- represents history state that has completed day-end consolidation;
- directly participates in the next natural day's Planner and interaction context;
- periodically deduplicated, merged, and compressed.

There is only one formal conversion path between the two:

```text
Complete Working Session
+ DailyPlan
+ Event Journal
+ Proactive messages and user interactions
        ↓
step04
        ↓
Daily Memory / MEMORY.md
```

### 2.4 Persistence, Retrieval, and Continuity

A long-term companionship system cannot treat "the data still exists" as directly equivalent to "the persona still remembers."

```text
Persistence
= whether the information is still physically saved

Retrieval
= whether the current run can find that information again

Continuity
= whether the persona can naturally use it without user prompting
```

These three levels must be handled separately.

For example:

```text
Yesterday:
"I have an exam tomorrow afternoon."

Today:
"I'm heading out now."
```

The second sentence contains almost no strong semantic retrieval cues, yet its reasonable interpretation depends on yesterday's exam arrangement. If the system only restores history after the user appends "go look up what we said yesterday," then Persistence and Retrieval may both have succeeded, but Continuity has failed.

Therefore, xiaodou-system treats "yesterday's important state" as direct input to the next natural day, not merely as a searchable archive.

### 2.5 Generation Plane and Control Plane

The system clearly separates generation capability from operational control.

**Generation plane:**

- DailyPlan content generation;
- message copy generation;
- Daily Memory content generation;
- Seedream image generation.

**Control plane:**

- scheduled triggering;
- schema validation;
- idempotency judgment;
- `flock` concurrency locking;
- transaction state;
- file persistence;
- Session rollover;
- failure recovery.

Its design principle can be summarized as:

> **Generative intelligence inside deterministic boundaries.**

The model is responsible for "what to generate"; the program is responsible for "when to generate, whether execution is allowed, whether it has already been executed, where results are written, and how to recover after failure."

---

## III. Overall Architecture

### 3.1 Architecture Composition

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system architecture" width="880"/>
  <br/>
  <em>Fig. 1: Overall system architecture — three business pipelines with a persistent layer running throughout</em>
</p>

The system consists of four layers:

- **Persona contract layer**: the 7 Markdown files;
- **Business pipeline layer**: step02, step03, step04;
- **External dependency layer**: LLM, image service, OpenClaw Gateway, message channels;
- **Infrastructure and persistence layer**: Linux, `cron`, `at`, `flock`, daily, chatlog, MEMORY, logs, and backups.

### 3.2 External Dependency Abstraction

Third-party services are uniformly encapsulated in `providers/`:

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

Business logic depends on provider interfaces rather than directly on the specific SDK of a model vendor or messaging service. This allows replacing external implementations while keeping the DailyPlan, Event Model, and Memory Pipeline unchanged.

### 3.3 Persistent State

Primary persistence targets include:

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

The system prefers writing key state to structured files or logs rather than keeping it only in the current model context.

---

## IV. Natural-Day Operating Mechanism

### 4.1 step02: Daily Planning

**Default trigger time: 06:00**

Primary scripts:

- `build_daily_plan.py`
- `validate_daily_plan.py`
- `initialize_daily_file.py`
- `schema_engine.py`
- `schema_validator.py`
- `weather_provider.py`

Input:

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

The DailyPlan describes:

- the day's activities;
- locations;
- emotional states;
- outreach windows;
- silent / non-silent events;
- possible media behaviors such as selfies.

Model-generated output must pass JSON Schema validation before proceeding to the next stage. The schema guarantees structural contract validity; it does not guarantee that text semantics are fully correct, nor that identical input produces identical plans.

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

When a single event reaches its trigger time, it runs through the following path:

```text
Read DailyPlan and context
        ↓
Idempotency check
        ↓
Acquire flock
        ↓
Build Context Snapshot
        ↓
Decide whether the current event should still execute
        ↓
Generate message
        ↓
Optional: Seedream image generation
        ↓
Send Telegram
        ↓
Write into the OpenClaw Session
        ↓
Event Journal / Transaction write-back
```

An event is not a single "success / failure" boolean. The generate, send, inject, and final commit steps are each recorded, to support partial-failure recovery.

For example:

```text
delivery = succeeded
injection = failed
transaction = partial
```

In this case, recovery can only redo the Session injection, not resend the Telegram message, otherwise duplicate outreach may occur.

### 4.3 step04: Day-End Memory Compilation

Default schedule:

```text
00:20 / 00:50  finalize
02:00          incremental
02:03          attach
04:00          session_rollover
Sun 04:30      weekly compress
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

Day-end input includes:

```text
Complete daily Session
+ DailyPlan
+ Actual event execution state
+ Event Journal
+ Proactive messages
+ User replies
+ Day artifacts
```

Its output is not a simple chat summary, but an episode-level cross-day state representation. The system needs to answer:

- what was planned today;
- which events actually occurred;
- which events were suppressed;
- what the persona proactively said;
- how the user replied;
- which state still affects the next day;
- which information should enter long-term MEMORY.

### 4.4 Session Rollover

xiaodou-system treats each daily Session as that day's high-fidelity working memory.

Rollover must occur after the main memory processing:

```text
Day N Complete Session
        ↓
Daily Memory
        ↓
MEMORY update
        ↓
Main persistence completes
        ↓
Session Rollover
        ↓
Day N+1
```

Its key constraint is:

> **Memory processing precedes rollover.**

The purpose of rollover is not to forget, nor because the Session "expired," but to establish a clear natural-day boundary and, before a long Session enters an uncontrollable historical summary, hand that day's complete evidence to the single cross-day Memory Pipeline.

### 4.5 The Cross-Day Closed Loop

```text
Yesterday's Consolidated Memory
        ↓
Today's Planner
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
New Consolidated Memory
        ↓
Rollover
        ↓
Next natural day
```

Daily Memory is therefore used not only to preserve the past, but to construct the future state.

---

## V. OpenClaw Capability Boundaries and Adaptability Analysis

### 5.1 OpenClaw's Basic Capabilities

As of 2026-08-16, OpenClaw already has generic infrastructure highly relevant to this project, including:

- Agent workspace and context files [4];
- Gateway-owned Session and transcripts [5][6];
- Markdown memory, memory search, and memory-core [7];
- Dreaming and consolidation [8];
- Active Memory [9];
- Compaction and automatic memory flush [7][10];
- message channels such as Telegram [11];
- the Automations scheduler [12].

Therefore, xiaodou-system does not re-implement a generic Agent Runtime. Its addition lies at a higher application-semantic layer:

```text
OpenClaw
provides Agent Runtime primitives

        ↓

xiaodou-system
defines the natural-day lifecycle of a companion persona
```

The relationship between the two is not "OpenClaw lacks a feature, so we build a new set of features," but "OpenClaw's generic mechanisms cannot automatically derive the time, event, and cross-day state constraints that a companion persona needs."

### 5.2 Native Memory and Cross-Day Continuity

OpenClaw memory files can be persisted and retrieved by `memory_search` / `memory_get`; Active Memory can also proactively recall history in qualifying interactions [7][9].

This model is reasonable for a general agent: only retrieve history when the current task is related, reducing context usage and irrelevant information.

Companionship scenarios have different constraints.

A large amount of natural-language carryover contains no explicit recall intent. For example:

```text
Yesterday:
"I have an exam tomorrow afternoon."

Today:
"I'm heading out now."
```

The current message does not explicitly ask the system to recall yesterday, nor does it necessarily form a high-quality semantic query; yet for a companion persona, yesterday's exam arrangement is already part of the current relationship state.

Therefore, xiaodou-system does not treat "memory search can eventually find yesterday" as a sufficient condition for cross-day continuity. Recent key state must already be part of the day's working state before the user's current message arrives.

Formally:

```text
OpenClaw generic retrieval path:

Current Query
    ↓
Decide whether history is needed
    ↓
Search
    ↓
Past Memory


xiaodou-system recent-continuity path:

Past Episode
    ↓
Current State
    ↓
Interpret Current Query
```

The two are not in conflict. Distant history can still use OpenClaw memory search; but yesterday's important events do not depend on ad-hoc retrieval to hold.

### 5.3 Session Reset and Memory Commit Not Yet Completed

The physical existence of a Session transcript is not equivalent to the persona being able to use that content now.

Even if old history is still stored in a transcript, SQLite, or other archive, if the new Session lacks the corresponding state, the user may still see:

```text
Yesterday:
The user already said they are going to the hospital today.

Today:
User: "I've arrived."

Persona:
"Arrived where?"
```

From the standpoint of underlying storage, history may not have been lost; from the standpoint of companionship interaction, continuity has already been interrupted.

Therefore, xiaodou-system does not treat "old transcripts are recoverable" as a normal memory mechanism, but only as an audit and disaster-recovery capability. Normal cross-day state must be explicitly committed before the Session boundary.

### 5.4 Compaction and Dual Summary Authority

OpenClaw's compaction is used to control the context size of long Sessions. Earlier conversations are summarized into a persistent compaction entry, which is then used with:

```text
Compaction Summary
+
Recent uncompressed messages
```

to continue the current Session [6][10].

In a general agent, this is a necessary context-management mechanism.

xiaodou-system already has another cross-day summarization chain:

```text
Complete Session
+ DailyPlan
+ Event Journal
+ Proactive messages
+ User replies
        ↓
step04
        ↓
Daily Memory / MEMORY.md
```

If the same history is allowed to pass first through OpenClaw compaction and then through the xiaodou-system Memory Compiler, later models may read both:

```text
OpenClaw Compaction Summary
            +
xiaodou Daily Memory
```

Even if neither has obvious factual errors, they can differ in detail retention, time ordering, event causality, uncompleted items, relationship state, and importance judgment.

This problem is not "data loss" in the traditional sense, but **semantic authority fragmentation**:

```text
The same history
    ├── Summary A
    └── Summary B

Two summaries simultaneously become the basis for future reasoning
```

A companionship system needs stable interpersonal state, so such uncertainty is unacceptable.

xiaodou-system adopts the **Single Memory Authority** principle:

> The day's complete Session serves as raw working evidence; cross-day consolidated memory is formally generated only by step04.

This does not mean OpenClaw compaction itself is wrong; rather, its responsibility overlaps with this project's cross-day Memory Compiler. If both assume canonical history, they create unnecessary semantic conflict.

### 5.5 Necessity of Daily Rollover

OpenClaw can maintain a persistent Session and also supports configurable daily / idle reset [5][6].

xiaodou-system still actively performs daily rollover to establish a deterministic boundary between two goals:

1. **keep the day's complete raw Session as much as possible;**
2. **after a cross-day boundary, only use state that has passed through the project's own Memory Pipeline.**

If the Session continues indefinitely:

```text
Day 1
+ Day 2
+ Day 3
+ Day 4
...
```

it will inevitably hit the context-window constraint and enter compaction.

If it is reset each day before memory is complete, information from that day that has not yet entered consolidated memory may leave the current working context.

Therefore it adopts:

```text
Complete single-day Session
        ↓
Memory Commit
        ↓
Rollover
```

Rollover effectively constitutes a **Memory Commit Boundary**.

### 5.6 Automations and the Daily Event Model

OpenClaw Automations can already execute one-shot and recurring tasks [12].

But a scheduler solves:

```text
when to run a certain task
```

while xiaodou-system needs to define:

```text
what this event means in the persona's life today
```

For example, also at 17:45:

```text
Scheduler:
17:45 execute job A
```

The Daily Event Model additionally contains:

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

So Automations can replace part of the current `cron + at` backend, but cannot replace the DailyPlan and Event Model.

The current version uses `cron + at` because its runtime chain has already been validated around:

```text
Linux process
+ file state
+ flock
+ event journal
+ transaction
```

and scheduling state can be inspected independently of the Gateway.

In the future, an OpenClaw Automations adapter can be added, as long as the semantics of the DailyPlan, Event Model, and Memory Pipeline remain unchanged.

### 5.7 Active Memory and Recent Working State

Active Memory improves the problem of purely on-demand retrieval, but it still belongs to the mechanism of "after the current interaction occurs, decide what to recall based on the current input" [9].

xiaodou-system's recent-memory goal is more strict:

> Before the user message arrives, yesterday's key state has already become today's running state.

Therefore the two serve different levels:

```text
xiaodou recent-state continuity
responsible for: yesterday / today's key state

OpenClaw Active Memory / memory search
responsible for: more distant, topic-based, semantic supplementary recall
```

This is a complementary relationship, not a substitution.

### 5.8 Adaptation Conclusion

OpenClaw has already solved generic Agent Runtime problems such as Session, Memory, Search, Compaction, Channel, and Automation, but these capabilities do not by default include the following application-layer constraints:

```text
the natural day as a state boundary
DailyPlan as the world state of the day
events belong to the persona's life, not merely background tasks
yesterday's key state must directly participate in today
keep the single-day Session as high-fidelity raw text as much as possible
only one canonical compiler for cross-day history
Memory Commit must precede Rollover
```

The necessity of xiaodou-system is constituted jointly by these constraints, rather than by the absence of any one specific OpenClaw feature.

---

## VI. Reliability and State Consistency

### 6.1 Data Contracts

The project uses JSON Schema to structurally constrain cross-stage data, currently including 19 schemas.

Schemas are used to detect:

- missing required fields;
- type errors;
- out-of-range enum values;
- model outputs that do not conform to the protocol.

Schemas cannot guarantee that natural-language facts are correct, nor that different model calls generate identical content.

### 6.2 Idempotency and Concurrency Control

step03, through:

- stable event identity;
- idempotency state checks;
- `flock`;
- Event Journal;
- Transaction State;

avoids duplicate sends caused by cron re-entry, duplicate `at` triggers, or manual reruns.

### 6.3 External Side-Effect Transactions

Message sending and Session injection are two independent side effects.

If:

```text
Telegram send = succeeded
chat.inject = failed
```

the event cannot be retried as a whole, otherwise it may be sent twice.

The system therefore records, separately:

```text
generated
delivered
injected
committed
```

Recovery logic decides the compensation operation based on which stage has completed.

### 6.4 Backup and Audit

The project provides:

- `backup_openclaw.py`
- `raw_backup.py`
- `rollover_artifacts.py`

Backups cover runtime configuration, long-term memory, chat records, and related event artifacts.

The purpose of backup is to preserve evidence and disaster-recovery material, not to replace the normal memory-continuity mechanism.

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

During operation:

```text
daily/
```

stores plans and event state;

```text
chatlog/
```

stores chat records;

```text
daily_selfies/
```

stores image artifacts;

```text
MEMORY.md
```

stores the consolidated long-term state.

These files together form the primary input for both day-end memory and fault diagnosis.

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
- Python dependencies:

```bash
pip install -r requirements.txt
```

### 8.2 OpenClaw Initialization

```bash
openclaw configure
openclaw health
```

At minimum you need to complete:

- a usable model/provider configuration;
- Gateway startup and health checks;
- the Telegram channel;
- workspace / session correspondence.

If the default port is occupied, configure another Gateway port, for example:

```bash
openclaw config set gateway.port 19203
```

### 8.3 xiaodou-system Initialization

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` performs:

1. environment checks;
2. persona core-file preparation;
3. `settings.yaml` generation;
4. runtime directory creation;
5. script and configuration validation;
6. optional cron installation.

Default instance directory:

```text
~/.openclaw/workspace
```

Another location can be specified with `--instance-dir`.

Existing persona files are not actively overwritten by the initialization process; templates or skeletons are created only for missing files.

### 8.4 Setup Wizard

```bash
python3 scripts/guided_setup.py
```

The wizard handles:

1. Python / atd / OpenClaw / timezone checks;
2. core-file checks;
3. API key configuration;
4. Telegram token and chat id binding;
5. Gateway Session binding;
6. xiaodou-system memory-contract configuration;
7. final validation;
8. Gateway configuration activation.

Available modes:

```text
--non-interactive
--verify-only
```

### 8.5 Scheduled Tasks

```bash
bash init.sh --instance-dir ./instance --install-cron
```

When `ALLOW_CRON_APPLY=1`:

- root user writes to `/etc/crontab`;
- non-root user writes to the user-level crontab.

Default schedule:

```text
06:00           step02-morning
06:20           step03-morning
00:20 / 00:50   finalize
02:00           incremental
02:03           attach
04:00           session_rollover
Sun 04:30      weekly compress
```

step03 depends on `atd`.

### 8.6 Configuration

Runtime configuration is centralized in:

```text
settings.yaml
```

Primary configuration domains:

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

Persona personality and life content should remain in Markdown files rather than in Python control logic.

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
├── scripts/                   # planner / validate / execute / memory
├── providers/                 # LLM / image / gateway / delivery
├── prompts/                   # per-stage model prompts
├── schemas/                   # JSON Schema
├── cron/                      # step02 / step03 / step04
├── docs/                      # architecture diagram and runtime screenshots
└── settings.example.yaml
```

A running instance also produces:

```text
daily/
chatlog/
daily_selfies/
event journal
transaction state
backups/
```

---

## X. Limitations and Future Work

### 10.1 Validation Scope

The current system has only been validated end-to-end on Linux.

Not yet established:

- a multi-distribution compatibility matrix;
- Windows / macOS scheduling adaptation;
- long-cycle failure-rate and SLA data;
- a proactive-message quality evaluation benchmark;
- systematic migration tests across different personas.

### 10.2 Single-Day Context Budget

The system hopes to keep the complete, uncompacted Session within a natural day as far as possible, but the model context window still has a hard ceiling.

If the volume of messages, tool outputs, or proactive events within a day is unusually high, compaction may still be triggered before rollover. The current version therefore cannot interpret "daily rollover" as the absolute elimination of compaction.

High-interaction-volume scenarios will later need:

- single-day context budget monitoring;
- controlled day-internal checkpoints;
- or a dedicated context adapter.

### 10.3 Platform Dependencies

The current control plane depends on:

```text
cron
at / atd
flock
```

Windows and macOS require replacing the platform-specific scheduler / lock adapter, while keeping the data contracts of the DailyPlan, Event Model, and Memory Pipeline unchanged.

### 10.4 Message Channels

The currently validated channel is Telegram.

Other message channels need separate validation of:

- text sending;
- media sending;
- identity binding;
- Session mapping;
- proactive-message write-back.

### 10.5 External Models and Services

DeepSeek and Seedream are external services. Model names, APIs, pricing, rate limits, and availability may change, so production deployments should manage them via provider configuration.

---

## XI. Conclusion

xiaodou-system models a long-term companion agent as a sustained operating system bounded by the natural day, rather than a dialogue interface driven only by user input.

Its core state transition is:

```text
Persona and historical state
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
Next natural day
```

The system's main engineering contribution lies not in re-implementing OpenClaw, nor in proposing new language-model algorithms, but in making explicit four long-term companionship operating constraints:

1. **Separation of persona content from runtime code;**
2. **Separation of the generative model from the deterministic control plane;**
3. **Separation of the Working Session from Consolidated Memory;**
4. **Only one canonical cross-day semantic conversion path for the same history.**

OpenClaw provides generic Agent Runtime capabilities such as Session, Memory, Search, Compaction, Channel, and Automation; xiaodou-system further defines, on top of these primitives, a time model, DailyPlan, an event state machine, proactive interaction transactions, and a cross-day memory protocol.

For a companion agent, what truly needs to be maintained is not that "historical data still exists," but that the persona can naturally bring yesterday into today without relying on the user's explicit reminder.

---

## References

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

> OpenClaw capability boundaries were verified against official documentation on **2026-08-16**. Later versions may adjust default Session, Memory, Compaction, or scheduling behavior; at deployment, follow the official documentation of the installed version.

---

## License

MIT © xiaodou-system
