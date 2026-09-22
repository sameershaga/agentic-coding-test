# Shared Agent Memory and Context Broker

Shared agent memory gives workers a small, durable set of validated repository facts before they start. It is not a transcript, a chatbot, or a replacement for inspecting the repository. Capture is explicit so an agent's unsupported output does not become project truth.

The system has four distinct kinds of information:

| Kind | Lifetime | Purpose |
| --- | --- | --- |
| MODEL CONTEXT | One worker run | Relevant facts supplied to a model for its current task |
| PROJECT MEMORY | Durable | Validated repository knowledge worth reusing across runs |
| TELEMETRY | Durable run record | What happened during a run, without prompts or conversations |
| EVALUATION | Per result | Whether a change met its acceptance and quality criteria |

## Architecture

`scripts/agent-memory` explicitly writes and queries structured JSON records in `.agent-memory/records/`. `scripts/agent-context` ranks active records for a task and creates a compact PROJECT CONTEXT packet. First Mate invokes the broker at the Task to Worker boundary and prepends the same packet to every provider attempt. If the broker is absent or fails, First Mate preserves its existing behavior and sends the original task.

Linked Git worktrees resolve the primary checkout through Git's common directory, so workers share one store. The implementation uses only the Python standard library and requires no model, embeddings, vector database, network API, or external service.

## Memory records

Every record contains an `id`, `type`, `scope`, `tags`, `summary`, `status`, `confidence`, `created_at`, a deterministic fingerprint, and a `provenance` object. Provenance can contain `source_run`, `source_commit`, and `branch`.

Supported types are:

- `DECISION`: a validated design or policy choice
- `DISCOVERY`: a verified repository fact
- `FAILURE`: a known failed approach or recurring failure mode
- `CONVENTION`: an established repository practice
- `COMMAND`: a verified operational or validation command
- `ARCHITECTURE`: a stable structural fact or component relationship

Example:

```json
{
  "id": "mem-65c8c1f44a77bf4d",
  "type": "COMMAND",
  "scope": "repository",
  "tags": ["testing"],
  "summary": "Run repository tests with python3 -m unittest discover",
  "status": "active",
  "confidence": 1.0,
  "provenance": {
    "source_run": "run-42",
    "source_commit": "abc123",
    "branch": "feature/memory"
  },
  "created_at": "2026-09-21T12:00:00Z",
  "fingerprint": "65c8c1f44a77bf4d000000000000000000000000000000000000000000000000"
}
```

## Command line use

Capture only intentional, validated knowledge:

```console
./scripts/agent-memory add \
  --type COMMAND \
  --tags testing \
  --summary "Run repository tests with python3 -m unittest discover" \
  --run-id run-42 \
  --commit abc123 \
  --branch feature/memory
```

List, search, and inspect aggregate state:

```console
./scripts/agent-memory list
./scripts/agent-memory search testing
./scripts/agent-memory stats
./scripts/agent-memory --json search testing
```

Generate bounded context for a task:

```console
./scripts/agent-context --task "improve agent evaluation tests" --budget 1000
./scripts/agent-context --json --task "improve agent evaluation tests" --budget 1000
```

The broker prints its approximate token count and configured budget. First Mate uses a default budget of 1200 tokens. Set `AGENT_CONTEXT_BUDGET` to change it.

## Relevance and determinism

Search tokenizes text case-insensitively, treats underscores as term separators so repository identifiers match natural-language tasks, and considers only records with active lifecycle status unless inactive records are explicitly requested. Text is normalized to Unicode NFC before fingerprinting and retrieval so canonically equivalent input cannot create duplicate memories. A matching summary term is worth 4 points, a tag term 8 points, a scope term 2 points, and a type term 1 point. Results sort by score, then confidence, then newest `created_at`, then stable memory ID. Timestamps are normalized to timezone-aware UTC on capture. This makes ranking explainable and reproducible for identical input and repository state.

The context broker considers ranked records in order and includes each complete record line only when the final packet remains within budget. It never calls a model. Repeating the same request against unchanged memory produces the same packet.

