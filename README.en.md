# xiaodou-system

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux-blue" alt="platform"/>
  <img src="https://img.shields.io/github/v/release/SHIJINS66/xiaodou-system?color=green&label=release" alt="release"/>
  <img src="https://img.shields.io/badge/channel-Telegram-lightgrey" alt="channel"/>
  <img src="https://img.shields.io/badge/docs-简体中文-blue?label=%E4%B8%AD%E6%96%87" alt="docs-zh"/>
  <a href="README.md"><img src="https://img.shields.io/badge/lang-简体中文-blue" alt="lang-zh"/></a>
  <img src="https://img.shields.io/badge/lang-English-brightgreen" alt="lang-en"/>
</p>

## Abstract

This paper proposes and implements a **persona-agnostic** runtime framework for "companion agents" — **xiaodou-system**. It organizes three capabilities into a deterministic daily pipeline: **automatically generating a persona's daily life trajectory**, **proactively reaching out to the user at appropriate moments**, and **consolidating the day's conversations into long-term memory at day's end** — enabling a text-and-image conversational agent to run autonomously, stably, and unattended over long periods.

At its core is a design principle: **separation of persona from system**. A persona is not hard-coded into scripts, but driven by a set of core files defined by the user in the workspace; the system is responsible only for deterministic operations such as scheduling, execution, and memory. Thus, replacing one set of persona files yields a companion with entirely different behavior and temperament, and the framework can be freely reused and extended.

