# Agentic Coding Test

This repository is used to test the multi-agent development environment.

## Multi-Agent Development Environment

The repository is used to test isolated worktrees, OpenCode workers, OpenRouter free routing, Codex escalation, tmux supervision, and Treehouse leasing.
GNHF final smoke verified

## Agent Run Observatory

First Mate automatically writes one append-only JSON record per completed run to
`.agent-runs/<run_id>.json`. Records contain the agent and dynamically available
model/provider metadata, a hashed task identifier, branch and worktree, start and
finish times, duration, final status, test outcome, files and lines changed, and
commits created. Full prompts, conversations, credentials, secrets, and environment
variable contents are not collected. Optional values may be `null`, and telemetry
failures never change the result of the underlying agent run.

Run the observatory report from the repository root:

```console
./scripts/agent-stats
```

Example output:

```text
AGENT RUN OBSERVATORY
=====================
Total runs:       4
Successful runs:  3
Failed runs:      1
Success rate:     75.0%
Average duration: 92.5s

By agent:
  codex: 1 runs, 100.0% success
  opencode: 3 runs, 66.7% success

Files changed:    9
Lines added:      184
Lines removed:    37
Commits created:  4
Runs with tests:  3
Tests passed:     3
```

The report skips malformed records rather than failing. This lightweight history
makes it possible to compare agent reliability, test usage, run time, and change
size across the multi-agent workflow without retaining sensitive task content.

```mermaid
flowchart LR
    Captain --> FirstMate[First Mate]
    FirstMate --> Agents
    Agents --> Worktrees
    Worktrees --> Telemetry[Telemetry records]
    Telemetry --> Observatory[Agent Run Observatory]
```

## Shared Agent Memory

The repository includes a local shared-memory store for validated project knowledge
and a deterministic context broker that selects relevant records for each worker.
Memory capture is explicit, provenance-aware, lifecycle-managed, and protected
against duplicate and secret-bearing records. Context packets use an approximate
token budget and require no model, embeddings, database, network API, or external
service.

```console
./scripts/agent-memory add --type COMMAND --tags testing \
  --summary "Run repository tests with python3 -m unittest discover"
./scripts/agent-memory search testing
./scripts/agent-memory stats
./scripts/agent-context --task "improve agent evaluation tests" --budget 1000
```

```mermaid
flowchart LR
    Captain --> FirstMate[First Mate]
    FirstMate --> Broker[Context Broker]
    Broker --> Memory[Relevant Shared Memory]
    Memory --> Packet[Context Packet]
    Packet --> Worker[Worker Agent]
    Worker --> Discoveries[Validated Discoveries]
    Discoveries --> Memory
```

MODEL CONTEXT is temporary information for one run. PROJECT MEMORY is durable,
validated repository knowledge. TELEMETRY records what happened during a run.
EVALUATION determines whether the result was acceptable. See
[Shared Agent Memory and Context Broker](docs/agent-memory.md) for architecture,
security, ranking, lifecycle, concurrency, and CLI details.

## Daily Engineering Task Planner

The deterministic daily planner decides what engineering task should be queued and
which registered repository should receive it. It gathers bounded local evidence,
accepts optional structured trend signals, ranks explainable candidates, and builds
a GN-HF-ready prompt. Planning remains separate from execution: the planner can
only enqueue through the existing Daily GN-HF Runner boundary and never launches
GN-HF itself.

```mermaid
flowchart TD
    Sources[Task and Trend Sources] --> Planner[Daily Engineering Planner]
    Planner --> Selector[Repository Selector]
    Selector --> Prompt[GN-HF Prompt Builder]
    Prompt --> Enqueue[daily-gnhf enqueue]
    Enqueue --> Runner[Daily GN-HF Runner]
    Runner --> Execution[First Mate / Captain / GN-HF]
```

```console
./scripts/daily-planner status
./scripts/daily-planner projects
./scripts/daily-planner candidates
./scripts/daily-planner plan
./scripts/daily-planner enqueue plan-2026-09-23
```

Plans use deterministic daily identities and automatic enqueue is disabled by
default. See [Daily Engineering Task Planner](docs/daily-planner.md) for registry,
evidence, ranking, duplicate protection, repository selection, and scheduling
details.

## Autonomous Daily GN-HF Runner

The durable local daily runner queues dated engineering tasks and safely launches
the next eligible task through the existing First Mate and GN-HF interface. It
provides deterministic daily IDs, concurrent claim protection, conservative
missed-task catch-up, retained history, explicit retries, and stale-running
visibility. Automatic push and merge are disabled by default.

```mermaid
flowchart LR
    Source[Task Source] --> Inbox[Task Inbox]
    Inbox --> Runner[Daily Runner]
    Runner --> GNHF[GN-HF / First Mate]
    GNHF --> Worktree[Worktree]
    Worktree --> Agent[Agent]
    Agent --> Quality[Tests / Evaluation]
    Quality --> Completed[Completed]
    Quality --> Failed[Failed]
```

```console
./scripts/daily-gnhf enqueue task.md --date 2026-09-22
./scripts/daily-gnhf status
./scripts/daily-gnhf run
./scripts/daily-gnhf catch-up
./scripts/daily-gnhf history
```

See [Autonomous Daily GN-HF Runner](docs/daily-gnhf.md) for the task format,
lifecycle, safety and recovery model, scheduling setup, and WSL2 limitations.