## Token budgeting

The estimator is `ceil(UTF-8 byte length / 4)`. This is a deterministic approximation, not a provider tokenizer, so the displayed value can differ from a model's billable token count. The packet, including its heading and accounting footer, never exceeds the configured estimate. Very small budgets receive a deterministic abbreviated packet.

## Deduplication and lifecycle

The store normalizes type, scope, tags, and summary, then hashes their canonical representation. Equivalent normalized content produces the same memory ID and is rejected as a duplicate.

Records are never silently deleted. An active record can transition to `invalid`, or it can be linked to a newer active record as `superseded`:

```console
./scripts/agent-memory invalidate mem-0123456789abcdef
./scripts/agent-memory supersede mem-0123456789abcdef mem-fedcba9876543210
```

Invalid and superseded records remain available for audit through `list` and `search --include-inactive`, but they are excluded from normal search and context packets. A superseded record must reference another valid record in the store. Dangling or cyclic supersession links are reported as malformed, while links to replacements that were later invalidated remain valid historical evidence. JSON records with duplicate member names are also malformed because their meaning depends on parser behavior.

## Concurrency and malformed data

Writers use a Linux advisory file lock. The lock must be owned by the current effective user, be a regular file with one hard link, and be opened without following symbolic links. Its permissions are repaired to owner-only before use. Each record is written to a private temporary file, flushed, and atomically renamed into place while the lock is held. File and directory metadata are synchronized before the operation completes. This prevents concurrent workers from publishing partial records. Read and write operations reject memory directories owned by another user. Read operations also reject symbolic links, non-directories, and group- or world-accessible permissions at the memory and records directory boundaries. A completely absent store is treated as empty, but an existing store missing its required `records` directory is rejected as unsafe. Enumeration, record opens, and atomic publication remain anchored to verified directory descriptors, so concurrent path replacement cannot redirect a read or write. The store also rejects symbolic links and unsafe record links. Malformed records, including excessively nested JSON and JSON files with noncanonical memory filenames, are counted and skipped so one damaged file does not make retrieval unavailable.

## Security

Memory must never contain conversations, `.env` contents, private keys, passwords, access tokens, or API keys. Add operations reject likely credential material before writing it. Text must be valid UTF-8. Non-whitespace control and Unicode formatting characters are also rejected so records cannot break output encoding, spoof terminal output, or hide text direction. Error messages identify the affected field but never echo its value. Stored records are scanned again during reads, and their deterministic fingerprint and ID are recomputed from the content. Stored text and tags must retain the canonical normalization applied during creation. Only files owned by the current effective user that are regular, have one hard link, and have no group or world permissions are accepted as records. Manually inserted secret-like, malformed, unsafe, permissively exposed, or inconsistently modified content is excluded from search and context.

The scanner is intentionally conservative and covers common PEM and OpenPGP private key headers, GitHub personal, OAuth, user, server, and refresh tokens, GitLab, Slack, npm, Hugging Face, PyPI, legacy and project-scoped OpenAI-style keys, Anthropic-style keys, Google API keys and OAuth access tokens, Stripe secret and restricted key formats, long-lived and temporary AWS access key IDs, Bearer, Basic, Digest, API key, and token authorization credentials, assignments labeled as passwords, credentials, secrets, or tokens regardless of value length, credential-bearing URLs including short passwords, signed URLs with common cloud signature parameters, and pasted multi-line dotenv content. A single benign environment assignment in a command memory remains supported. It is defense in depth, not a general secret-management system. Keep credentials outside project memory and use the repository's existing MCP Firewall and checkpoint safety controls for their respective boundaries.

## Limitations

- Relevance is lexical. Synonyms without shared terms do not match.
- Token counts are approximations and may differ by provider.
- Recency only breaks ties after keyword relevance and confidence. It does not allow a newer weak match to displace a stronger repository-specific match.
- Secret detection is pattern based and cannot recognize every possible credential format.
- Memory quality depends on deliberate human or agent validation before capture.
- The store is designed for cooperating Linux processes on a local filesystem, not distributed hosts.
