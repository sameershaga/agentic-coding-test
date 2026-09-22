"""Best-effort, privacy-conscious storage for agent run telemetry."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


TELEMETRY_FIELDS = (
    "run_id",
    "agent",
    "model",
    "provider",
    "task_id",
    "branch",
    "worktree",
    "started_at",
    "finished_at",
    "duration_seconds",
    "status",
    "tests_run",
    "tests_passed",
    "files_changed",
    "lines_added",
    "lines_deleted",
    "commits",
)

SUCCESS_STATUSES = frozenset(
    {"accepted", "completed", "review", "success", "succeeded"}
)
MAX_RECORD_BYTES = 1024 * 1024
MAX_DISPLAY_LABEL_LENGTH = 80


def _reject_non_finite_json(value: str) -> None:
    """Reject Python JSON extensions that are not valid standard JSON."""
    raise ValueError(f"non-finite JSON number: {value}")


def task_identifier(task: str) -> str:
    """Return a stable identifier without retaining prompt text."""
    return hashlib.sha256(task.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def duration_seconds(started_at: str, finished_at: str) -> float:
    """Calculate a non-negative duration from ISO-8601 timestamps."""
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    return max(0.0, (finished - started).total_seconds())


def _git_count(value: str) -> int | None:
    """Parse a non-negative Git count without allowing malformed output to escape."""
    if not value.isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        # Python bounds decimal conversion length to resist denial of service.
        return None


def telemetry_record(values: Mapping[str, Any]) -> dict[str, Any]:
    """Build a record containing only the documented telemetry fields."""
    record = {field: values.get(field) for field in TELEMETRY_FIELDS}
    if not isinstance(record["run_id"], str) or not record["run_id"]:
        raise ValueError("run_id must be a non-empty string")
    return record


def write_record(repo_root: os.PathLike[str] | str, values: Mapping[str, Any]) -> Path:
    """Create one immutable JSON file for a completed run."""
    record = telemetry_record(values)
    run_id = str(record["run_id"])
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise ValueError("run_id contains unsafe filename characters")
    serialized = (
        json.dumps(record, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    if len(serialized.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("telemetry record is too large")

    telemetry_dir = Path(repo_root) / ".agent-runs"
    telemetry_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(telemetry_dir, directory_flags)
    temporary_name = f".{run_id}.{secrets.token_hex(8)}.tmp"
    try:
        os.fchmod(directory_fd, 0o700)
        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        temporary_fd = os.open(temporary_name, temporary_flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as output:
                output.write(serialized)
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
            os.link(
                temporary_name,
                f"{run_id}.json",
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        finally:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return telemetry_dir / f"{run_id}.json"


def safe_write_record(
    repo_root: os.PathLike[str] | str, values: Mapping[str, Any]
) -> bool:
    """Store telemetry without allowing observability failures to escape."""
    try:
        write_record(repo_root, values)
    except (OSError, RecursionError, TypeError, ValueError):
        return False
    return True


def record_first_mate_run(
    control_root: os.PathLike[str] | str,
    runtime: os.PathLike[str] | str,
    worktree: os.PathLike[str] | str,
    started_at: str,
    agent: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    tests_run: bool = False,
    tests_passed: bool = False,
) -> bool:
    """Collect a completed First Mate run without exposing prompt contents."""
    runtime_path = Path(runtime)
    worktree_path = Path(worktree)

    def read_runtime_file(name: str) -> str | None:
        try:
            return (runtime_path / name).read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeError):
            return None

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree_path), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None
        # NUL-delimited output is already unambiguous and may contain filenames
        # whose leading or trailing whitespace must not be removed.
        return result.stdout if "-z" in args else result.stdout.strip()

    def untracked_numstat(path: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree_path),
                    "diff",
                    "--no-index",
                    "--numstat",
                    "-z",
                    "/dev/null",
                    "--",
                    path,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except (OSError, UnicodeError):
            return None
        if result.returncode not in (0, 1):
            return None
        return result.stdout.strip()

    def numstat_rows(output: str) -> list[list[str]]:
        """Parse NUL-delimited numstat without interpreting filename characters."""
        return [
            entry.split("\t", 2)
            for entry in output.split("\0")
            if entry.count("\t") >= 2
        ]

    run_id = runtime_path.name
    task = read_runtime_file("task.txt")
    base_commit = read_runtime_file("base-commit")
    status = read_runtime_file("status") or "unknown"
    finished_at = datetime.now().astimezone().isoformat()
    changed = git("diff", "--numstat", "-z", base_commit) if base_commit else None
    files_changed = lines_added = lines_deleted = None
    if changed is not None:
        rows = numstat_rows(changed)
        untracked = git("ls-files", "-z", "--others", "--exclude-standard")
        if untracked is not None:
            for path in untracked.split("\0"):
                if not path:
                    continue
                stat = untracked_numstat(path)
                if stat:
                    rows.extend(numstat_rows(stat))
        files_changed = len(rows)
        additions = (_git_count(row[0]) for row in rows)
        deletions = (_git_count(row[1]) for row in rows)
        lines_added = sum(value for value in additions if value is not None)
        lines_deleted = sum(value for value in deletions if value is not None)
    commit_output = git("rev-list", "--count", f"{base_commit}..HEAD") if base_commit else None

    try:
        duration = duration_seconds(started_at, finished_at)
    except (TypeError, ValueError):
        duration = None

    return safe_write_record(
        control_root,
        {
            "run_id": run_id,
            "agent": agent,
            "model": model,
            "provider": provider,
            "task_id": task_identifier(task) if task is not None else None,
            "branch": git("branch", "--show-current"),
            "worktree": str(worktree_path),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "status": status,
            "tests_run": tests_run,
            "tests_passed": tests_passed,
            "files_changed": files_changed,
            "lines_added": lines_added,
            "lines_deleted": lines_deleted,
            "commits": _git_count(commit_output) if commit_output else None,
        },
    )


def record_agent_run(
    control_root: os.PathLike[str] | str,
    worktree: os.PathLike[str] | str,
    run_id: str,
    task: str,
    started_at: str,
    status: str,
    agent: str,
    provider: str | None = None,
    model: str | None = None,
) -> bool:
    """Record a standalone agent-run invocation without retaining its prompt."""
    worktree_path = Path(worktree)

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree_path), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None
        return result.stdout.strip()

    finished_at = datetime.now().astimezone().isoformat()
    try:
        duration = duration_seconds(started_at, finished_at)
    except (TypeError, ValueError):
        duration = None

    return safe_write_record(
        control_root,
        {
            "run_id": run_id,
            "agent": agent,
            "model": model,
            "provider": provider,
            "task_id": task_identifier(task),
            "branch": git("branch", "--show-current"),
            "worktree": str(worktree_path),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "status": status,
            "tests_run": False,
            "tests_passed": False,
            "files_changed": None,
            "lines_added": None,
            "lines_deleted": None,
            "commits": None,
        },
    )


def read_records(repo_root: os.PathLike[str] | str) -> tuple[list[dict[str, Any]], int]:
    """Read valid telemetry objects, skipping malformed records."""
    telemetry_dir = Path(repo_root) / ".agent-runs"
    records: list[dict[str, Any]] = []
    malformed = 0
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(telemetry_dir, directory_flags)
    except OSError:
        return records, malformed

    try:
        with os.scandir(directory_fd) as entries:
            names = sorted(
                entry.name for entry in entries if entry.name.endswith(".json")
            )
        for name in names:
            try:
                flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(name, flags, dir_fd=directory_fd)
                with os.fdopen(descriptor, "rb") as telemetry_file:
                    file_stat = os.fstat(telemetry_file.fileno())
                    if not stat.S_ISREG(file_stat.st_mode):
                        raise ValueError("telemetry record must be a regular file")
                    if file_stat.st_nlink != 1:
                        raise ValueError("telemetry record must not be hard linked")
                    if file_stat.st_size > MAX_RECORD_BYTES:
                        raise ValueError("telemetry record is too large")
                    serialized = telemetry_file.read(MAX_RECORD_BYTES + 1)
                    if len(serialized) > MAX_RECORD_BYTES:
                        raise ValueError("telemetry record is too large")
                    record = json.loads(
                        serialized.decode("utf-8"),
                        parse_constant=_reject_non_finite_json,
                    )
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("run_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._-]+", record["run_id"])
                    or Path(name).stem != record["run_id"]
                ):
                    raise ValueError(
                        "telemetry record must contain a safe run_id matching its filename"
                    )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                RecursionError,
                ValueError,
            ):
                malformed += 1
                continue
            records.append(record)
    except OSError:
        return records, malformed
    finally:
        os.close(directory_fd)
    return records, malformed


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except OverflowError:
            return 0.0
        if math.isfinite(numeric):
            return max(0.0, numeric)
    return 0.0


def _display_label(value: Any, fallback: str = "unknown") -> str:
    """Return a single-line, safely encodable report label."""
    if not isinstance(value, str) or not value:
        return fallback
    label = "".join(
        "\\\\"
        if character == "\\"
        else character
        if character.isprintable() and not 0xD800 <= ord(character) <= 0xDFFF
        else f"\\u{ord(character):04x}"
        for character in value
    )
    if len(label) <= MAX_DISPLAY_LABEL_LENGTH:
        return label
    digest = hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:8]
    suffix = f"...[{digest}]"
    return label[: MAX_DISPLAY_LABEL_LENGTH - len(suffix)] + suffix


def aggregate_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate telemetry records into observatory statistics."""
    result: dict[str, Any] = {
        "total_runs": 0,
        "successful_runs": 0,
        "failed_runs": 0,
        "average_duration_seconds": 0.0,
        "agents": {},
        "files_changed": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "commits": 0,
        "runs_with_tests": 0,
        "tests_passed": 0,
    }
    duration_average = 0.0
    duration_count = 0

    for record in records:
        result["total_runs"] += 1
        successful = str(record.get("status", "")).lower() in SUCCESS_STATUSES
        result["successful_runs" if successful else "failed_runs"] += 1

        agent = _display_label(record.get("agent"))
        agent_stats = result["agents"].setdefault(agent, {"runs": 0, "successful": 0})
        agent_stats["runs"] += 1
        agent_stats["successful"] += int(successful)

        duration = record.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            try:
                numeric_duration = float(duration)
            except OverflowError:
                pass
            else:
                if math.isfinite(numeric_duration):
                    duration_count += 1
                    numeric_duration = max(0.0, numeric_duration)
                    duration_average += (
                        numeric_duration - duration_average
                    ) / duration_count

        for field in ("files_changed", "lines_added", "lines_deleted", "commits"):
            result[field] += int(_number(record.get(field)))

        tests_run = record.get("tests_run") is True
        if tests_run:
            result["runs_with_tests"] += 1
        if tests_run and record.get("tests_passed") is True:
            result["tests_passed"] += 1

    if duration_count:
        result["average_duration_seconds"] = duration_average
    return result


