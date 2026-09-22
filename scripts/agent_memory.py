#!/usr/bin/env python3
"""Local, deterministic storage for validated shared agent memory."""

from __future__ import annotations

import builtins
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


MEMORY_TYPES = frozenset(
    {"DECISION", "DISCOVERY", "FAILURE", "CONVENTION", "COMMAND", "ARCHITECTURE"}
)
LIFECYCLE_STATES = frozenset({"active", "superseded", "invalid"})
ID_PATTERN = re.compile(r"^mem-[0-9a-f]{16}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_RECORD_BYTES = 64 * 1024
# Underscores separate terms so repository identifiers such as agent_context
# remain discoverable from natural-language tasks such as "agent context".
WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)

# Deliberately conservative. Rejected input is never included in an exception.
SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY(?: BLOCK)?-----",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk|rk|pk)-(?:live|test|proj)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bpypi-AgEI[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(
        r"\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
        r"client[_-]?secret|credential|secret|token|secret(?:[_-]access)?[_-]?key|private[_-]?key|"
        r"database[_-]?url)\s*[:=]\s*(?:[^\s,;]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:Authorization\s*:\s*)?Basic\s+[A-Za-z0-9+/]{4,}={0,2}(?![A-Za-z0-9+/=])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bAuthorization\s*:\s*(?:Api[-_ ]?Key|Token)\s+"
        r"[A-Za-z0-9._~+/=-]{4,}(?![A-Za-z0-9._~+/=-])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bAuthorization\s*:\s*Digest\s+[^\r\n]+",
        re.IGNORECASE,
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bhttps?://[^\s]*[?&](?:sig|signature|x-amz-signature|x-goog-signature)="
        r"[A-Za-z0-9%._~+/=-]{12,}",
        re.IGNORECASE,
    ),
)
DOTENV_ASSIGNMENT = re.compile(
    r"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*\S.*$",
    re.IGNORECASE,
)


class MemoryError(ValueError):
    """A safe, user-facing memory validation or storage error."""


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    return metadata.st_uid == os.geteuid()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Build a JSON object while rejecting ambiguous duplicate member names."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise MemoryError("malformed memory record")
        result[key] = value
    return result