On the implementation side, the framework runs on top of [OpenClaw](https://docs.openclaw.ai). Calls to external model services (DeepSeek, Seedream), message delivery, and gateway interactions are decoupled through a `providers/` abstraction layer; all remaining steps are carried out by deterministic scripts, backed by JSON Schema validation, idempotency gates, file locks, and state write-back to ensure reproducibility, recoverability, and auditability. The framework is extracted from a long-running online companion system of the same name, preserving full functionality while removing persona and platform coupling, and is released as general-purpose infrastructure.

---

## I. Background and Motivation

### 1.1 Background

Advances in large language models have given conversational systems the ability to express natural and coherent personas. However, advancing from "a one-off conversational response" to "a sustainable, long-lived companion" remains a significant gap: a true companion agent should not merely respond when the user initiates, but autonomously live, think, remember, and reach out at the right moments without being prompted.

This agent paradigm — variously explored under the labels "AI companion" and "virtual character" — mostly remains at the demonstration stage. Systems either rely on pre-scripted dialogs and lack real autonomous living; or depend entirely on the model's incidental output and become unpredictable and unrecoverable; or are so tightly coupled to a specific character that they cannot be reused.

### 1.2 Three Foundational Problems

To move companion agents from demonstration toward a long-running, stable, autonomous service, we distill the core challenges into three mutually related problems:

- **Proactivity** — How can the system, without user triggers, autonomously decide "whether, when, and in what manner" to communicate, and gracefully sense the user's availability to modulate its intensity;
- **Continuity** — How can interaction information be sedimented and organized into sustainable, ever-growing long-term memory, rather than perishing with the end of a session; and how can the persona evolve on top of yesterday when a new day begins;
- **Determinism** — For a system expected to run unattended for long periods, behavior must be predictable, verifiable, and recoverable — reliability must not rest on the model's incidental output each time.

### 1.3 Limitations of the Status Quo and Motivation

Existing solutions typically address only one of these dimensions: pre-scripted dialogs deliver proactivity but sacrifice authenticity; memory plugins deliver continuity but lack proactive outreach; and approaches that "leave it to the model's improvisation" entirely forfeit determinism. Rarely does a single solution unify all three while remaining persona-swappable and deployable.

The motivation behind this project is precisely this: **to provide a unified, deployable, reusable engineering answer to these three problems**, and to detach the system from the specific persona with which it has been bound for years, turning it into a piece of infrastructure capable of hosting any persona.

---

## II. Significance and Value

### 2.1 Theoretical Significance: Validating Persona–System Decoupling

By defining persona through a set of core files while delegating deterministic operations to the system, the framework demonstrates the feasibility of **fully decoupling personality from runtime system**. All behavioral tendencies, tone, values, boundaries, and daily routines of a persona are carried by readable, editable text declarations (e.g., `AGENTS.md`, `SOUL.md`, `LIFE.md`) rather than scattered through code logic. This reduces "changing a persona" from a code-level refactor to an inexpensive configuration-level operation, offering a viable paradigm for low-cost, large-scale reuse of companion agents.

### 2.2 Practical Significance: From Demonstration to Long-Running Service

The framework concentrates on **deterministic engineering**: every step is Schema-validated, guarded by idempotency gates and file locks, and accompanied by complete state write-back and backup archival. This design lets the system run day after day on an unattended server without depending on manual intervention or the stability of model output. For developers who wish to deploy long-term AI companions, virtual characters, or persona-driven assistants, this is a ready-to-use and trustworthy engineering foundation.

### 2.3 Openness: Binding-Free General Infrastructure

The system deliberately strips out everything tied to a specific persona, scenario, or platform, retaining only general logic and releasing it as open source. It is both a distillation of the author's years of practical experience and a reusable asset for the community; anyone may define their own persona on top of it or extend new platforms and capabilities as needed.

---

## III. System Architecture

### 3.1 Overall Design

<p align="center">
  <img src="docs/architecture.svg" alt="xiaodou-system architecture" width="880"/>
  <br/>
  <em>Fig. 1: Overall system architecture — three-layer business pipeline with a persistent layer running throughout</em>
</p>

The system comprises **three serial business pipelines** and a **persistent layer running throughout**, with external dependencies uniformly integrated through an abstraction layer.

### 3.2 The Three-Layer Business Pipeline

- **step02 · Daily Planning**: Reads the persona's core files and daily weather to generate a full day's life trajectory — including activities, locations, emotional states, and available interaction windows; the output is validated against a JSON Schema and persisted, retried on failure, eliminating randomness at generation time.
- **step03 · Scheduling & Execution**: Submits the "non-silent" events requiring proactive outreach to the operating-system-level `at` timer; when a trigger fires, it executes the full outreach chain — sensing the user's availability, generating an adapted message, synthesizing a selfie via the image service when needed, delivering through the message channel, and finally writing the result back to the day's journal.
- **step04 · Day-End Memory**: At the end of the day, consolidates chat records and all events into a fused daily memory; merges it into the long-term memory file via incremental and append modes; every Sunday uses the language model to deduplicate, merge, and compress the long-term memory body before rewriting it, keeping its size bounded over time; and completes session reset and carryover in the early morning.

### 3.3 Persistent Layer

All configuration, runtime state, logs, backups, and long-term memory are persisted to the working directory and periodically archived and validated. The backup chain covers three data classes — sessions, memory, and raw evidence — ensuring traceability and recovery at any moment.

### 3.4 Deterministic Design Principles

Four principles run through the entire pipeline and together ensure predictability and recoverability:

- **Contract-First** — Inputs and outputs at each stage are defined by JSON Schemas as data contracts, giving cross-stage data exchange a firm basis;
- **Idempotency Gate** — Each event has a stable identifier, so the same event is never delivered twice due to repeated triggers;
- **Concurrency & Locking** — Critical operations are mutually excluded with file locks (flock) to prevent state corruption from concurrent writes;
- **State Write-Back & Audit** — Each execution is traceable down to day, event, status field, and exception, with complete run logs and evidence files.

### 3.5 Abstraction and Integration

Interactions with language models, image generation, message channels, and the OpenClaw gateway are uniformly encapsulated in the `providers/` abstraction layer, so swapping model suppliers, adding message channels, or integrating a new gateway becomes a localized change rather than a structural one.

### 3.6 The Development Process: Rebuilt from OpenClaw

This framework was not written from scratch. Instead, it was **rebuilt step by step on top of OpenClaw, a general-purpose agent runtime, by supplementing the deterministic scheduling and memory operations that a companion system requires**. Understanding this journey helps users see which capabilities OpenClaw provides natively and which this framework adds — so it is easier to reason about usage and further development.

**What OpenClaw provides natively**

OpenClaw is a general-purpose agent runtime. This framework reuses several of its capabilities:

- **A persistent agent**: a set of Markdown files in the workspace (e.g., `MEMORY.md` for long-term memory, `memory/` for dated notes) carry the agent's identity, rules, and memory; they load automatically on session start, and the model only remembers what has been written to disk — no hidden state;
- **Session and gateway lifecycle**: session creation, history reading, and message injection (`chat.inject`) are all managed by the OpenClaw gateway, so the framework does not need to build its own session management;
- **Channel integration**: message channels such as Telegram and model providers such as DeepSeek can be configured natively by OpenClaw.

**What this framework adds on top**

OpenClaw is generative and responsive: it replies, but it will not proactively, on its own schedule, run a day and reach out to the user; and it relies on the model's incidental output, lacking strong deterministic operations. These are exactly the gaps this framework fills. During development we also uncovered several concrete limitations of OpenClaw and addressed each with a systematic change:

- **Limited proactivity: heartbeat is constrained and uncontrollable.** OpenClaw's built-in heartbeat mechanism can only wake the model within a single session — it cannot span sessions or fire at a chosen time in a predictable rhythm, so it cannot proactively reach the user at a specific moment. This framework therefore does not rely on heartbeat to drive proactive behavior; instead it orchestrates step02 / step03 / step04 with operating-system-level `cron` (fixed-time triggers) and `at` (one-shot timers), turning proactivity from "the model happens to wake" into "a deterministic system rhythm";
- **Proactivity**: on top of that scheduling, this framework adds the step02 daily planner (reads core files and weather to produce a full-day trajectory) and, via step03, submits events to the operating-system `at` timer — achieving genuinely autonomous outreach;
- **Determinism**: Native agent sessions are driven by model generation. The framework wraps them in a layer of deterministic engineering — JSON Schema contract validation, idempotency gates (the same event is never delivered twice), file locks (flock to prevent concurrent corruption), and state write-back with audit—turning these risky steps from "left to the model" into "handled by scripts";
- **Unstructured, ever-growing memory**: OpenClaw's memory writes and dream (recall) mechanism add some value, but in the long run it keeps writing into the same `MEMORY.md`, which only grows monotonically — piling up without organization and never converging. The framework's step04 takes this over with deterministic scripts, structuring memory into layers (daily Fused Memory → incremental merge → weekly LLM dedup/compress rewrite) and keeping the body size bounded (≤5000 chars), so long-term memory is a tidy, growing asset rather than an endlessly accumulating dump;
- **Visibility of proactive injections — letting the model see what it said**: OpenClaw's proactive message injection (`chat.inject`) does not, by default, necessarily return the persona's own just-sent messages into the session context; without handling this, the model may not see its own prior proactive outreach when woken, breaking the continuity between its words and memory. The framework writes the actual sent text, event type, scene, and image path back into the session history on the same injection path, so the model can fully see what it proactively said and why on the next wake — keeping its behavior and memory coherent;
- **Integration approach**: the framework reuses the OpenClaw gateway through the `providers/` abstraction layer — reading session history, resolving the sessionId, and injecting proactive messages via `chat.inject` — while also bringing model calls (DeepSeek) and image generation (Seedream) into the same abstraction so external dependencies are replaceable local pieces rather than structural coupling.

**The essence of the rebuild**

If OpenClaw is like the brain and nervous system that "thinks and converses," then what this framework adds is the daily rhythm and deterministic skeleton that "lives, remembers, and proactively reaches out at the right time." The core of the rebuild is not rewriting conversational ability, but **layering a deterministic scheduling-and-memory engineering shell on top of a generative runtime** — so an agent can truly run day after day in a predictable, recoverable way.

---

## IV. Demonstration

The figures below show the framework in **normal operation** in two parts: one shows real conversations between the persona and the user on Telegram; the other shows the data the system continuously persists in the background (chat logs, daily plans, selfie archives).

### 4.1 Telegram Conversations

The persona autonomously generates conversations from its own life trajectory, senses timing to reach out proactively, and shares selfies at appropriate moments:

<p align="center">
  <img src="docs/screenshot-telegram.jpg" alt="Telegram conversations in normal operation" width="800"/>
  <br/>
  <em>Fig. 2-a: Day-to-day Telegram conversations and selfies by the persona</em>
</p>

### 4.2 File Persistence

As the framework runs, every experience is automatically persisted to files: `daily/` stores the daily life plan, `chatlog/` stores conversations with the user, and `daily_selfies/` stores the auto-generated selfie archive. Long-term memory keeps accumulating into `MEMORY.md`:

<p align="center">
  <img src="docs/filesystem-directories.jpg" alt="auto-persisted daily / chatlog / daily_selfies directories" width="800"/>
  <br/>
  <em>Fig. 2-b: The auto-persisted daily / chatlog / daily_selfies data directories</em>
</p>

---

## V. Deployment

### 5.1 Environment

The framework currently supports **Linux**:

- Linux, Python 3.10+
- `at` + `atd` service (`systemctl enable --now atd`)
- Timezone `Asia/Shanghai`
- [OpenClaw](https://docs.openclaw.ai) installed (`openclaw` CLI in PATH; run its own setup first)
- `pip install -r requirements.txt` (PyYAML, openai)

> The latest release can be downloaded directly from [GitHub Releases](https://github.com/SHIJINS66/xiaodou-system/releases), or you can `git clone` this repository.

### 5.2 Step 1: Configure OpenClaw Itself

The framework's proactive messaging and memory depend on OpenClaw, so finish its own setup first:

```bash
openclaw configure        # interactive guide: gateway / primary model / Telegram bot
openclaw health           # verify health
```

- Configure the agent's primary model (framework uses DeepSeek, `deepseek/deepseek-v4-flash` + API key)
- Bind the Telegram bot (create via @BotFather, paste the token)
- **Port conflict**: if an existing OpenClaw instance occupies the default port (e.g., 18789), switch this instance's port: `openclaw config set gateway.port 19203`, then `openclaw gateway start`.

### 5.3 Step 2: Initialize the Instance

```bash
git clone https://github.com/SHIJINS66/xiaodou-system.git
cd xiaodou-system
bash init.sh
```

`init.sh` sets the instance directory to `~/.openclaw/workspace` by default (overridable with `--instance-dir`), and: validates the environment → copies core file templates into the workspace (skipping existing ones, creating missing ones such as `LIFE.md`/`MEMORY.md`) → generates `settings.yaml` → creates directories → runs full compilation validation.

### 5.4 Step 3: Run the Setup Wizard

```bash
python3 scripts/guided_setup.py
```

The wizard walks through the full chain in order:

1. **Environment check** (python / atd / openclaw / timezone)
2. **Prepare core configuration** — the 7 core md files live at the workspace root and are read automatically by OpenClaw; the user uploads files with the same names to overwrite the content.
3. **Configure API keys** into `.env`
4. **Telegram permission binding** (create via @BotFather → paste token → verify); after approving pairing, the wizard **automatically captures and records the chat id** — no manual entry needed.
5. **Gateway session binding** (fill in `scheduling.gateway.session_key`; the session id is resolved automatically)
6. **OpenClaw memory contract in place** (automatically disables the native memory writer to avoid double-write conflicts)
7. **Final validation** (continue only when everything passes)
8. **Restart the gateway to apply**

> **Ops note**: Do not wrap injection commands (e.g., `execute_daily_event.py` and cron/at-triggered scripts) with `timeout <seconds>`. Generation and injection can take tens of seconds; `timeout` will kill the process midway, leaving a message generated but not injected/delivered. The framework's cron/at templates carry their own flock lock and add no timeout; scripts exit safely on timeout or exception.
>
> `--non-interactive` uses defaults (CI / scripting); `--verify-only` runs only the final validation.

### 5.5 Step 4: Install Scheduled Tasks (Required)

> **This step is not optional — it is required.** The framework's autonomy depends entirely on scheduling: step02 (daily planning) and step04 (day-end memory) are both triggered by cron, and step03 relies on `at` for its one-shot execution. **Without installing the scheduled tasks, none of the three pipelines will ever run automatically**, and the system cannot run a day on its own.

```bash
bash init.sh --instance-dir ./instance --install-cron
```

Writes to crontab when `ALLOW_CRON_APPLY=1` (otherwise prints only):

- **root user** → system crontab `/etc/crontab` (commands prefixed with a `root` user field)
- **non-root user** → user-level crontab (`crontab -`, no user field)

Installs three pipeline schedules:

- `step02-morning` — generates the day's plan at 06:00 daily
- `step03-morning` — submits non-silent events to `at` at 06:20 daily
- `step04-nightly` — finalize / incremental / attach / session_rollover / weekly compress overnight

> A non-root user needs `atd` enabled on the system (at least root has `at` + `atd` installed) for step03's `at` scheduling to run.

### 5.6 Core Files

The persona is defined by 7 core files at the workspace root, with templates copied by init:

| File | Defines |
|---|---|
| `AGENTS.md` | Operating rules: proactive messaging / selfies / memory / reply cadence |
| `IDENTITY.md` | Fixed identity: who I am / appearance / family / relationships |
| `SOUL.md` | Personality / emotions / speaking style / boundaries |
| `LIFE.md` | Lifestyle and routine (basis for the Planner) |
| `USER.md` | The companion: who they are / preferences / boundaries |
| `TOOLS.md` | Tool and script documentation (provided by the system) |
| `MEMORY.md` | Long-term memory (maintained by the daily memory tasks) |

> Existing files are skipped by init and never overwritten; only missing ones get a new skeleton. To customize any file, simply upload a same-named file to the workspace.

---

## VI. Directory Structure

```
├── init.sh                    # Installer (validate → copy templates → settings → compile → cron)
├── AGENTS.md / IDENTITY.md / SOUL.md / LIFE.md / USER.md / TOOLS.md / MEMORY.md
│                              # Core file templates (copied to workspace by init)
├── scripts/                   # Deterministic execution scripts (planner / validate / execute / memory)
├── providers/                 # External dependency abstraction (LLM / image / gateway / delivery)
├── prompts/                   # Stage model prompts (highly generic, built-in)
├── schemas/                   # Per-stage JSON Schemas (v1 series)
├── cron/                      # Cron templates (step02 / 03 / 04)
├── docs/                      # Docs: architecture diagram, runtime screenshots
└── settings.example.yaml      # Data contract (runtime config template)
```

---

## VII. Current Limitations and Roadmap

The framework is currently at the **single-environment validation** stage; the following are known limitations, all on the roadmap.

### 7.1 Platform Support

- **Currently supported**: Linux. All pipelines, both cron forms, and `at` scheduling have been validated end-to-end in a real Linux environment.
- **Planned**: Windows and macOS. The `at` / `flock` scheduling mechanisms require platform-specific rework and will be advanced in future versions.

### 7.2 Message Channels

- **Currently supported**: Telegram plugin.
- **Planned**: WeChat and more channels. The `providers/` delivery abstraction is ready for multiple channels, but the WeChat plugin is not yet integrated.

### 7.3 Engineering Items

- Scheduling optimization for trigger-based cron under a non-root-user `atd`
- More built-in persona prompt samples
- Lint / CI integration

---

## VIII. Conclusion

This paper proposes and implements a persona-agnostic companion-agent framework capable of long-term unattended operation. Guided by the triad of "persona files driving + deterministic engineering + abstraction-layer integration," the system unifies proactive outreach, long-term memory, and predictability/recoverability within a single daily pipeline, sheds specific persona coupling, and opens up as general-purpose infrastructure. The framework has been validated end-to-end in a real Linux environment, and will continue to extend platform and channel support and refine its engineering practices.

It is both an engineering answer to "how companion agents can move toward long-running operation" and an open-source foundation that any persona can adopt at low cost.

---

## License

MIT © xiaodou-system