def render_report(stats: Mapping[str, Any], malformed: int = 0) -> str:
    """Render observatory statistics as a human-readable report."""
    total = stats["total_runs"]
    success_rate = 100 * stats["successful_runs"] / total if total else 0.0
    lines = [
        "AGENT RUN OBSERVATORY",
        "=====================",
        f"Total runs:       {total}",
        f"Successful runs:  {stats['successful_runs']}",
        f"Failed runs:      {stats['failed_runs']}",
        f"Success rate:     {success_rate:.1f}%",
        f"Average duration: {stats['average_duration_seconds']:.1f}s",
        "",
        "By agent:",
    ]
    if not stats["agents"]:
        lines.append("  No runs recorded")
    for agent, values in sorted(stats["agents"].items()):
        agent_rate = 100 * values["successful"] / values["runs"]
        lines.append(f"  {agent}: {values['runs']} runs, {agent_rate:.1f}% success")
    lines.extend(
        [
            "",
            f"Files changed:    {stats['files_changed']}",
            f"Lines added:      {stats['lines_added']}",
            f"Lines removed:    {stats['lines_deleted']}",
            f"Commits created:  {stats['commits']}",
            f"Runs with tests:  {stats['runs_with_tests']}",
            f"Tests passed:     {stats['tests_passed']}",
        ]
    )
    if malformed:
        lines.extend(["", f"Skipped malformed records: {malformed}"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Provide the narrow command used by shell orchestration."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record-first-mate")
    for option in ("control-root", "runtime", "worktree", "started-at"):
        record.add_argument(f"--{option}", required=True)
    for option in ("agent", "model", "provider"):
        record.add_argument(f"--{option}")
    record.add_argument("--tests-run", choices=("true", "false"), default="false")
    record.add_argument("--tests-passed", choices=("true", "false"), default="false")
    args = parser.parse_args(argv)

    succeeded = record_first_mate_run(
        args.control_root,
        args.runtime,
        args.worktree,
        args.started_at,
        args.agent or None,
        args.model or None,
        args.provider or None,
        args.tests_run == "true",
        args.tests_passed == "true",
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