def shared_repository_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Return the primary checkout root so linked worktrees share one store."""
    working_directory = Path(start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=working_directory,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return working_directory
    common_directory = Path(result.stdout.strip())
    if common_directory.name == ".git":
        return common_directory.parent
    return working_directory


def contains_secret(value: str) -> bool:
    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        return True
    dotenv_lines = 0
    for line in value.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#") and DOTENV_ASSIGNMENT.fullmatch(candidate):
            dotenv_lines += 1
            if dotenv_lines >= 2:
                return True
    return False


def _clean_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise MemoryError(f"{field} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MemoryError(f"{field} must be valid UTF-8 text") from error
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        and not character.isspace()
        for character in value
    ):
        raise MemoryError(f"{field} contains unsafe control characters")
    if contains_secret(value):
        raise MemoryError(f"{field} appears to contain secret material")
    cleaned = unicodedata.normalize("NFC", " ".join(value.split()))
    if not cleaned:
        raise MemoryError(f"{field} must not be empty")
    return cleaned


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _terms(value: str) -> frozenset[str]:
    return frozenset(WORD_PATTERN.findall(_normalize(value)))


def _fingerprint(memory_type: str, scope: str, tags: list[str], summary: str) -> str:
    canonical = json.dumps(
        [memory_type, _normalize(scope), sorted(_normalize(tag) for tag in tags), _normalize(summary)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _memory_id(fingerprint: str) -> str:
    return f"mem-{fingerprint[:16]}"


def _normalize_timestamp(value: str) -> str:
    value = _clean_text(value, "created_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MemoryError("created_at must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise MemoryError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _serialize(record: dict) -> bytes:
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise MemoryError("memory record is too large")
    return payload


def _validate_id(memory_id: str) -> None:
    if not isinstance(memory_id, str) or not ID_PATTERN.fullmatch(memory_id):
        raise MemoryError("invalid memory id")


class MemoryStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.directory = self.root / ".agent-memory"
        self.records_directory = self.directory / "records"
        self.lock_path = self.directory / ".lock"

    def _open_directories(self) -> tuple[int, int]:
        """Create and open the store without following its directory links."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(self.directory, mode=0o700)
        except FileExistsError:
            pass
        directory_fd = records_fd = -1
        try:
            directory_fd = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.mkdir("records", mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            records_fd = os.open(
                "records", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            if not all(
                _owned_by_current_user(os.fstat(descriptor))
                for descriptor in (directory_fd, records_fd)
            ):
                raise MemoryError("unsafe memory directory")
            os.fchmod(directory_fd, 0o700)
            os.fchmod(records_fd, 0o700)
            return directory_fd, records_fd
        except (OSError, MemoryError) as error:
            if records_fd >= 0:
                os.close(records_fd)
            if directory_fd >= 0:
                os.close(directory_fd)
            raise MemoryError("unsafe memory directory") from error

    @contextmanager
    def _lock(self) -> Iterator[int]:
        directory_fd, records_fd = self._open_directories()
        descriptor = -1
        try:
            descriptor = os.open(
                ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not _owned_by_current_user(metadata)
            ):
                os.close(descriptor)
                os.close(records_fd)
                os.close(directory_fd)
                raise MemoryError("unsafe memory lock")
            os.fchmod(descriptor, 0o600)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(records_fd)
            os.close(directory_fd)
            raise MemoryError("unsafe memory lock") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield records_fd
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            os.close(records_fd)
            os.close(directory_fd)

    def _atomic_write(self, record: dict, records_fd: int) -> None:
        destination = f"{record['id']}.json"
        temporary = f".{record['id']}.{secrets.token_hex(8)}.tmp"
        payload = _serialize(record)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            dir_fd=records_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary, destination, src_dir_fd=records_fd, dst_dir_fd=records_fd
            )
            os.fsync(records_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=records_fd)
            except FileNotFoundError:
                pass

    def _read_name(self, records_fd: int, memory_id: str) -> dict:
        _validate_id(memory_id)
        descriptor = -1
        try:
            descriptor = os.open(
                f"{memory_id}.json",
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=records_fd,
            )
            return self._read_descriptor(descriptor, memory_id)
        except OSError as error:
            raise MemoryError("unsafe memory record") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def add(
        self,
        *,
        memory_type: str,
        summary: str,
        scope: str = "repository",
        tags: list[str] | None = None,
        confidence: float = 1.0,
        run_id: str | None = None,
        commit: str | None = None,
        branch: str | None = None,
        created_at: str | None = None,
    ) -> dict:
        memory_type = _clean_text(memory_type, "type").upper()
        if memory_type not in MEMORY_TYPES:
            raise MemoryError(f"type must be one of: {', '.join(sorted(MEMORY_TYPES))}")
        summary = _clean_text(summary, "summary")
        scope = _clean_text(scope, "scope")
        if tags is not None and not isinstance(tags, list):
            raise MemoryError("tags must be a list")
        clean_tags = sorted({_clean_text(tag, "tag").casefold() for tag in (tags or [])})
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise MemoryError("confidence must be between 0 and 1")
        provenance = {}
        for key, value in (("source_run", run_id), ("source_commit", commit), ("branch", branch)):
            if value is not None:
                provenance[key] = _clean_text(value, key)
        timestamp = _normalize_timestamp(
            created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        fingerprint = _fingerprint(memory_type, scope, clean_tags, summary)
        record = {
            "id": _memory_id(fingerprint),
            "type": memory_type,
            "scope": scope,
            "tags": clean_tags,
            "summary": summary,
            "status": "active",
            "confidence": float(confidence),
            "provenance": provenance,
            "created_at": timestamp,
            "fingerprint": fingerprint,
        }
        with self._lock() as records_fd:
            try:
                existing = self._read_name(records_fd, record["id"])
            except MemoryError as error:
                try:
                    os.stat(
                        f"{record['id']}.json",
                        dir_fd=records_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    existing = None
                else:
                    raise error
            if existing is not None:
                if existing.get("fingerprint") == fingerprint:
                    raise MemoryError(f"duplicate memory: {record['id']}")
                raise MemoryError("memory id collision")
            self._atomic_write(record, records_fd)
        return record

    def _read_descriptor(self, descriptor: int, expected_id: str) -> dict:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not _owned_by_current_user(metadata)
            or metadata.st_mode & 0o077
            or metadata.st_size > MAX_RECORD_BYTES
        ):
            raise MemoryError("unsafe memory record")
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_RECORD_BYTES:
            raise MemoryError("unsafe memory record")
        try:
            record = json.loads(
                payload.decode("utf-8"), object_pairs_hook=_unique_object
            )
        except (
            UnicodeError,
            json.JSONDecodeError,
            MemoryError,
            RecursionError,
            builtins.MemoryError,
        ) as error:
            raise MemoryError("malformed memory record") from error
        if not self._valid_record(record, expected_id):
            raise MemoryError("malformed memory record")
        return record

    @staticmethod
    def _valid_record(record: object, expected_id: str) -> bool:
        if not isinstance(record, dict):
            return False
        allowed_fields = {
            "id", "type", "scope", "tags", "summary", "status", "confidence",
            "provenance", "created_at", "fingerprint", "superseded_by",
        }
        if not set(record) <= allowed_fields:
            return False
        if record.get("id") != expected_id or record.get("status") not in LIFECYCLE_STATES:
            return False
        if record["status"] == "superseded":
            superseded_by = record.get("superseded_by")
            if (
                not isinstance(superseded_by, str)
                or not ID_PATTERN.fullmatch(superseded_by)
                or superseded_by == record["id"]
            ):
                return False
        elif "superseded_by" in record:
            return False
        if record.get("type") not in MEMORY_TYPES:
            return False
        if not isinstance(record.get("scope"), str) or not record["scope"]:
            return False
        if not isinstance(record.get("summary"), str) or not record["summary"]:
            return False
        tags = record.get("tags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags):
            return False
        confidence = record.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            return False
        provenance = record.get("provenance")
        if not isinstance(provenance, dict) or any(
            key not in {"source_run", "source_commit", "branch"}
            or not isinstance(value, str)
            or not value
            for key, value in provenance.items()
        ):
            return False
        try:
            if record["scope"] != _clean_text(record["scope"], "scope"):
                return False
            if record["summary"] != _clean_text(record["summary"], "summary"):
                return False
            if tags != sorted({_clean_text(tag, "tag").casefold() for tag in tags}):
                return False
            if any(value != _clean_text(value, key) for key, value in provenance.items()):
                return False
        except MemoryError:
            return False
        if not isinstance(record.get("created_at"), str) or not record["created_at"]:
            return False
        try:
            if _normalize_timestamp(record["created_at"]) != record["created_at"]:
                return False
        except MemoryError:
            return False
        fingerprint = record.get("fingerprint")
        if not isinstance(fingerprint, str) or not FINGERPRINT_PATTERN.fullmatch(fingerprint):
            return False
        expected_fingerprint = _fingerprint(
            record["type"], record["scope"], tags, record["summary"]
        )
        if fingerprint != expected_fingerprint or record["id"] != _memory_id(fingerprint):
            return False
        strings = [record["scope"], record["summary"], record["created_at"], *tags, *provenance.values()]
        return not any(contains_secret(value) for value in strings)

    def list(self, *, include_inactive: bool = True) -> tuple[list[dict], int]:
        directory_fd = records_fd = -1
        try:
            directory_fd = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            directory_metadata = os.fstat(directory_fd)
            if (
                not _owned_by_current_user(directory_metadata)
                or directory_metadata.st_mode & 0o077
            ):
                raise MemoryError("unsafe memory directory")
            records_fd = os.open(
                "records", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            records_metadata = os.fstat(records_fd)
            if (
                not _owned_by_current_user(records_metadata)
                or records_metadata.st_mode & 0o077
            ):
                raise MemoryError("unsafe memory directory")
            records = []
            directory_names = os.listdir(records_fd)
            malformed = sum(
                name.endswith(".json")
                and not ID_PATTERN.fullmatch(name.removesuffix(".json"))
                for name in directory_names
            )
            names = sorted(
                name for name in directory_names
                if name.endswith(".json")
                and ID_PATTERN.fullmatch(name.removesuffix(".json"))
            )
            for name in names:
                descriptor = -1
                try:
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                        dir_fd=records_fd,
                    )
                    record = self._read_descriptor(
                        descriptor, name.removesuffix(".json")
                    )
                except (MemoryError, OSError):
                    malformed += 1
                    continue
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                records.append(record)
            valid_ids = {record["id"] for record in records}
            while True:
                dangling_ids = {
                    record["id"] for record in records
                    if record["status"] == "superseded"
                    and record["superseded_by"] not in valid_ids
                }
                if not dangling_ids:
                    break
                malformed += len(dangling_ids)
                records = [
                    record for record in records if record["id"] not in dangling_ids
                ]
                valid_ids -= dangling_ids
            by_id = {record["id"]: record for record in records}
            cyclic_ids = set()
            for record in records:
                path = []
                current = record
                while current["status"] == "superseded":
                    if current["id"] in path:
                        cyclic_ids.update(path)
                        break
                    path.append(current["id"])
                    current = by_id[current["superseded_by"]]
            if cyclic_ids:
                malformed += len(cyclic_ids)
                records = [
                    record for record in records if record["id"] not in cyclic_ids
                ]
            if not include_inactive:
                records = [record for record in records if record["status"] == "active"]
            return records, malformed
        except FileNotFoundError as error:
            # A wholly absent store is the supported empty-store case. Once the
            # top-level store has opened successfully, a missing records
            # directory indicates a damaged or concurrently altered boundary
            # and must not be silently reported as empty memory.
            if directory_fd < 0:
                return [], 0
            raise MemoryError("unsafe memory directory") from error
        except OSError as error:
            raise MemoryError("unsafe memory directory") from error
        finally:
            if records_fd >= 0:
                os.close(records_fd)
            if directory_fd >= 0:
                os.close(directory_fd)

    def search(self, query: str, *, include_inactive: bool = False) -> tuple[list[dict], int]:
        """Return matching memories in a deterministic, explainable order."""
        query = _clean_text(query, "query")
        query_terms = _terms(query)
        records, malformed = self.list(include_inactive=include_inactive)
        results = []
        for record in records:
            summary_matches = sorted(query_terms & _terms(record.get("summary", "")))
            tag_terms = frozenset().union(
                *(_terms(tag) for tag in record.get("tags", []))
            )
            tag_matches = sorted(query_terms & tag_terms)
            scope_matches = sorted(query_terms & _terms(record.get("scope", "")))
            type_matches = sorted(query_terms & _terms(record.get("type", "")))
            if not (summary_matches or tag_matches or scope_matches or type_matches):
                continue
            score = (
                4 * len(summary_matches)
                + 8 * len(tag_matches)
                + 2 * len(scope_matches)
                + len(type_matches)
            )
            results.append(
                {
                    "record": record,
                    "relevance": {
                        "score": score,
                        "summary_terms": summary_matches,
                        "tag_terms": tag_matches,
                        "scope_terms": scope_matches,
                        "type_terms": type_matches,
                    },
                }
            )
        results.sort(
            key=lambda result: (
                -result["relevance"]["score"],
                -result["record"].get("confidence", 0),
                -datetime.fromisoformat(
                    result["record"]["created_at"].replace("Z", "+00:00")
                ).timestamp(),
                result["record"]["id"],
            )
        )
        return results, malformed

    def stats(self) -> dict:
        records, malformed = self.list()
        by_status = {status: 0 for status in sorted(LIFECYCLE_STATES)}
        by_type = {memory_type: 0 for memory_type in sorted(MEMORY_TYPES)}
        for record in records:
            by_status[record["status"]] += 1
            by_type[record["type"]] += 1
        return {
            "total": len(records),
            "malformed": malformed,
            "by_status": by_status,
            "by_type": by_type,
        }

    def invalidate(self, memory_id: str) -> dict:
        return self._set_status(memory_id, "invalid")

    def supersede(self, old_id: str, new_id: str) -> dict:
        _validate_id(new_id)
        if old_id == new_id:
            raise MemoryError("a memory cannot supersede itself")
        with self._lock() as records_fd:
            new_record = self._read_name(records_fd, new_id)
            if new_record["status"] != "active":
                raise MemoryError("replacement memory must be active")
            old_record = self._read_name(records_fd, old_id)
            if old_record["status"] != "active":
                raise MemoryError("only active memory can be superseded")
            old_record["status"] = "superseded"
            old_record["superseded_by"] = new_id
            self._atomic_write(old_record, records_fd)
            return old_record

    def _set_status(self, memory_id: str, status: str) -> dict:
        with self._lock() as records_fd:
            record = self._read_name(records_fd, memory_id)
            if record["status"] != "active":
                raise MemoryError("only active memory can change lifecycle state")
            record["status"] = status
            self._atomic_write(record, records_fd)
            return record
