# Captain Environment Architecture

## Purpose

This repository will provide a Linux-native control plane for delegating coding
tasks to multiple agents without sharing mutable checkouts. The captain owns
planning, assignment, review, verification, and integration. Workers own only
their assigned branch and leased worktree.

The environment wraps the tools already installed on the host; it does not
vendor, reinstall, or replace gnhf, Codex, Claude Code, OpenCode, tmux,
Treehouse, or Git.

## Current baseline

The initial inspection found:

| Component | Installed version | Repository state |
| --- | --- | --- |
| gnhf | 0.1.45 | Active run state is stored under `.gnhf/runs/` and ignored locally. |
| Codex | 0.149.0 | Available on `PATH`; no project session integration exists. |
| Claude Code | 2.1.241 | Available on `PATH`; no project session integration exists. |
| OpenCode | 1.18.21 | Available on `PATH`; no project session integration exists. |
| tmux | 3.4 | Available; no tmux server is currently running. |
| Treehouse | 2.3.0 | Configured with an empty root, so its existing pool is outside the project under `~/.treehouse`. |
| Git | 2.43.0 | The repository already has several worktrees; they are pre-existing state and must not be altered implicitly. |
| AXI skill | project-local | Installed at `.agents/skills/axi/SKILL.md`. |

There is no existing orchestrator script, test harness, agent-role contract, or
integration workflow to preserve. The committed repository is otherwise a
minimal test fixture.

## System boundaries

The project-local control plane will be a small `captain` CLI and declarative
configuration. It coordinates existing tools but does not duplicate their job:

- **gnhf** drives autonomous, iterative objectives when a task benefits from
  repeated bounded attempts.
- **Codex, Claude Code, and OpenCode** are selectable worker backends. A task
  records its backend explicitly; the captain workflow is backend-neutral.
- **Treehouse** allocates and durably leases isolated worktrees.
- **Git** supplies branch isolation, reviewable commits, and integration.
- **tmux** keeps one persistent worker session per task and makes logs and
  reattachment predictable.
- **The captain CLI** validates prerequisites, creates task metadata, composes
  non-interactive launch commands, reports live state, and performs guarded
  cleanup. It does not make product-code decisions for workers.

Project configuration and reusable role prompts are committed. Runtime state,
worktrees, logs, sockets, and transcripts remain ignored and project-local.

## Roles and authority

### Captain

The captain is the only role allowed to integrate into the captain branch. It:

1. decomposes the objective into non-overlapping, verifiable tasks;
2. records scope, owned paths, dependencies, backend, and acceptance checks;
3. assigns one branch, Treehouse lease, worktree, and tmux session per task;
4. reviews the worker's diff and handoff evidence;
5. runs repository checks from the integration checkout;
6. integrates accepted commits and releases task resources.

The captain never edits a worker checkout concurrently with its worker and
never integrates a dirty worktree.

### Worker

A worker operates only inside its leased worktree and assigned scope. It must
inspect repository guidance, preserve unrelated changes, run the declared
checks, commit coherent changes, and return a structured handoff containing the
commit, checks, changed paths, risks, and unresolved issues. A worker must not
merge, rebase shared branches, modify another task's worktree, or release its
own lease.

### Reviewer

A reviewer is a fresh agent context used for higher-risk changes. It receives
the task contract and commit range, works read-only unless the captain creates a
separate remediation task, and reports findings by severity with file and line
references. The author does not review its own work.

## Task and resource model

Each task has an immutable task ID and a runtime record with these minimum
fields:

| Field | Meaning |
| --- | --- |
| `id` | Filesystem- and tmux-safe identifier. |
| `title` | Short human-readable objective. |
| `status` | `planned`, `leased`, `running`, `review`, `accepted`, `integrated`, `failed`, or `released`. |
| `backend` | `codex`, `claude`, `opencode`, or `gnhf:<backend>`. |
| `base_commit` | Exact commit from which the worker started. |
| `branch` | Dedicated `agent/<task-id>` branch. |
| `worktree` | Absolute leased worktree path. |
| `lease_id` | Treehouse lease identity used for guarded return. |
| `session` | Namespaced tmux session name. |
| `owned_paths` | Paths the worker may change without renewed coordination. |
| `checks` | Non-interactive acceptance commands. |

The state transition is monotonic:

```text
planned -> leased -> running -> review -> accepted -> integrated -> released
                         |          |          |
                         +--------> failed <---+
```

`failed` preserves the worktree and evidence for diagnosis. Cleanup is a
separate, explicit transition so failure cannot silently destroy recoverable
work.

## Runtime layout

The intended project-local layout is:

```text
.captain/
  config.toml              # committed defaults and check definitions
  roles/                   # committed captain, worker, and reviewer contracts
  runtime/                 # ignored task records, locks, logs, and handoffs
.treehouse/                 # ignored Treehouse pool and leased worktrees
bin/captain                 # agent-facing entry point
tests/                      # CLI contract and lifecycle tests
```

Treehouse will be changed to `root = "."` only alongside ignore rules and a
migration-safe validation check. Existing worktrees outside this layout are
never adopted, reset, pruned, or returned by the captain.

tmux sessions use a repository-derived namespace plus the task ID, preventing
collisions between repositories. Commands target sessions by exact name; broad
tmux server shutdown is forbidden.

## Delegation lifecycle

### 1. Plan

