import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "agent_telemetry.py"
SPEC = importlib.util.spec_from_file_location("agent_telemetry", MODULE_PATH)
assert SPEC and SPEC.loader
telemetry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telemetry)


class AgentTelemetryTest(unittest.TestCase):
    def test_task_identifier_handles_surrogate_code_points(self):
        task = "task with invalid byte \udcff"

        identifier = telemetry.task_identifier(task)

        self.assertRegex(identifier, r"^[0-9a-f]{16}$")
        self.assertEqual(identifier, telemetry.task_identifier(task))

    def test_creates_one_private_record_per_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = {
                "run_id": "fm-123",
                "agent": "opencode",
                "task_id": telemetry.task_identifier("secret task prompt"),
                "status": "review",
                "unexpected": "must not be persisted",
            }

            destination = telemetry.write_record(temporary_directory, values)
            record = json.loads(destination.read_text(encoding="utf-8"))

            self.assertEqual(destination.name, "fm-123.json")
            self.assertEqual(set(record), set(telemetry.TELEMETRY_FIELDS))
            self.assertNotIn("secret task prompt", destination.read_text())
            self.assertNotIn("unexpected", record)

    def test_telemetry_storage_is_private(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir(mode=0o755)

            destination = telemetry.write_record(
                temporary_directory, {"run_id": "fm-private"}
            )

            self.assertEqual(telemetry_directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(destination.stat().st_mode & 0o077, 0)

    def test_published_record_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = telemetry.write_record(
                temporary_directory, {"run_id": "fm-read-only"}
            )

            self.assertEqual(destination.stat().st_mode & 0o777, 0o400)

    def test_syncs_record_and_directory_before_returning(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            real_fsync = os.fsync
            real_fchmod = os.fchmod
            real_unlink = os.unlink
            synced_modes = []
            events = []

            def capture_fsync(descriptor):
                synced_modes.append(os.fstat(descriptor).st_mode)
                events.append(("fsync", stat.S_IFMT(os.fstat(descriptor).st_mode)))
                return real_fsync(descriptor)

            def capture_fchmod(descriptor, mode):
                events.append(("fchmod", mode))
                return real_fchmod(descriptor, mode)

            def capture_unlink(path, **kwargs):
                events.append(("unlink", Path(path).suffix))
                return real_unlink(path, **kwargs)

            with mock.patch("os.fsync", side_effect=capture_fsync), mock.patch(
                "os.fchmod", side_effect=capture_fchmod
            ), mock.patch(
                "os.unlink", side_effect=capture_unlink
            ):
                destination = telemetry.write_record(
                    temporary_directory, {"run_id": "fm-durable"}
                )

            self.assertTrue(destination.exists())
            self.assertEqual(len(synced_modes), 2)
            self.assertTrue(stat.S_ISREG(synced_modes[0]))
            self.assertTrue(stat.S_ISDIR(synced_modes[1]))
            self.assertLess(
                events.index(("fchmod", 0o400)),
                events.index(("fsync", stat.S_IFREG)),
            )
            self.assertLess(
                events.index(("unlink", ".tmp")),
                events.index(("fsync", stat.S_IFDIR)),
            )

    def test_rejects_symbolic_link_telemetry_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            (root / ".agent-runs").symlink_to(outside, target_is_directory=True)

            self.assertFalse(
                telemetry.safe_write_record(root, {"run_id": "fm-symlink"})
            )
            self.assertFalse((outside / "fm-symlink.json").exists())
            self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is not supported")
    def test_does_not_follow_telemetry_directory_replaced_before_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            telemetry_directory = root / ".agent-runs"
            outside = root / "outside"
            outside.mkdir()
            real_open = os.open

            def replace_then_open(path, flags, *args, **kwargs):
                if Path(path) == telemetry_directory:
                    telemetry_directory.rmdir()
                    telemetry_directory.symlink_to(outside, target_is_directory=True)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=replace_then_open):
                written = telemetry.safe_write_record(root, {"run_id": "fm-swapped"})

            self.assertFalse(written)
            self.assertFalse((outside / "fm-swapped.json").exists())

    def test_missing_optional_metadata_is_null(self):
        record = telemetry.telemetry_record({"run_id": "fm-minimal"})

        self.assertEqual(record["run_id"], "fm-minimal")
        self.assertIsNone(record["model"])
        self.assertIsNone(record["tests_passed"])

    def test_safe_writer_swallows_storage_failures(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = {"run_id": "fm-duplicate"}
            self.assertTrue(telemetry.safe_write_record(temporary_directory, values))
            self.assertFalse(telemetry.safe_write_record(temporary_directory, values))

    def test_serialization_failure_does_not_leave_malformed_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = {"run_id": "fm-invalid", "model": object()}

            self.assertFalse(telemetry.safe_write_record(temporary_directory, values))
            self.assertFalse(
                (Path(temporary_directory) / ".agent-runs" / "fm-invalid.json").exists()
            )

    def test_non_finite_metadata_does_not_leave_invalid_json_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = {"run_id": "fm-non-finite", "duration_seconds": float("nan")}

            self.assertFalse(telemetry.safe_write_record(temporary_directory, values))
            self.assertFalse(
                (
                    Path(temporary_directory)
                    / ".agent-runs"
                    / "fm-non-finite.json"
                ).exists()
            )

    def test_oversized_metadata_does_not_leave_unreadable_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            values = {
                "run_id": "fm-oversized",
                "model": "x" * telemetry.MAX_RECORD_BYTES,
            }

            self.assertFalse(telemetry.safe_write_record(temporary_directory, values))
            self.assertFalse(
                (
                    Path(temporary_directory)
                    / ".agent-runs"
                    / "fm-oversized.json"
                ).exists()
            )

    def test_recursive_metadata_does_not_escape_best_effort_writer(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            recursive_model = []
            recursive_model.append(recursive_model)

            self.assertFalse(
                telemetry.safe_write_record(
                    temporary_directory,
                    {"run_id": "fm-recursive", "model": recursive_model},
                )
            )
            self.assertFalse(
                (
                    Path(temporary_directory)
                    / ".agent-runs"
                    / "fm-recursive.json"
                ).exists()
            )

    def test_publication_failure_does_not_leave_partial_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with mock.patch("os.link", side_effect=OSError("publication failed")):
                self.assertFalse(
                    telemetry.safe_write_record(
                        temporary_directory, {"run_id": "fm-unpublished"}
                    )
                )

            telemetry_dir = Path(temporary_directory) / ".agent-runs"
            self.assertEqual(list(telemetry_dir.iterdir()), [])

    def test_rejects_run_ids_that_escape_telemetry_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertFalse(
                telemetry.safe_write_record(
                    temporary_directory, {"run_id": "../outside"}
                )
            )

    def test_rejects_non_string_run_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertFalse(
                telemetry.safe_write_record(temporary_directory, {"run_id": 123})
            )

    def test_skips_record_with_non_string_run_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            (telemetry_directory / "invalid.json").write_text(
                '{"run_id": ["not", "an", "identifier"]}', encoding="utf-8"
            )
            (telemetry_directory / "unsafe.json").write_text(
                '{"run_id": "../not-an-identifier"}', encoding="utf-8"
            )

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 2)

    def test_skips_record_whose_run_id_does_not_match_filename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            (telemetry_directory / "renamed.json").write_text(
                '{"run_id":"original","status":"success"}', encoding="utf-8"
            )

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_skips_hard_linked_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            telemetry_directory = root / ".agent-runs"
            telemetry_directory.mkdir()
            outside = root / "outside.json"
            outside.write_text(
                '{"run_id":"linked","status":"success"}', encoding="utf-8"
            )
            os.link(outside, telemetry_directory / "linked.json")

            records, malformed = telemetry.read_records(root)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_calculates_duration(self):
        self.assertEqual(
            telemetry.duration_seconds(
                "2026-09-18T10:00:00Z", "2026-09-18T10:00:03.250Z"
            ),
            3.25,
        )

    def test_records_completed_first_mate_run_from_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime" / "fm-integrated"
            worktree = root / "worktree"
            runtime.mkdir(parents=True)
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
            (worktree / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            (runtime / "task.txt").write_text("private prompt", encoding="utf-8")
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("failed", encoding="utf-8")
            (worktree / "file.txt").write_text("base\nchange\n", encoding="utf-8")

            self.assertTrue(
                telemetry.record_first_mate_run(
                    root, runtime, worktree, "2026-09-18T10:00:00-04:00", "codex"
                )
            )
            record = json.loads((root / ".agent-runs" / "fm-integrated.json").read_text())

            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["agent"], "codex")
            self.assertEqual(record["files_changed"], 1)
            self.assertEqual(record["lines_added"], 1)
            self.assertEqual(record["commits"], 0)
            self.assertNotIn("private prompt", json.dumps(record))

    def test_malformed_git_counts_do_not_escape_first_mate_collection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime" / "fm-malformed-counts"
            worktree = root / "worktree"
            runtime.mkdir(parents=True)
            worktree.mkdir()
            (runtime / "base-commit").write_text("base", encoding="utf-8")
            (runtime / "status").write_text("failed", encoding="utf-8")
            oversized_count = "9" * 5000

            def git_result(command, **kwargs):
                if "diff" in command:
                    output = f"{oversized_count}\t1\tfile.txt\0"
                elif "rev-list" in command:
                    output = oversized_count
                else:
                    output = ""
                return subprocess.CompletedProcess(command, 0, output, "")

            with mock.patch("subprocess.run", side_effect=git_result):
                recorded = telemetry.record_first_mate_run(
                    root, runtime, worktree, "2026-09-18T10:00:00-04:00"
                )

            self.assertTrue(recorded)
            record = json.loads(
                (root / ".agent-runs" / "fm-malformed-counts.json").read_text()
            )
            self.assertEqual(record["files_changed"], 1)
            self.assertEqual(record["lines_added"], 0)
            self.assertEqual(record["lines_deleted"], 1)
            self.assertIsNone(record["commits"])

    def test_records_untracked_files_left_by_failed_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime" / "fm-untracked"
            worktree = root / "worktree"
            runtime.mkdir(parents=True)
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.name", "Test"],
                check=True,
            )
            (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("failed", encoding="utf-8")
            (worktree / "untracked.txt").write_text("one\ntwo\n", encoding="utf-8")

            self.assertTrue(
                telemetry.record_first_mate_run(
                    root, runtime, worktree, "2026-09-18T10:00:00-04:00"
                )
            )
            record = json.loads((root / ".agent-runs" / "fm-untracked.json").read_text())

            self.assertEqual(record["files_changed"], 1)
            self.assertEqual(record["lines_added"], 2)
            self.assertEqual(record["lines_deleted"], 0)

    def test_records_untracked_file_with_trailing_newline_in_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime" / "fm-untracked-newline"
            worktree = root / "worktree"
            runtime.mkdir(parents=True)
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.name", "Test"],
                check=True,
            )
            (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("failed", encoding="utf-8")
            (worktree / "line-break.txt\n").write_text("one\ntwo\n", encoding="utf-8")

            self.assertTrue(
                telemetry.record_first_mate_run(
                    root, runtime, worktree, "2026-09-18T10:00:00-04:00"
                )
            )
            record = json.loads(
                (root / ".agent-runs" / "fm-untracked-newline.json").read_text()
            )

            self.assertEqual(record["files_changed"], 1)
            self.assertEqual(record["lines_added"], 2)
            self.assertEqual(record["lines_deleted"], 0)

    def test_records_tracked_file_with_unquoted_newline_in_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime" / "fm-tracked-newline"
            worktree = root / "worktree"
            runtime.mkdir(parents=True)
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "core.quotePath", "false"],
                check=True,
            )
            changed_file = worktree / "line\nbreak.txt"
            changed_file.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("failed", encoding="utf-8")
            changed_file.write_text("base\nchange\n", encoding="utf-8")

            self.assertTrue(
                telemetry.record_first_mate_run(
                    root, runtime, worktree, "2026-09-18T10:00:00-04:00"
                )
            )
            record = json.loads(
                (root / ".agent-runs" / "fm-tracked-newline.json").read_text()
            )

            self.assertEqual(record["files_changed"], 1)
            self.assertEqual(record["lines_added"], 1)
            self.assertEqual(record["lines_deleted"], 0)

    def test_records_successful_first_mate_run_with_tests_and_commit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime" / "fm-success"
            worktree = root / "worktree"
            runtime.mkdir(parents=True)
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.name", "Test"],
                check=True,
            )
            (worktree / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (runtime / "task.txt").write_text("private prompt", encoding="utf-8")
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("review", encoding="utf-8")
            (worktree / "file.txt").write_text("base\ncompleted\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "file.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "commit", "-qm", "complete task"],
                check=True,
            )

            self.assertTrue(
                telemetry.record_first_mate_run(
                    root,
                    runtime,
                    worktree,
                    "2026-09-18T10:00:00-04:00",
                    "opencode",
                    "free",
                    "openrouter",
                    tests_run=True,
                    tests_passed=True,
                )
            )
            record = json.loads((root / ".agent-runs" / "fm-success.json").read_text())

            self.assertEqual(record["status"], "review")
            self.assertEqual(record["agent"], "opencode")
            self.assertEqual(record["model"], "free")
            self.assertEqual(record["provider"], "openrouter")
            self.assertTrue(record["tests_run"])
            self.assertTrue(record["tests_passed"])
            self.assertEqual(record["files_changed"], 1)
            self.assertEqual(record["lines_added"], 1)
            self.assertEqual(record["commits"], 1)
            self.assertNotIn("private prompt", json.dumps(record))

    def test_failed_acceptance_check_records_failed_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root = root / "control"
            scripts = control_root / "scripts"
            runtime = control_root / ".captain" / "runtime" / "fm-check-failure"
            worktree = root / "worktree"
            bin_directory = root / "bin"
            scripts.mkdir(parents=True)
            runtime.mkdir(parents=True)
            worktree.mkdir()
            bin_directory.mkdir()
            shutil.copy(MODULE_PATH, scripts / "agent_telemetry.py")
            shutil.copy(MODULE_PATH.parent / "first-mate-runner", scripts)
            opencode_arguments = root / "opencode-arguments"
            opencode = bin_directory / "opencode"
            opencode.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$OPENCODE_ARGUMENTS"\nexit 0\n',
                encoding="utf-8",
            )
            opencode.chmod(0o755)
            codex = bin_directory / "codex"
            codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex.chmod(0o755)

            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.name", "Test"],
                check=True,
            )
            (worktree / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (runtime / "task.txt").write_text("private prompt", encoding="utf-8")
            (runtime / "check.txt").write_text("exit 23", encoding="utf-8")
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("starting", encoding="utf-8")

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
            environment["OPENCODE_ARGUMENTS"] = str(opencode_arguments)
            result = subprocess.run(
                [
                    str(scripts / "first-mate-runner"),
                    "fm-check-failure",
                    str(control_root),
                    str(worktree),
                ],
                env=environment,
                check=False,
            )
            record = json.loads(
                (control_root / ".agent-runs" / "fm-check-failure.json").read_text()
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(record["status"], "failed")
            self.assertTrue(record["tests_run"])
            self.assertFalse(record["tests_passed"])
            self.assertNotIn("private prompt", json.dumps(record))
            self.assertEqual(
                opencode_arguments.read_text(encoding="utf-8").splitlines(),
                ["run --model openrouter/free private prompt"] * 2,
            )

    def test_failed_gnhf_run_records_terminal_agent_and_tests(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root = root / "control"
            scripts = control_root / "scripts"
            runtime = control_root / ".captain" / "runtime" / "fm-gnhf-failure"
            worktree = root / "worktree"
            bin_directory = root / "bin"
            scripts.mkdir(parents=True)
            runtime.mkdir(parents=True)
            worktree.mkdir()
            bin_directory.mkdir()
            shutil.copy(MODULE_PATH, scripts / "agent_telemetry.py")
            shutil.copy(MODULE_PATH.parent / "first-mate-gnhf-runner", scripts)

            opencode_arguments = root / "opencode-arguments"
            opencode = bin_directory / "opencode"
            opencode.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" > "$OPENCODE_ARGUMENTS"\nexit 0\n',
                encoding="utf-8",
            )
            opencode.chmod(0o755)
            gnhf = bin_directory / "gnhf"
            gnhf.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            gnhf.chmod(0o755)

            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "config", "user.name", "Test"],
                check=True,
            )
            (worktree / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "file.txt"], check=True)
            subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (runtime / "task.txt").write_text("private gnhf prompt", encoding="utf-8")
            (runtime / "check.txt").write_text("exit 23", encoding="utf-8")
            (runtime / "base-commit").write_text(base, encoding="utf-8")
            (runtime / "status").write_text("starting", encoding="utf-8")
            (runtime / "max-iterations").write_text("1", encoding="utf-8")
            (runtime / "codex-iterations").write_text("1", encoding="utf-8")

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
            environment["OPENCODE_ARGUMENTS"] = str(opencode_arguments)
            result = subprocess.run(
                [
                    str(scripts / "first-mate-gnhf-runner"),
                    "fm-gnhf-failure",
                    str(control_root),
                    str(worktree),
                ],
                env=environment,
                check=False,
            )
            record = json.loads(
                (control_root / ".agent-runs" / "fm-gnhf-failure.json").read_text()
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["agent"], "codex")
            self.assertEqual(record["provider"], "openai")
            self.assertTrue(record["tests_run"])
            self.assertFalse(record["tests_passed"])
            self.assertNotIn("private gnhf prompt", json.dumps(record))
            self.assertEqual(
                opencode_arguments.read_text(encoding="utf-8").strip(),
                "run --model openrouter/free private gnhf prompt",
            )

    def test_agent_run_records_failure_without_changing_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root = root / "control"
            scripts = control_root / "scripts"
            bin_directory = root / "bin"
            worktree = root / "worktree"
            scripts.mkdir(parents=True)
            bin_directory.mkdir()
            worktree.mkdir()
            shutil.copy(MODULE_PATH, scripts / "agent_telemetry.py")
            shutil.copy(MODULE_PATH.parent / "agent-run", scripts)

            codex = bin_directory / "codex"
            codex.write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")
            codex.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

            result = subprocess.run(
                [str(scripts / "agent-run"), "--codex", "private direct prompt"],
                cwd=worktree,
                env=environment,
                check=False,
            )
            records = list((control_root / ".agent-runs").glob("*.json"))
            self.assertEqual(result.returncode, 19)
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["agent"], "codex")
            self.assertEqual(record["provider"], "openai")
            self.assertEqual(record["status"], "failed")
            self.assertFalse(record["tests_run"])
            self.assertNotIn("private direct prompt", json.dumps(record))

    def test_aggregates_run_statistics(self):
        records = [
            {
                "run_id": "one",
                "agent": "opencode",
                "status": "review",
                "duration_seconds": 10,
                "tests_run": True,
                "tests_passed": True,
                "files_changed": 3,
                "lines_added": 20,
                "lines_deleted": 4,
                "commits": 1,
            },
            {
                "run_id": "two",
                "agent": "opencode",
                "status": "failed",
                "duration_seconds": 20,
                "tests_run": True,
                "tests_passed": False,
                "files_changed": 1,
                "lines_added": 2,
                "lines_deleted": 8,
                "commits": 0,
            },
        ]

        stats = telemetry.aggregate_records(records)

        self.assertEqual(stats["total_runs"], 2)
        self.assertEqual(stats["successful_runs"], 1)
        self.assertEqual(stats["failed_runs"], 1)
        self.assertEqual(stats["average_duration_seconds"], 15)
        self.assertEqual(stats["agents"]["opencode"], {"runs": 2, "successful": 1})
        self.assertEqual(stats["files_changed"], 4)
        self.assertEqual(stats["lines_added"], 22)
        self.assertEqual(stats["lines_deleted"], 12)
        self.assertEqual(stats["commits"], 1)
        self.assertEqual(stats["runs_with_tests"], 2)
        self.assertEqual(stats["tests_passed"], 1)

    def test_skips_malformed_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            (telemetry_directory / "valid.json").write_text(
                '{"run_id":"valid","status":"success"}', encoding="utf-8"
            )
            (telemetry_directory / "broken.json").write_text("{broken", encoding="utf-8")
            (telemetry_directory / "array.json").write_text("[]", encoding="utf-8")

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual([record["run_id"] for record in records], ["valid"])
            self.assertEqual(malformed, 2)

    def test_skips_invalid_utf8_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            (telemetry_directory / "valid.json").write_text(
                '{"run_id":"valid","status":"success"}', encoding="utf-8"
            )
            (telemetry_directory / "invalid-utf8.json").write_bytes(
                b'{"run_id":"invalid-utf8","model":"\xff"}'
            )

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual([record["run_id"] for record in records], ["valid"])
            self.assertEqual(malformed, 1)

    def test_skips_records_with_non_finite_json_numbers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            for run_id, value in (
                ("nan", "NaN"),
                ("infinity", "Infinity"),
                ("negative-infinity", "-Infinity"),
            ):
                (telemetry_directory / f"{run_id}.json").write_text(
                    f'{{"run_id":"{run_id}","duration_seconds":{value}}}',
                    encoding="utf-8",
                )

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 3)

    def test_skips_excessively_nested_json_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            (telemetry_directory / "nested.json").write_text(
                "[" * 2000 + "]" * 2000, encoding="utf-8"
            )

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_skips_oversized_record_without_reading_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            oversized = telemetry_directory / "oversized.json"
            oversized.write_bytes(b" " * (telemetry.MAX_RECORD_BYTES + 1))

            with mock.patch.object(
                Path, "read_text", side_effect=AssertionError("must not be read")
            ):
                records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_bounds_read_when_record_grows_after_size_check(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            oversized = telemetry_directory / "growing.json"
            oversized.write_bytes(b" " * (telemetry.MAX_RECORD_BYTES + 2))
            real_fstat = os.fstat

            def stale_fstat(descriptor):
                result = list(real_fstat(descriptor))
                result[6] = 0
                return os.stat_result(result)

            with mock.patch("os.fstat", side_effect=stale_fstat):
                records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_does_not_follow_symbolic_links_while_reading_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "external.json").write_text(
                '{"run_id":"external","status":"success"}', encoding="utf-8"
            )

            telemetry_directory = root / ".agent-runs"
            telemetry_directory.mkdir()
            (telemetry_directory / "linked.json").symlink_to(
                outside / "external.json"
            )

            records, malformed = telemetry.read_records(root)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

            shutil.rmtree(telemetry_directory)
            telemetry_directory.symlink_to(outside, target_is_directory=True)

            records, malformed = telemetry.read_records(root)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 0)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is not supported")
    def test_does_not_follow_telemetry_directory_replaced_before_open(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            telemetry_directory = root / ".agent-runs"
            telemetry_directory.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "external.json").write_text(
                '{"run_id":"external","status":"success"}', encoding="utf-8"
            )
            real_open = os.open

            def replace_then_open(path, flags, *args, **kwargs):
                if Path(path) == telemetry_directory:
                    telemetry_directory.rmdir()
                    telemetry_directory.symlink_to(outside, target_is_directory=True)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=replace_then_open):
                records, malformed = telemetry.read_records(root)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 0)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is not supported")
    def test_does_not_follow_record_replaced_before_open(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            telemetry_directory = root / ".agent-runs"
            telemetry_directory.mkdir()
            record_path = telemetry_directory / "swapped.json"
            record_path.write_text(
                '{"run_id":"swapped","status":"success"}', encoding="utf-8"
            )
            external = root / "external.json"
            external.write_text(
                '{"run_id":"swapped","status":"success"}', encoding="utf-8"
            )
            real_open = os.open

            def replace_then_open(path, flags, *args, **kwargs):
                record_path.unlink()
                record_path.symlink_to(external)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=replace_then_open):
                records, malformed = telemetry.read_records(root)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are not supported")
    def test_skips_special_files_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            telemetry_directory = Path(temporary_directory) / ".agent-runs"
            telemetry_directory.mkdir()
            os.mkfifo(telemetry_directory / "blocking.json")

            records, malformed = telemetry.read_records(temporary_directory)

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_aggregation_ignores_invalid_optional_values(self):
        stats = telemetry.aggregate_records(
            [
                {
                    "run_id": "odd",
                    "status": "failed",
                    "duration_seconds": float("nan"),
                    "files_changed": "many",
                    "tests_run": "false",
                    "tests_passed": True,
                }
            ]
        )

        self.assertEqual(stats["total_runs"], 1)
        self.assertEqual(stats["average_duration_seconds"], 0)
        self.assertEqual(stats["files_changed"], 0)
        self.assertEqual(stats["runs_with_tests"], 0)
        self.assertEqual(stats["tests_passed"], 0)

    def test_report_safely_renders_unusual_agent_labels(self):
        stats = telemetry.aggregate_records(
            [
                {"run_id": "control", "agent": "line\nbreak", "status": "success"},
                {
                    "run_id": "literal-escape",
                    "agent": r"line\u000abreak",
                    "status": "failed",
                },
                {"run_id": "surrogate", "agent": "bad\udcff", "status": "failed"},
                {"run_id": "container", "agent": ["not", "a", "label"]},
            ]
        )

        report = telemetry.render_report(stats)

        self.assertIn(r"line\u000abreak: 1 runs", report)
        self.assertIn(r"line\\u000abreak: 1 runs", report)
        self.assertIn(r"bad\udcff: 1 runs", report)
        self.assertIn("unknown: 1 runs", report)
        self.assertEqual(len(stats["agents"]), 4)
        report.encode("utf-8")

    def test_report_bounds_long_agent_labels_without_merging_them(self):
        stats = telemetry.aggregate_records(
            [
                {"run_id": "long-a", "agent": "a" * 1000, "status": "success"},
                {
                    "run_id": "long-b",
                    "agent": "a" * 999 + "b",
                    "status": "failed",
                },
            ]
        )

        report = telemetry.render_report(stats)
        agent_lines = [line for line in report.splitlines() if " runs," in line]

        self.assertEqual(len(agent_lines), 2)
        self.assertTrue(all(len(line) < 120 for line in agent_lines))
        self.assertNotIn("a" * 100, report)

    def test_aggregation_ignores_overflowing_numeric_values(self):
        enormous_integer = 10**10000

        stats = telemetry.aggregate_records(
            [
                {
                    "run_id": "overflow",
                    "status": "failed",
                    "duration_seconds": enormous_integer,
                    "files_changed": enormous_integer,
                    "lines_added": enormous_integer,
                    "lines_deleted": enormous_integer,
                    "commits": enormous_integer,
                }
            ]
        )

        self.assertEqual(stats["total_runs"], 1)
        self.assertEqual(stats["average_duration_seconds"], 0)
        self.assertEqual(stats["files_changed"], 0)
        self.assertEqual(stats["lines_added"], 0)
        self.assertEqual(stats["lines_deleted"], 0)
        self.assertEqual(stats["commits"], 0)

    def test_duration_average_does_not_overflow(self):
        stats = telemetry.aggregate_records(
            [
                {"run_id": "huge-one", "duration_seconds": 1e308},
                {"run_id": "huge-two", "duration_seconds": 1e308},
            ]
        )

        self.assertTrue(
            telemetry.math.isfinite(stats["average_duration_seconds"])
        )
        self.assertEqual(stats["average_duration_seconds"], 1e308)

    def test_empty_telemetry_directory_renders_useful_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            records, malformed = telemetry.read_records(temporary_directory)
            report = telemetry.render_report(
                telemetry.aggregate_records(records), malformed
            )

            self.assertIn("Total runs:       0", report)
            self.assertIn("Success rate:     0.0%", report)
            self.assertIn("No runs recorded", report)


if __name__ == "__main__":
    unittest.main()
