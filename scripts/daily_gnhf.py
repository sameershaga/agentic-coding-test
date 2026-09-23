#!/usr/bin/env python3
"""Durable local inbox primitives for the daily GN-HF runner."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


TASK_ID = re.compile(r"^daily-(\d{4}-\d{2}-\d{2})$")
STATES = ("pending", "running", "completed", "failed")
CAPTAIN_STATUSES = {
    "leasing",
    "starting-gnhf",
    "opencode-primary",
    "gnhf-codex-account2",
    "gnhf-codex-account1",
    "review",
    "accepted",
    "integrated",
    "released",
    "failed",
}


class TaskError(ValueError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def task_root(repo: Path) -> Path:
    return Path(os.environ.get("DAILY_GNHF_TASK_ROOT", repo / ".agent-tasks"))


def initialize(root: Path) -> None:
    if root.is_symlink():
        raise TaskError(f"task root must not be a symlink: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise TaskError(f"task root is not a directory: {root}")
    os.chmod(root, 0o700)
    for name in (*STATES, "prompts"):
        directory = root / name
        if directory.is_symlink():
            raise TaskError(f"task directory must not be a symlink: {directory}")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir():
            raise TaskError(f"task path is not a directory: {directory}")
        os.chmod(directory, 0o700)


def daily_id(day: dt.date) -> str:
    return f"daily-{day.isoformat()}"


def parse_day(value: str) -> dt.date:
    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise TaskError(f"invalid task date: {value}") from exc
    if value != day.isoformat():
        raise TaskError(f"invalid task date: {value}")
    return day


def atomic_create(path: Path, content: bytes, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.close(descriptor)
        descriptor = -1
        # A hard link publishes the fully synced file while retaining O_EXCL-like
        # behavior: an existing destination makes the operation fail unchanged.
        os.link(temporary, path)
        temporary.unlink()
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            durable_unlink(temporary)


def atomic_replace(path: Path, content: bytes, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            durable_unlink(temporary)


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes across a host crash."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_move(source: Path, destination: Path) -> None:
    """Atomically move a record and durably publish both directory changes."""
    os.replace(source, destination)
    fsync_directory(destination.parent)
    if source.parent != destination.parent:
        fsync_directory(source.parent)


def durable_unlink(path: Path) -> None:
    """Remove a file and durably publish its directory-entry deletion."""
    path.unlink()
    fsync_directory(path.parent)


@contextmanager
def queue_lock(root: Path):
    """Serialize queue mutations across manual and scheduled runner processes."""
    initialize(root)
    lock_path = root / ".runner.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TaskError(f"cannot safely open queue lock: {lock_path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TaskError(f"queue lock must be a regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def enqueue(root: Path, prompt_source: Path, day: dt.date, title: str | None) -> dict:
    task_id = daily_id(day)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(prompt_source, flags)
    except OSError as exc:
        raise TaskError(f"prompt is not a safe regular file: {prompt_source}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TaskError(f"prompt is not a safe regular file: {prompt_source}")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            prompt = source.read()
    except OSError as exc:
        raise TaskError(f"prompt is not readable: {prompt_source}") from exc
    finally:
        os.close(descriptor)
    if not prompt:
        raise TaskError("prompt must not be empty")
    try:
        prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskError("prompt must be valid UTF-8") from exc
    with queue_lock(root):
        destination = root / "pending" / f"{task_id}.json"
        if any((root / state / f"{task_id}.json").exists() for state in STATES):
            raise TaskError(f"task already exists: {task_id}")

        prompt_name = f"{task_id}.md"
        prompt_path = root / "prompts" / prompt_name
        if prompt_path.exists():
            raise TaskError(f"task prompt already exists: {task_id}")
        atomic_create(prompt_path, prompt)
        record = {
            "task_id": task_id,
            "date": day.isoformat(),
            "title": title or prompt_source.stem,
            "prompt_file": f"prompts/{prompt_name}",
            "status": "PENDING",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "attempts": 0,
            "run_id": None,
            "branch": None,
            "commit": None,
            "worktree": None,
            "exit_code": None,
            "evaluation": None,
        }
        try:
            atomic_create(destination, (json.dumps(record, indent=2) + "\n").encode())
        except Exception:
            durable_unlink(prompt_path)
            raise
        return record


def validate_record(record: object, state: str) -> dict:
    if not isinstance(record, dict):
        raise TaskError("task metadata must be a JSON object")
    task_id = record.get("task_id")
    match = TASK_ID.fullmatch(task_id) if isinstance(task_id, str) else None
    if not match or record.get("date") != match.group(1):
        raise TaskError("task_id and date must identify the same daily task")
    parse_day(record["date"])
    if record.get("status") != state.upper():
        raise TaskError("metadata status does not match its state directory")
    return record


def validate_record_file(record: object, state: str, path: Path) -> dict:
    """Validate metadata and bind its identity to the durable record filename."""
    validated = validate_record(record, state)
    if path.name != f"{validated['task_id']}.json":
        raise TaskError("task_id does not match metadata filename")
    return validated


def load_json_file(path: Path) -> object:
    """Read JSON without following substituted filesystem objects."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TaskError(f"cannot safely open task metadata: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TaskError(f"task metadata must be a regular file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
            return json.load(source)
    except UnicodeError as exc:
        raise TaskError(f"task metadata must be valid UTF-8: {path}") from exc
    finally:
        os.close(descriptor)


def read_safe_text(path: Path) -> str:
    """Read UTF-8 from a regular file without following substituted objects."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"not a regular file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def read_captain_text(repo: Path, run_id: str, name: str) -> str:
    """Read Captain metadata only through real runtime directories."""
    captain = repo / ".captain"
    runtime = captain / "runtime"
    run = runtime / run_id
    for directory in (captain, runtime, run):
        if directory.is_symlink() or not directory.is_dir():
            raise OSError(f"unsafe Captain runtime directory: {directory}")
    return read_safe_text(run / name)


def load_record_file(path: Path, state: str) -> dict:
    """Safely read and validate a durable queue record."""
    return validate_record_file(load_json_file(path), state, path)


def validate_prompt(root: Path, record: dict) -> str:
    """Safely read a task prompt without following substituted objects."""
    relative = record.get("prompt_file")
    if not isinstance(relative, str) or not relative:
        raise TaskError("task prompt_file must be a non-empty string")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or len(relative_path.parts) != 2
        or relative_path.parts[0] != "prompts"
    ):
        raise TaskError("task prompt_file must name a file directly inside prompts")
    prompt = root / relative_path
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(prompt, flags)
    except OSError as exc:
        raise TaskError(f"task prompt is missing or unsafe: {relative}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TaskError(f"task prompt is missing or unsafe: {relative}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
            contents = source.read()
    except (OSError, UnicodeError) as exc:
        raise TaskError(f"task prompt is not readable UTF-8: {relative}") from exc
    finally:
        os.close(descriptor)
    if not contents:
        raise TaskError(f"task prompt is empty: {relative}")
    return contents


def claim_next(
    root: Path,
    today: dt.date,
    max_attempts: int,
    *,
    missed_only: bool = False,
) -> dict | None:
    """Atomically claim the oldest eligible pending task under the queue lock."""
    if max_attempts < 1:
        raise TaskError("max_attempts must be positive")
    with queue_lock(root):
        state_files: dict[str, list[str]] = {}
        for state in STATES:
            for path in (root / state).glob("*.json"):
                state_files.setdefault(path.name, []).append(state)
        duplicates = {
            name: states for name, states in state_files.items() if len(states) > 1
        }
        if duplicates:
            name = min(duplicates)
            states = ", ".join(sorted(duplicates[name]))
            raise TaskError(f"task exists in multiple states: {name} ({states})")

        # A process can stop after durably preparing the source record but before
        # returning the claimed record to the launcher. Such a record was never
        # launchable, so restore it before selecting work instead of blocking the
        # sequential queue. Move first so another interruption remains recoverable.
        for path in sorted((root / "running").glob("*.json")):
            try:
                prepared = load_json_file(path)
            except (OSError, json.JSONDecodeError) as exc:
                raise TaskError(f"cannot claim malformed task {path.name}: {exc}") from exc
            if isinstance(prepared, dict) and prepared.get("claim_prepared") is True:
                validate_record_file(prepared, "running", path)
                destination = root / "pending" / path.name
                if destination.exists():
                    raise TaskError(f"pending task already exists: {prepared['task_id']}")
                durable_move(path, destination)
        for path in sorted((root / "pending").glob("*.json")):
            try:
                prepared = load_json_file(path)
            except (OSError, json.JSONDecodeError) as exc:
                raise TaskError(f"cannot claim malformed task {path.name}: {exc}") from exc
            if isinstance(prepared, dict) and prepared.get("claim_prepared") is True:
                validate_record_file(prepared, "running", path)
                attempts = prepared.get("attempts")
                if (
                    not isinstance(attempts, int)
                    or isinstance(attempts, bool)
                    or attempts < 1
                ):
                    raise TaskError(f"invalid prepared claim attempts: {path.name}")
                prepared.update(
                    status="PENDING",
                    started_at=None,
                    attempts=attempts - 1,
                )
                prepared.pop("claim_prepared")
                atomic_replace(path, (json.dumps(prepared, indent=2) + "\n").encode())
        if any((root / "running").glob("*.json")):
            return None
        candidates = []
        for path in sorted((root / "pending").glob("*.json")):
            try:
                record = load_record_file(path, "pending")
            except (OSError, json.JSONDecodeError, TaskError) as exc:
                raise TaskError(f"cannot claim malformed task {path.name}: {exc}") from exc
            task_day = parse_day(record["date"])
            if task_day < today or (task_day == today and not missed_only):
                candidates.append((record["date"], record["task_id"], path, record))
        if not candidates:
            return None

        _, task_id, source, record = min(candidates)
        validate_prompt(root, record)
        attempts = record.get("attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise TaskError(f"invalid attempts for task: {task_id}")
        if attempts >= max_attempts:
            raise TaskError(f"task has reached max attempts: {task_id}")

        destination = root / "running" / source.name
        if destination.exists():
            raise TaskError(f"running task already exists: {task_id}")
        record.update(
            status="RUNNING",
            started_at=utc_now(),
            finished_at=None,
            attempts=attempts + 1,
            claim_prepared=True,
        )
        atomic_replace(source, (json.dumps(record, indent=2) + "\n").encode())
        durable_move(source, destination)
        record.pop("claim_prepared")
        atomic_replace(destination, (json.dumps(record, indent=2) + "\n").encode())
        return record


def update_running(root: Path, task_id: str, updates: dict) -> dict:
    """Update a claimed record while verifying it is still RUNNING."""
    with queue_lock(root):
        states = [
            state
            for state in STATES
            if (root / state / f"{task_id}.json").exists()
        ]
        if len(states) > 1:
            raise TaskError(
                f"task exists in multiple states: {task_id}.json "
                f"({', '.join(sorted(states))})"
            )
        path = root / "running" / f"{task_id}.json"
        try:
            record = load_record_file(path, "running")
        except FileNotFoundError as exc:
            raise TaskError(f"running task disappeared: {task_id}") from exc
        except (OSError, json.JSONDecodeError, TaskError) as exc:
            raise TaskError(f"cannot update running task {task_id}: {exc}") from exc
        record.update(updates)
        atomic_replace(path, (json.dumps(record, indent=2) + "\n").encode())
        return record


def fail_launch(root: Path, task_id: str, exit_code: int) -> dict:
    """Atomically preserve a task whose First Mate launcher failed."""
    with queue_lock(root):
        states = [
            state
            for state in STATES
            if (root / state / f"{task_id}.json").exists()
        ]
        if len(states) > 1:
            raise TaskError(
                f"task exists in multiple states: {task_id}.json "
                f"({', '.join(sorted(states))})"
            )
        source = root / "running" / f"{task_id}.json"
        try:
            record = load_record_file(source, "running")
        except (OSError, json.JSONDecodeError, TaskError) as exc:
            raise TaskError(f"cannot fail running task {task_id}: {exc}") from exc
        destination = root / "failed" / source.name
        if destination.exists():
            raise TaskError(f"failed task already exists: {task_id}")
        record.update(status="FAILED", finished_at=utc_now(), exit_code=exit_code)
        atomic_replace(source, (json.dumps(record, indent=2) + "\n").encode())
        durable_move(source, destination)
        return record


def retry_failed(root: Path, task_id: str, max_attempts: int) -> dict:
    """Atomically return an eligible failed task to PENDING."""
    if not TASK_ID.fullmatch(task_id):
        raise TaskError(f"invalid task id: {task_id}")
    if max_attempts < 1:
        raise TaskError("max_attempts must be positive")
    with queue_lock(root):
        states = [
            state
            for state in STATES
            if (root / state / f"{task_id}.json").exists()
        ]
        if len(states) > 1:
            raise TaskError(
                f"task exists in multiple states: {task_id}.json "
                f"({', '.join(sorted(states))})"
            )
        source = root / "failed" / f"{task_id}.json"
        try:
            record = load_record_file(source, "failed")
        except FileNotFoundError as exc:
            raise TaskError(f"failed task does not exist: {task_id}") from exc
        except (OSError, json.JSONDecodeError, TaskError) as exc:
            raise TaskError(f"cannot retry failed task {task_id}: {exc}") from exc
        validate_prompt(root, record)
        attempts = record.get("attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise TaskError(f"invalid attempts for task: {task_id}")
        if attempts >= max_attempts:
            raise TaskError(f"task has reached max attempts: {task_id}")
        destination = root / "pending" / source.name
        if destination.exists():
            raise TaskError(f"pending task already exists: {task_id}")
        failures = record.get("failures", [])
        if not isinstance(failures, list):
            raise TaskError(f"invalid failure history for task: {task_id}")
        failures.append(
            {
                "attempt": attempts,
                "finished_at": record.get("finished_at"),
                "exit_code": record.get("exit_code"),
                "run_id": record.get("run_id"),
                "branch": record.get("branch"),
                "commit": record.get("commit"),
                "worktree": record.get("worktree"),
                "evaluation": record.get("evaluation"),
                "captain_status": record.get("captain_status"),
                "launcher_exit_code": record.get("launcher_exit_code"),
            }
        )
        record.update(
            status="PENDING",
            started_at=None,
            finished_at=None,
            run_id=None,
            branch=None,
            commit=None,
            worktree=None,
            exit_code=None,
            evaluation=None,
            failures=failures,
        )
        record.pop("captain_status", None)
        record.pop("launcher_exit_code", None)
        atomic_replace(source, (json.dumps(record, indent=2) + "\n").encode())
        durable_move(source, destination)
        return record


def reconcile_running(root: Path, repo: Path) -> list[dict]:
    """Atomically resolve tasks whose linked Captain runs are terminal."""
    terminal = {
        "review": ("completed", 0),
        "accepted": ("completed", 0),
        "integrated": ("completed", 0),
        "released": ("completed", 0),
        "failed": ("failed", 1),
    }
    reconciled = []
    with queue_lock(root):
        state_files: dict[str, list[str]] = {}
        for state in STATES:
            for path in (root / state).glob("*.json"):
                state_files.setdefault(path.name, []).append(state)
        duplicates = {
            name: states for name, states in state_files.items() if len(states) > 1
        }
        if duplicates:
            name = min(duplicates)
            states = ", ".join(sorted(duplicates[name]))
            raise TaskError(f"task exists in multiple states: {name} ({states})")

        for source in sorted((root / "running").glob("*.json")):
            try:
                record = load_record_file(source, "running")
            except (OSError, json.JSONDecodeError, TaskError) as exc:
                raise TaskError(f"cannot reconcile running task {source.name}: {exc}") from exc
            run_id = record.get("run_id")
            if not isinstance(run_id, str) or not re.fullmatch(
                r"fm-[A-Za-z0-9_-]+", run_id
            ):
                continue
            try:
                captain_status = read_captain_text(repo, run_id, "status").strip()
            except (OSError, UnicodeError):
                continue
            outcome = terminal.get(captain_status)
            if outcome is None:
                continue
            state, exit_code = outcome
            destination = root / state / source.name
            if destination.exists():
                raise TaskError(f"{state} task already exists: {record['task_id']}")
            commit = None
            for name in ("integrated-commit", "accepted-commit"):
                try:
                    value = read_captain_text(repo, run_id, name).strip()
                except (OSError, UnicodeError):
                    continue
                if re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
                    commit = value
                    break
            record.update(
                status=state.upper(),
                finished_at=utc_now(),
                exit_code=exit_code,
                captain_status=captain_status,
                commit=commit or record.get("commit"),
            )
            atomic_replace(source, (json.dumps(record, indent=2) + "\n").encode())
            durable_move(source, destination)
            reconciled.append(record)
    return reconciled


def launch_next(
    root: Path,
    today: dt.date,
    max_attempts: int,
    repo: Path,
    first_mate: Path,
    acceptance_check: str,
    *,
    missed_only: bool = False,
) -> dict | None:
    """Claim one task and launch it through First Mate's existing GN-HF API."""
    reconcile_running(root, repo)
    record = claim_next(root, today, max_attempts, missed_only=missed_only)
    if record is None:
        return None
    task_id = record["task_id"]
    try:
        prompt = validate_prompt(root, record)
    except TaskError:
        # The prompt may be removed or substituted after claim-time validation.
        # Preserve the consumed attempt as FAILED instead of stranding RUNNING.
        return fail_launch(root, task_id, 1)
    descriptor, run_id_name = tempfile.mkstemp(prefix=".run-id.", dir=root)
    os.close(descriptor)
    run_id_file = Path(run_id_name)
    try:
        completed = subprocess.run(
            [
                str(first_mate),
                "gnhf",
                "--repo",
                str(repo),
                "--check",
                acceptance_check,
                "--run-id-file",
                str(run_id_file),
                prompt,
            ],
            check=False,
        )
        if completed.returncode != 0:
            return fail_launch(root, task_id, completed.returncode)
        try:
            run_id = read_safe_text(run_id_file).strip()
        except (OSError, UnicodeError):
            return fail_launch(root, task_id, 1)
        if not re.fullmatch(r"fm-[A-Za-z0-9_-]+", run_id):
            return fail_launch(root, task_id, 1)
        metadata = {"run_id": run_id, "launcher_exit_code": 0}
        record = update_running(root, task_id, metadata)
        for field in ("branch", "worktree"):
            try:
                value = read_captain_text(repo, run_id, field).strip()
            except (OSError, UnicodeError):
                continue
            if value:
                record = update_running(root, task_id, {field: value})
        return record
    except OSError:
        return fail_launch(root, task_id, 127)
    finally:
        if run_id_file.exists():
            durable_unlink(run_id_file)


def catch_up(
    root: Path,
    today: dt.date,
    max_attempts: int,
    max_tasks: int,
    repo: Path,
    first_mate: Path,
    acceptance_check: str,
) -> list[dict]:
    """Launch missed tasks oldest-first, bounded by the configured limit."""
    if max_tasks < 1:
        raise TaskError("max_catch_up_tasks must be positive")
    launched = []
    for _ in range(max_tasks):
        record = launch_next(
            root,
            today,
            max_attempts,
            repo,
            first_mate,
            acceptance_check,
            missed_only=True,
        )
        if record is None:
            break
        launched.append(record)
        # First Mate runs asynchronously. Sequential mode intentionally leaves the
        # remaining missed tasks pending until a later invocation reconciles this run.
        if record["status"] == "RUNNING":
            break
    return launched


def load_config(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TaskError(f"cannot safely open configuration: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TaskError(f"configuration must be a regular file: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
            config = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskError(f"cannot load configuration: {exc}") from exc
    finally:
        os.close(descriptor)
    if not isinstance(config, dict):
        raise TaskError("configuration must be a JSON object")
    for field in ("enabled", "catch_up", "auto_push", "auto_merge"):
        if not isinstance(config.get(field), bool):
            raise TaskError(f"configuration {field} must be a boolean")
    if config.get("execution_mode") != "sequential":
        raise TaskError("configuration execution_mode must be sequential")
    if config["auto_push"]:
        raise TaskError("configuration auto_push is not supported")
    if config["auto_merge"]:
        raise TaskError("configuration auto_merge is not supported")
    if (
        not isinstance(config.get("max_attempts"), int)
        or isinstance(config["max_attempts"], bool)
        or config["max_attempts"] < 1
    ):
        raise TaskError("configuration max_attempts must be positive")
    if not isinstance(config.get("acceptance_check"), str) or not config["acceptance_check"]:
        raise TaskError("configuration acceptance_check must be a non-empty string")
    if (
        not isinstance(config.get("max_catch_up_tasks"), int)
        or isinstance(config["max_catch_up_tasks"], bool)
        or config["max_catch_up_tasks"] < 1
    ):
        raise TaskError("configuration max_catch_up_tasks must be positive")
    if config.get("catch_up_mode") != "oldest_first":
        raise TaskError("configuration catch_up_mode must be oldest_first")
    if (
        not isinstance(config.get("stale_after_hours"), int)
        or isinstance(config["stale_after_hours"], bool)
        or config["stale_after_hours"] < 1
    ):
        raise TaskError("configuration stale_after_hours must be positive")
    return config


def stale_running_reason(
    record: dict, repo: Path, now: dt.datetime, stale_after_hours: int
) -> str | None:
    """Identify old RUNNING records that have no observable Captain lifecycle."""
    started_at = record.get("started_at")
    if not isinstance(started_at, str):
        return "missing started_at"
    try:
        started = dt.datetime.fromisoformat(started_at)
    except ValueError:
        return "invalid started_at"
    if started.tzinfo is None:
        return "started_at has no timezone"
    if now - started.astimezone(dt.timezone.utc) <= dt.timedelta(
        hours=stale_after_hours
    ):
        return None
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"fm-[A-Za-z0-9_-]+", run_id):
        return "no linked Captain run"
    try:
        captain_status = read_captain_text(repo, run_id, "status").strip()
    except UnicodeError:
        return "linked Captain status is unreadable"
    except OSError:
        return "linked Captain status is missing or unsafe"
    if captain_status not in CAPTAIN_STATUSES:
        return "linked Captain status is unrecognized"
    return None


def status(
    root: Path,
    today: dt.date,
    repo: Path | None = None,
    *,
    now: dt.datetime | None = None,
    stale_after_hours: int = 24,
) -> dict:
    if stale_after_hours < 1:
        raise TaskError("stale_after_hours must be positive")
    repo = repo or Path(__file__).resolve().parents[1]
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise TaskError("status time must include a timezone")
    now = now.astimezone(dt.timezone.utc)
    with queue_lock(root):
        result: dict[str, object] = {state: [] for state in STATES}
        errors = []
        missed = []
        stale = []
        state_files: dict[str, list[str]] = {}
        for state in STATES:
            for path in (root / state).glob("*.json"):
                state_files.setdefault(path.name, []).append(state)
        duplicates = {
            name: states for name, states in state_files.items() if len(states) > 1
        }
        for name, states in sorted(duplicates.items()):
            errors.append(
                {
                    "file": name,
                    "error": "task exists in multiple states: "
                    + ", ".join(sorted(states)),
                }
            )
        for state in STATES:
            for path in sorted((root / state).glob("*.json")):
                if path.name in duplicates:
                    continue
                try:
                    record = load_record_file(path, state)
                    item = {"task_id": record["task_id"], "date": record["date"]}
                    result[state].append(item)  # type: ignore[union-attr]
                    if state == "pending" and parse_day(record["date"]) < today:
                        missed.append(item)
                    if state == "running":
                        reason = stale_running_reason(
                            record, repo, now, stale_after_hours
                        )
                        if reason:
                            stale.append({**item, "reason": reason})
                except (OSError, json.JSONDecodeError, TaskError) as exc:
                    errors.append({"file": str(path), "error": str(exc)})
        result["missed"] = missed
        result["stale_running"] = stale
        result["errors"] = errors
        return result


def show_task(root: Path, task_id: str) -> dict:
    """Return validated task metadata without reading or exposing prompt contents."""
    if not TASK_ID.fullmatch(task_id):
        raise TaskError(f"invalid task id: {task_id}")
    initialize(root)
    with queue_lock(root):
        matches = []
        for state in STATES:
            path = root / state / f"{task_id}.json"
            if path.exists():
                matches.append((state, path))
        if not matches:
            raise TaskError(f"task does not exist: {task_id}")
        if len(matches) != 1:
            raise TaskError(f"task exists in multiple states: {task_id}")
        state, path = matches[0]
        try:
            return load_record_file(path, state)
        except (OSError, json.JSONDecodeError, TaskError) as exc:
            raise TaskError(f"cannot show task {task_id}: {exc}") from exc


def task_history(root: Path, limit: int = 20) -> list[dict]:
    """Return recent terminal task metadata, newest completion first."""
    if isinstance(limit, bool) or limit < 1:
        raise TaskError("history limit must be positive")
    initialize(root)
    with queue_lock(root):
        state_files: dict[str, list[str]] = {}
        for state in STATES:
            for path in (root / state).glob("*.json"):
                state_files.setdefault(path.name, []).append(state)
        duplicates = {
            name: states for name, states in state_files.items() if len(states) > 1
        }
        if duplicates:
            name = min(duplicates)
            states = ", ".join(sorted(duplicates[name]))
            raise TaskError(f"task exists in multiple states: {name} ({states})")

        records = []
        for state in ("completed", "failed"):
            for path in (root / state).glob("*.json"):
                try:
                    record = load_record_file(path, state)
                except (OSError, json.JSONDecodeError, TaskError) as exc:
                    raise TaskError(
                        f"cannot read history task {path.name}: {exc}"
                    ) from exc
                records.append(record)
        def completion_key(record: dict) -> tuple[dt.datetime, str]:
            finished_at = record.get("finished_at")
            if isinstance(finished_at, str):
                try:
                    finished = dt.datetime.fromisoformat(finished_at)
                    if finished.tzinfo is not None:
                        return finished.astimezone(dt.timezone.utc), record["task_id"]
                except ValueError:
                    pass
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc), record["task_id"]

        records.sort(key=completion_key, reverse=True)
        return records[:limit]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="daily-gnhf")
    result.add_argument("--json", action="store_true", help="emit JSON")
    commands = result.add_subparsers(dest="command", required=True)
    enqueue_parser = commands.add_parser("enqueue")
    enqueue_parser.add_argument("prompt_file", type=Path)
    enqueue_parser.add_argument("--date", default=dt.date.today().isoformat())
    enqueue_parser.add_argument("--title")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--today", help=argparse.SUPPRESS)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--today", help=argparse.SUPPRESS)
    catch_up_parser = commands.add_parser("catch-up")
    catch_up_parser.add_argument("--today", help=argparse.SUPPRESS)
    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("task_id")
    history_parser = commands.add_parser("history")
    history_parser.add_argument("--limit", type=int, default=20)
    show_parser = commands.add_parser("show")
    show_parser.add_argument("task_id")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    root = task_root(repo)
    try:
        if args.command == "enqueue":
            output = enqueue(root, args.prompt_file, parse_day(args.date), args.title)
        elif args.command == "status":
            config = load_config(repo / "config" / "daily-gnhf.json")
            output = status(
                root,
                parse_day(args.today) if args.today else dt.date.today(),
                repo,
                stale_after_hours=config["stale_after_hours"],
            )
        elif args.command == "retry":
            config = load_config(repo / "config" / "daily-gnhf.json")
            output = retry_failed(root, args.task_id, config["max_attempts"])
        elif args.command == "history":
            output = task_history(root, args.limit)
        elif args.command == "show":
            output = show_task(root, args.task_id)
        else:
            config = load_config(repo / "config" / "daily-gnhf.json")
            if not config.get("enabled", False):
                raise TaskError("runner is disabled")
            today = parse_day(args.today) if args.today else dt.date.today()
            if args.command == "catch-up":
                if not config.get("catch_up", False):
                    raise TaskError("catch-up is disabled")
                output = catch_up(
                    root,
                    today,
                    config["max_attempts"],
                    config["max_catch_up_tasks"],
                    repo,
                    repo / "scripts" / "first-mate",
                    config["acceptance_check"],
                )
            else:
                output = launch_next(
                    root,
                    today,
                    config["max_attempts"],
                    repo,
                    repo / "scripts" / "first-mate",
                    config["acceptance_check"],
                )
    except (OSError, TaskError) as exc:
        print(f"daily-gnhf: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