The captain verifies a clean integration checkout, resolves the base commit,
checks task-ID uniqueness, and rejects overlapping owned paths among active
tasks. The task contract includes a concrete objective and executable checks.

### 2. Lease and prepare

The captain acquires a durable Treehouse lease using `treehouse get --lease
--json`, records the lease identity before mutation, creates the task branch at
the recorded base commit, and verifies that the resulting checkout is clean.
Partial setup is rolled back only for resources created by that invocation.

### 3. Launch and observe

The captain starts the selected backend in a detached, exactly named tmux
session with the worktree as its working directory. Launches are
non-interactive and log to the task runtime directory. Re-running launch for an
already-running task is an idempotent no-op.

Status is derived from task metadata, Treehouse lease state, tmux session
existence, Git branch/HEAD, cleanliness, and handoff presence. Metadata alone
is never treated as proof that a worker is alive or complete.

### 4. Handoff and review

The worker writes a structured handoff and exits. The captain verifies that the
reported commit is reachable from the task branch, the diff is confined to
owned paths, the worktree is clean, and all acceptance checks pass. Risky work
then receives an independent reviewer. Failed review produces a new bounded
remediation assignment rather than untracked edits in the worker checkout.

### 5. Integrate

The captain updates its integration branch, checks that the task base has not
invalidated the result, and integrates one accepted commit range at a time.
Repository-wide checks run after each integration. On conflict or check
failure, integration stops with the task resources intact; no automatic force,
reset, or conflict resolution is allowed.

### 6. Release

Only an integrated task, or an explicitly abandoned task, may be released.
The captain terminates the exact task tmux session, returns the worktree with
Treehouse's lease-identity guard, and retains the handoff and summary log.
Destructive cleanup requires an explicit force flag and reports what will be
lost before acting.

## Git coordination rules

- One active task owns one branch and one worktree.
- Active tasks must have disjoint owned paths unless their dependency ordering
  makes them sequential.
- Workers commit; the captain integrates. Workers never push or merge by
  default.
- The recorded base commit and accepted head commit are immutable review
  inputs.
- Integration uses non-interactive Git commands and refuses dirty checkouts,
  detached integration heads, unexpected branch movement, and unmerged paths.
- No workflow command uses `reset --hard`, recursive deletion, force push, or
  unguarded `treehouse return --force` as a normal recovery mechanism.
- Existing branches, worktrees, tmux sessions, and Treehouse leases not present
  in captain runtime state are out of scope.

## Agent-facing interface contract

The CLI will follow the project-local AXI skill:

- no arguments shows a compact live dashboard rather than global help;
- stdout uses TOON for data, confirmations, and actionable errors;
- stderr is reserved for progress and diagnostics;
- exit codes are `0` for success/no-op, `1` for operational failure, and `2`
  for usage errors;
- unknown flags fail before dependencies run, and every operation is fully
  non-interactive;
- `-v`, `-V`, and `--version` are bare, fast-path version probes;
- list views expose minimal fields and total counts, while detail views provide
  bounded previews and a `--full` escape hatch;
- mutations are idempotent when the requested state already exists.

Internal state remains ordinary structured data and converts to TOON only at
the output boundary. TOON conformance will be covered by golden output and
strict decoding tests rather than handwritten output assumptions.

## Verification strategy

Tests will use temporary Git repositories, a dedicated tmux socket, and fake
agent executables. They must not depend on user-global sessions or worktrees.
The verification layers are:

1. **Static checks:** shell formatting/linting, configuration parsing, and role
   contract validation.
2. **CLI contract tests:** output schemas, exit codes, unknown flags, empty
   states, idempotency, and version fast path.
3. **Lifecycle integration tests:** lease, branch preparation, launch, status,
   handoff validation, guarded integration, and release.
4. **Failure tests:** dirty trees, stale bases, overlapping paths, missing
   tools, dead sessions, malformed handoffs, conflicts, and failed checks.
5. **Smoke test:** one disposable task runs through the complete lifecycle with
   a fake worker; real-agent smoke tests remain explicit because they consume
   credentials and external quotas.

## Incremental implementation order

Each stage is independently testable and should land only after its checks
pass:

1. Commit project-local runtime/ignore conventions and switch Treehouse to the
   project-local root without touching existing external worktrees.
2. Add the CLI skeleton, configuration loader, TOON output boundary, AXI help,
   errors, empty dashboard, and version fast path.
3. Add a read-only `doctor` command that verifies installed tools, repository
   state, Treehouse configuration, and tmux availability.
4. Add task contracts and task creation with ID, path-overlap, base-commit, and
   check validation.
5. Add durable lease/prepare and guarded release operations.
6. Add backend launch adapters and exact tmux-session management.
7. Add live status reconciliation and structured worker handoffs.
8. Add review gates and safe, one-task-at-a-time integration.
9. Add opt-in session integrations and generate an installable skill from the
   same static guidance as the CLI home view.
10. Run the disposable end-to-end smoke test and document the captain runbook.

## Definition of done

The environment is complete when all three worker backends and gnhf can be
launched through the same task contract; parallel workers are isolated in
durably leased worktrees and exact tmux sessions; status survives captain
process restarts; handoffs, reviews, checks, and integration are enforced; all
failure paths preserve unrelated state; automated tests pass; a full disposable
smoke task completes; no background process from verification remains; and the
integration checkout is clean.
