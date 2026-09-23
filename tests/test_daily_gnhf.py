import datetime as dt
import importlib.util
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "daily_gnhf.py"
SPEC = importlib.util.spec_from_file_location("daily_gnhf", MODULE_PATH)
assert SPEC and SPEC.loader
daily = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily)


def claim_in_process(root, today, start, results):
    start.wait()
    record = daily.claim_next(Path(root), today, 3)
    results.put(record["task_id"] if record else None)


def enqueue_in_process(root, prompt, start, results):
    start.wait()
    try:
        record = daily.enqueue(
            Path(root), Path(prompt), dt.date(2026, 9, 22), "Concurrent task"
        )
        results.put(("created", record["task_id"]))
    except daily.TaskError as exc:
        results.put(("rejected", str(exc)))


def status_in_process(root, started, results):
    started.set()
    results.put(daily.status(Path(root), dt.date(2026, 9, 22)))


def show_in_process(root, started, results):
    started.set()
    results.put(daily.show_task(Path(root), "daily-2026-09-22"))


def history_in_process(root, started, results):
    started.set()
    results.put(daily.task_history(Path(root)))


class DailyGnhfInboxTest(unittest.TestCase):
    def test_initialize_enforces_private_directory_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "tasks"
            root.mkdir(mode=0o755)
            for name in (*daily.STATES, "prompts"):
                (root / name).mkdir(mode=0o755)

            daily.initialize(root)

            self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
            for name in (*daily.STATES, "prompts"):
                self.assertEqual(os.stat(root / name).st_mode & 0o777, 0o700)

    def test_initialize_rejects_symlinked_queue_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "tasks"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / "pending").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                daily.TaskError, "task directory must not be a symlink"
            ):
                daily.initialize(root)

            self.assertEqual(list(outside.iterdir()), [])

    def test_queue_lock_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "tasks"
            daily.initialize(root)
            outside = base / "outside.lock"
            outside.write_text("preserve me")
            os.chmod(outside, 0o644)
            (root / ".runner.lock").symlink_to(outside)

            with self.assertRaisesRegex(daily.TaskError, "cannot safely open queue lock"):
                with daily.queue_lock(root):
                    self.fail("unsafe lock was acquired")

            self.assertEqual(outside.read_text(), "preserve me")
            self.assertEqual(os.stat(outside).st_mode & 0o777, 0o644)

    def test_claim_rejects_symlinked_metadata_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            metadata = root / "pending/daily-2026-09-22.json"
            outside = base / "outside.json"
            outside.write_bytes(metadata.read_bytes())
            metadata.unlink()
            metadata.symlink_to(outside)

            with self.assertRaisesRegex(
                daily.TaskError, "cannot safely open task metadata"
            ):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertTrue(metadata.is_symlink())
            self.assertEqual(outside.read_bytes(), metadata.read_bytes())
            self.assertEqual(list((root / "running").iterdir()), [])

    def test_atomic_create_syncs_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            path = parent / "task.json"

            with mock.patch.object(daily, "fsync_directory") as sync:
                daily.atomic_create(path, b"{}")

            self.assertEqual(path.read_bytes(), b"{}")
            sync.assert_called_once_with(parent)

    def test_atomic_create_publishes_only_fully_written_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            path = parent / "task.json"
            original_link = daily.os.link

            def inspect_before_publish(source, destination):
                self.assertFalse(path.exists())
                self.assertEqual(Path(source).read_bytes(), b'{"complete": true}')
                original_link(source, destination)

            with mock.patch.object(daily.os, "link", side_effect=inspect_before_publish):
                daily.atomic_create(path, b'{"complete": true}')

            self.assertEqual(path.read_bytes(), b'{"complete": true}')
            self.assertEqual(list(parent.glob(".task.json.*")), [])

    def test_atomic_create_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            path = parent / "task.json"
            path.write_bytes(b"existing")

            with mock.patch.object(daily, "fsync_directory") as sync:
                with self.assertRaises(FileExistsError):
                    daily.atomic_create(path, b"replacement")

            self.assertEqual(path.read_bytes(), b"existing")
            self.assertEqual(list(parent.glob(".task.json.*")), [])
            sync.assert_called_once_with(parent)

    def test_atomic_replace_failure_durably_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            path = parent / "task.json"
            path.write_bytes(b"existing")

            with mock.patch.object(
                daily.os, "replace", side_effect=OSError("replace failed")
            ), mock.patch.object(daily, "fsync_directory") as sync:
                with self.assertRaisesRegex(OSError, "replace failed"):
                    daily.atomic_replace(path, b"replacement")

            self.assertEqual(path.read_bytes(), b"existing")
            self.assertEqual(list(parent.glob(".task.json.*")), [])
            sync.assert_called_once_with(parent)

    def test_durable_move_syncs_both_state_directories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            source_directory = base / "pending"
            destination_directory = base / "running"
            source_directory.mkdir()
            destination_directory.mkdir()
            source = source_directory / "task.json"
            destination = destination_directory / "task.json"
            source.write_text("{}")

            with mock.patch.object(daily, "fsync_directory") as sync:
                daily.durable_move(source, destination)

            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            self.assertEqual(
                sync.call_args_list,
                [mock.call(destination_directory), mock.call(source_directory)],
            )

    def test_durable_unlink_syncs_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            path = parent / "orphan-prompt.md"
            path.write_text("work")

            with mock.patch.object(daily, "fsync_directory") as sync:
                daily.durable_unlink(path)

            self.assertFalse(path.exists())
            sync.assert_called_once_with(parent)

    def test_launch_durably_removes_run_id_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(
                base,
                'printf "fm-test-run\\n" > "$7"\n',
            )

            original_unlink = daily.durable_unlink
            with mock.patch.object(
                daily, "durable_unlink", wraps=original_unlink
            ) as unlink:
                record = daily.launch_next(
                    root,
                    dt.date(2026, 9, 22),
                    3,
                    base,
                    launcher,
                    "true",
                )

            self.assertEqual(record["run_id"], "fm-test-run")
            run_id_cleanup = [
                call.args[0]
                for call in unlink.call_args_list
                if call.args[0].parent == root
                and call.args[0].name.startswith(".run-id.")
            ]
            self.assertEqual(len(run_id_cleanup), 1)
            self.assertFalse(run_id_cleanup[0].exists())

    def make_launcher(self, base, body):
        launcher = base / "first-mate"
        launcher.write_text("#!/bin/sh\n" + body)
        launcher.chmod(0o755)
        return launcher

    def write_config(self, path, **overrides):
        config = {
            "enabled": True,
            "catch_up": True,
            "catch_up_mode": "oldest_first",
            "max_catch_up_tasks": 1,
            "max_attempts": 3,
            "stale_after_hours": 24,
            "execution_mode": "sequential",
            "acceptance_check": "python3 -m unittest discover -s tests",
            "auto_push": False,
            "auto_merge": False,
        }
        config.update(overrides)
        path.write_text(json.dumps(config))

    def test_config_rejects_truthy_string_instead_of_enabling_runner(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            self.write_config(path, enabled="false")

            with self.assertRaisesRegex(daily.TaskError, "enabled must be a boolean"):
                daily.load_config(path)

    def test_config_rejects_symlink_without_reading_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            target = base / "outside.json"
            self.write_config(target)
            path = base / "config.json"
            path.symlink_to(target)

            with self.assertRaisesRegex(
                daily.TaskError, "cannot safely open configuration"
            ):
                daily.load_config(path)

            self.assertTrue(path.is_symlink())
            self.assertEqual(json.loads(target.read_text())["enabled"], True)

    def test_config_rejects_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(
                daily.TaskError, "cannot load configuration"
            ):
                daily.load_config(path)

    def test_config_rejects_unsupported_execution_automation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            for override, message in (
                ({"execution_mode": "parallel"}, "execution_mode must be sequential"),
                ({"auto_push": True}, "auto_push is not supported"),
                ({"auto_merge": True}, "auto_merge is not supported"),
            ):
                with self.subTest(override=override):
                    self.write_config(path, **override)
                    with self.assertRaisesRegex(daily.TaskError, message):
                        daily.load_config(path)

    def test_enqueue_creates_deterministic_private_task_and_separate_prompt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "input.md"
            prompt.write_text("Treat $(touch /tmp/nope) as plain agent input.")

            record = daily.enqueue(
                base / "tasks", prompt, dt.date(2026, 9, 22), "Safe task"
            )

            self.assertEqual(record["task_id"], "daily-2026-09-22")
            self.assertEqual(record["status"], "PENDING")
            self.assertEqual(record["attempts"], 0)
            self.assertEqual(
                (base / "tasks/prompts/daily-2026-09-22.md").read_text(),
                prompt.read_text(),
            )
            metadata = json.loads(
                (base / "tasks/pending/daily-2026-09-22.json").read_text()
            )
            self.assertNotIn("touch /tmp/nope", json.dumps(metadata))
            self.assertEqual((base / "tasks").stat().st_mode & 0o077, 0)

    def test_enqueue_rejects_non_utf8_prompt_without_queue_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "input.md"
            prompt.write_bytes(b"invalid: \xff")
            root = base / "tasks"

            with self.assertRaisesRegex(daily.TaskError, "valid UTF-8"):
                daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)

            self.assertFalse(root.exists())

    def test_enqueue_reads_prompt_through_safe_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("safe contents")
            original_open = daily.os.open

            def replace_source(path, flags, *args, **kwargs):
                if Path(path) == prompt:
                    outside = base / "outside.md"
                    outside.write_text("substituted contents")
                    prompt.unlink()
                    prompt.symlink_to(outside)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(daily.os, "open", side_effect=replace_source):
                with self.assertRaisesRegex(
                    daily.TaskError, "prompt is not a safe regular file"
                ):
                    daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)

            self.assertFalse(root.exists())

    def test_duplicate_daily_task_is_rejected_across_states(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)

            with self.assertRaisesRegex(daily.TaskError, "already exists"):
                daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)

    def test_concurrent_enqueues_preserve_one_complete_task(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            context = multiprocessing.get_context("fork")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=enqueue_in_process,
                    args=(str(root), str(prompt), start, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(5)

            self.assertTrue(all(process.exitcode == 0 for process in processes))
            outcomes = [results.get(timeout=1), results.get(timeout=1)]
            self.assertEqual([outcome[0] for outcome in outcomes].count("created"), 1)
            self.assertEqual([outcome[0] for outcome in outcomes].count("rejected"), 1)
            self.assertTrue((root / "pending/daily-2026-09-22.json").is_file())
            self.assertEqual(
                (root / "prompts/daily-2026-09-22.md").read_text(), "work"
            )

    def test_status_detects_only_prior_pending_dates_as_missed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            for day in (20, 21, 22):
                daily.enqueue(root, prompt, dt.date(2026, 9, day), None)

            result = daily.status(root, dt.date(2026, 9, 22))

            self.assertEqual(
                [item["task_id"] for item in result["missed"]],
                ["daily-2026-09-20", "daily-2026-09-21"],
            )
            self.assertEqual(len(result["pending"]), 3)

    def test_malformed_task_is_reported_without_hiding_valid_tasks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "tasks"
            daily.initialize(root)
            (root / "pending/broken.json").write_text("{")

            result = daily.status(root, dt.date(2026, 9, 22))

            self.assertEqual(result["pending"], [])
            self.assertEqual(len(result["errors"]), 1)

    def test_status_reports_duplicate_state_as_corruption(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            record = daily.enqueue(root, prompt, dt.date(2026, 9, 20), None)
            duplicate = dict(record, status="COMPLETED")
            daily.atomic_create(
                root / "completed/daily-2026-09-20.json",
                (json.dumps(duplicate) + "\n").encode(),
            )

            result = daily.status(root, dt.date(2026, 9, 22))

            self.assertEqual(result["pending"], [])
            self.assertEqual(result["completed"], [])
            self.assertEqual(result["missed"], [])
            self.assertEqual(len(result["errors"]), 1)
            self.assertEqual(result["errors"][0]["file"], "daily-2026-09-20.json")
            self.assertIn("multiple states", result["errors"][0]["error"])

    def test_status_waits_for_atomic_queue_mutation_to_finish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            metadata_path = root / "pending/daily-2026-09-22.json"
            original = metadata_path.read_text()
            context = multiprocessing.get_context("fork")
            started = context.Event()
            results = context.Queue()
            process = context.Process(
                target=status_in_process, args=(str(root), started, results)
            )

            with daily.queue_lock(root):
                metadata = json.loads(original)
                metadata["status"] = "RUNNING"
                metadata_path.write_text(json.dumps(metadata))
                process.start()
                self.assertTrue(started.wait(1))
                process.join(0.2)
                self.assertTrue(process.is_alive())
                metadata_path.write_text(original)

            process.join(5)
            self.assertEqual(process.exitcode, 0)
            result = results.get(timeout=1)
            self.assertEqual(result["errors"], [])
            self.assertEqual(
                [item["task_id"] for item in result["pending"]],
                ["daily-2026-09-22"],
            )

    def test_status_exposes_old_orphaned_running_task_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            metadata_path = root / "running" / f"{record['task_id']}.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["started_at"] = "2026-09-20T12:00:00+00:00"
            metadata_path.write_text(json.dumps(metadata))

            result = daily.status(
                root,
                dt.date(2026, 9, 22),
                base / "repo",
                now=dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(len(result["stale_running"]), 1)
            self.assertEqual(
                result["stale_running"][0]["reason"], "no linked Captain run"
            )

    def test_status_keeps_old_task_active_when_captain_status_exists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-active"})
            metadata_path = root / "running" / f"{record['task_id']}.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["started_at"] = "2026-09-20T12:00:00+00:00"
            metadata_path.write_text(json.dumps(metadata))
            runtime = base / "repo/.captain/runtime/fm-active"
            runtime.mkdir(parents=True)
            (runtime / "status").write_text("opencode-primary\n")

            result = daily.status(
                root,
                dt.date(2026, 9, 22),
                base / "repo",
                now=dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(result["stale_running"], [])

    def test_status_exposes_old_task_with_unreadable_captain_status_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-unreadable"})
            metadata_path = root / "running" / f"{record['task_id']}.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["started_at"] = "2026-09-20T12:00:00+00:00"
            metadata_path.write_text(json.dumps(metadata))
            runtime = base / "repo/.captain/runtime/fm-unreadable"
            runtime.mkdir(parents=True)
            (runtime / "status").write_bytes(b"\xff")

            result = daily.status(
                root,
                dt.date(2026, 9, 22),
                base / "repo",
                now=dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(len(result["stale_running"]), 1)
            self.assertEqual(
                result["stale_running"][0]["reason"],
                "linked Captain status is unreadable",
            )

    def test_status_exposes_old_task_with_unknown_captain_status_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-unknown"})
            metadata_path = root / "running" / f"{record['task_id']}.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["started_at"] = "2026-09-20T12:00:00+00:00"
            metadata_path.write_text(json.dumps(metadata))
            runtime = base / "repo/.captain/runtime/fm-unknown"
            runtime.mkdir(parents=True)
            (runtime / "status").write_text("unexpected-state\n")

            result = daily.status(
                root,
                dt.date(2026, 9, 22),
                base / "repo",
                now=dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(len(result["stale_running"]), 1)
            self.assertEqual(
                result["stale_running"][0]["reason"],
                "linked Captain status is unrecognized",
            )

    def test_claim_moves_oldest_eligible_task_to_running(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            for day in (22, 20, 21):
                daily.enqueue(root, prompt, dt.date(2026, 9, day), None)

            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertEqual(record["task_id"], "daily-2026-09-20")
            self.assertEqual(record["status"], "RUNNING")
            self.assertEqual(record["attempts"], 1)
            self.assertIsNotNone(record["started_at"])
            self.assertFalse((root / "pending/daily-2026-09-20.json").exists())
            self.assertTrue((root / "running/daily-2026-09-20.json").exists())

    def test_claim_recovers_interrupted_prepared_transition(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            path = root / "pending/daily-2026-09-22.json"
            prepared = json.loads(path.read_text())
            prepared.update(
                status="RUNNING",
                started_at="2026-09-22T12:00:00+00:00",
                attempts=1,
                claim_prepared=True,
            )
            daily.atomic_replace(path, (json.dumps(prepared) + "\n").encode())

            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertEqual(record["status"], "RUNNING")
            self.assertEqual(record["attempts"], 1)
            self.assertNotIn("claim_prepared", record)
            persisted = json.loads(
                (root / "running/daily-2026-09-22.json").read_text()
            )
            self.assertNotIn("claim_prepared", persisted)

    def test_claim_recovers_interruption_after_state_rename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            pending = root / "pending/daily-2026-09-22.json"
            prepared = json.loads(pending.read_text())
            prepared.update(
                status="RUNNING",
                started_at="2026-09-22T12:00:00+00:00",
                attempts=1,
                claim_prepared=True,
            )
            daily.atomic_replace(pending, (json.dumps(prepared) + "\n").encode())
            running = root / "running/daily-2026-09-22.json"
            pending.replace(running)

            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertEqual(record["status"], "RUNNING")
            self.assertEqual(record["attempts"], 1)
            self.assertNotIn("claim_prepared", record)

    def test_claim_rejects_missing_prompt_without_mutating_queue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            (root / "prompts/daily-2026-09-22.md").unlink()

            with self.assertRaisesRegex(daily.TaskError, "missing or unsafe"):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_claim_rejects_symlinked_prompt_without_reading_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            stored = root / "prompts/daily-2026-09-22.md"
            outside = base / "outside.md"
            outside.write_text("must not execute")
            stored.unlink()
            stored.symlink_to(outside)

            with self.assertRaisesRegex(daily.TaskError, "missing or unsafe"):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertTrue(stored.is_symlink())
            self.assertEqual(outside.read_text(), "must not execute")
            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_claim_rejects_prompt_through_symlinked_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.md").write_text("must not execute")
            (root / "prompts/escape").symlink_to(outside, target_is_directory=True)
            pending = root / "pending/daily-2026-09-22.json"
            record = json.loads(pending.read_text())
            record["prompt_file"] = "prompts/escape/secret.md"
            daily.atomic_replace(pending, (json.dumps(record) + "\n").encode())

            with self.assertRaisesRegex(daily.TaskError, "directly inside prompts"):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertEqual((outside / "secret.md").read_text(), "must not execute")
            self.assertTrue(pending.exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_claim_rejects_metadata_whose_task_id_does_not_match_filename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            source = root / "pending/daily-2026-09-22.json"
            mismatched = root / "pending/daily-2026-09-21.json"
            source.replace(mismatched)

            with self.assertRaisesRegex(daily.TaskError, "does not match.*filename"):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertTrue(mismatched.exists())
            self.assertFalse(any((root / "running").glob("*.json")))

    def test_claim_rejects_completed_task_duplicated_in_pending(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            pending = root / "pending/daily-2026-09-22.json"
            completed = json.loads(pending.read_text())
            completed["status"] = "COMPLETED"
            daily.atomic_create(
                root / "completed/daily-2026-09-22.json",
                (json.dumps(completed) + "\n").encode(),
            )

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertTrue(pending.exists())
            self.assertFalse(any((root / "running").glob("*.json")))

    def test_claim_rejects_undecodable_prompt_without_mutating_queue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            (root / "prompts/daily-2026-09-22.md").write_bytes(b"\xff")

            with self.assertRaisesRegex(daily.TaskError, "not readable UTF-8"):
                daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_concurrent_claimers_cannot_claim_the_same_task(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            context = multiprocessing.get_context("fork")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=claim_in_process,
                    args=(str(root), dt.date(2026, 9, 22), start, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(5)

            self.assertTrue(all(process.exitcode == 0 for process in processes))
            self.assertCountEqual(
                [results.get(timeout=1), results.get(timeout=1)],
                ["daily-2026-09-22", None],
            )
            self.assertEqual(
                len(list((root / "running").glob("daily-2026-09-22.json"))), 1
            )

    def test_claim_preserves_sequential_execution_while_task_is_running(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            for day in (21, 22):
                daily.enqueue(root, prompt, dt.date(2026, 9, day), None)
            daily.claim_next(root, dt.date(2026, 9, 22), 3)

            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)

            self.assertIsNone(record)
            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())

    def test_reconcile_moves_terminal_captain_runs_and_records_commit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-finished"})
            runtime = repo / ".captain/runtime/fm-finished"
            runtime.mkdir(parents=True)
            (runtime / "status").write_text("review\n")
            commit = "a" * 40
            (runtime / "accepted-commit").write_text(commit + "\n")

            reconciled = daily.reconcile_running(root, repo)

            self.assertEqual(len(reconciled), 1)
            self.assertEqual(reconciled[0]["status"], "COMPLETED")
            self.assertEqual(reconciled[0]["exit_code"], 0)
            self.assertEqual(reconciled[0]["commit"], commit)
            self.assertEqual(reconciled[0]["captain_status"], "review")
            self.assertTrue((root / "completed/daily-2026-09-22.json").exists())

    def test_reconcile_moves_failed_run_without_guessing_active_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-active"})
            runtime = repo / ".captain/runtime/fm-active"
            runtime.mkdir(parents=True)
            (runtime / "status").write_text("gnhf-codex-account2\n")

            self.assertEqual(daily.reconcile_running(root, repo), [])
            self.assertTrue((root / "running/daily-2026-09-22.json").exists())

            (runtime / "status").write_text("failed\n")
            reconciled = daily.reconcile_running(root, repo)

            self.assertEqual(reconciled[0]["status"], "FAILED")
            self.assertEqual(reconciled[0]["exit_code"], 1)
            self.assertTrue((root / "failed/daily-2026-09-22.json").exists())

    def test_reconcile_rejects_running_task_duplicated_in_terminal_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-failed"})
            running = root / "running/daily-2026-09-22.json"
            completed = json.loads(running.read_text())
            completed["status"] = "COMPLETED"
            daily.atomic_create(
                root / "completed/daily-2026-09-22.json",
                (json.dumps(completed) + "\n").encode(),
            )
            runtime = repo / ".captain/runtime/fm-failed"
            runtime.mkdir(parents=True)
            (runtime / "status").write_text("failed\n")

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.reconcile_running(root, repo)

            self.assertTrue(running.exists())
            self.assertFalse((root / "failed/daily-2026-09-22.json").exists())

    def test_reconcile_ignores_unreadable_optional_commit_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-finished"})
            runtime = repo / ".captain/runtime/fm-finished"
            runtime.mkdir(parents=True)
            (runtime / "status").write_text("review\n")
            (runtime / "integrated-commit").write_bytes(b"\xff")

            reconciled = daily.reconcile_running(root, repo)

            self.assertEqual(len(reconciled), 1)
            self.assertEqual(reconciled[0]["status"], "COMPLETED")
            self.assertIsNone(reconciled[0]["commit"])
            self.assertTrue((root / "completed/daily-2026-09-22.json").exists())

    def test_reconcile_preserves_running_task_with_unreadable_captain_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-active"})
            runtime = repo / ".captain/runtime/fm-active"
            runtime.mkdir(parents=True)
            (runtime / "status").write_bytes(b"\xff")

            reconciled = daily.reconcile_running(root, repo)

            self.assertEqual(reconciled, [])
            self.assertTrue((root / "running/daily-2026-09-22.json").exists())
            self.assertFalse((root / "completed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "failed/daily-2026-09-22.json").exists())

    def test_reconcile_rejects_status_through_symlinked_run_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            runtime = repo / ".captain/runtime"
            runtime.mkdir(parents=True)
            outside = base / "outside-run"
            outside.mkdir()
            (outside / "status").write_text("review\n")
            (runtime / "fm-symlinked").symlink_to(outside, target_is_directory=True)
            root = base / "tasks"
            prompt = base / "prompt.md"
            prompt.write_text("work")
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(root, record["task_id"], {"run_id": "fm-symlinked"})

            reconciled = daily.reconcile_running(root, repo)

            self.assertEqual(reconciled, [])
            self.assertTrue((root / "running/daily-2026-09-22.json").exists())
            self.assertFalse((root / "completed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "failed/daily-2026-09-22.json").exists())

    def test_run_launches_first_mate_and_records_available_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            runtime = repo / ".captain/runtime/fm-test"
            runtime.mkdir(parents=True)
            (runtime / "branch").write_text("agent/fm-test\n")
            (runtime / "worktree").write_text("/safe/worktree\n")
            prompt = base / "prompt.md"
            prompt.write_text("literal $(touch /tmp/daily-gnhf-unsafe)")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(
                base,
                'for arg in "$@"; do previous="$current"; current="$arg"; '
                '[ "$previous" = "--run-id-file" ] && printf "fm-test\\n" > "$current"; '
                "done\nexit 0\n",
            )

            record = daily.launch_next(
                root, dt.date(2026, 9, 22), 3, repo, launcher, "safe check"
            )

            self.assertEqual(record["status"], "RUNNING")
            self.assertEqual(record["run_id"], "fm-test")
            self.assertEqual(record["branch"], "agent/fm-test")
            self.assertEqual(record["worktree"], "/safe/worktree")
            self.assertFalse(Path("/tmp/daily-gnhf-unsafe").exists())

    def test_running_metadata_update_rejects_duplicate_terminal_task(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            completed = dict(record, status="COMPLETED")
            daily.atomic_create(
                root / "completed/daily-2026-09-22.json",
                (json.dumps(completed) + "\n").encode(),
            )

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.update_running(root, record["task_id"], {"run_id": "fm-test"})

            persisted = json.loads(
                (root / "running/daily-2026-09-22.json").read_text()
            )
            self.assertIsNone(persisted["run_id"])

    def test_run_preserves_run_link_when_optional_runtime_metadata_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            runtime = repo / ".captain/runtime/fm-linked"
            runtime.mkdir(parents=True)
            (runtime / "branch").write_bytes(b"\xff")
            (runtime / "worktree").write_text("/safe/worktree\n")
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(
                base,
                'for arg in "$@"; do previous="$current"; current="$arg"; '
                '[ "$previous" = "--run-id-file" ] && printf "fm-linked\\n" > "$current"; '
                "done\nexit 0\n",
            )

            record = daily.launch_next(
                root, dt.date(2026, 9, 22), 3, repo, launcher, "safe check"
            )

            self.assertEqual(record["status"], "RUNNING")
            self.assertEqual(record["run_id"], "fm-linked")
            self.assertIsNone(record["branch"])
            self.assertEqual(record["worktree"], "/safe/worktree")
            persisted = json.loads(
                (root / "running/daily-2026-09-22.json").read_text()
            )
            self.assertEqual(persisted["run_id"], "fm-linked")

    def test_run_ignores_symlinked_runtime_metadata_without_persisting_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            runtime = repo / ".captain/runtime/fm-linked"
            runtime.mkdir(parents=True)
            secret = base / "secret.txt"
            secret.write_text("do-not-persist\n")
            (runtime / "branch").symlink_to(secret)
            (runtime / "worktree").write_text("/safe/worktree\n")
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(
                base,
                'for arg in "$@"; do previous="$current"; current="$arg"; '
                '[ "$previous" = "--run-id-file" ] && printf "fm-linked\\n" > "$current"; '
                "done\nexit 0\n",
            )

            record = daily.launch_next(
                root, dt.date(2026, 9, 22), 3, repo, launcher, "safe check"
            )

            self.assertEqual(record["run_id"], "fm-linked")
            self.assertIsNone(record["branch"])
            self.assertEqual(record["worktree"], "/safe/worktree")
            persisted = (root / "running/daily-2026-09-22.json").read_text()
            self.assertNotIn("do-not-persist", persisted)

    def test_launcher_failure_preserves_task_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(base, "exit 19\n")

            record = daily.launch_next(
                root, dt.date(2026, 9, 22), 3, base, launcher, "safe check"
            )

            self.assertEqual(record["status"], "FAILED")
            self.assertEqual(record["exit_code"], 19)
            self.assertTrue((root / "failed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_prompt_failure_after_claim_preserves_task_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(base, "exit 0\n")
            original_validate_prompt = daily.validate_prompt
            validations = 0

            def fail_second_validation(task_root, record):
                nonlocal validations
                validations += 1
                if validations == 2:
                    raise daily.TaskError("prompt changed after claim")
                return original_validate_prompt(task_root, record)

            with mock.patch.object(
                daily, "validate_prompt", side_effect=fail_second_validation
            ):
                record = daily.launch_next(
                    root, dt.date(2026, 9, 22), 3, base, launcher, "safe check"
                )

            self.assertEqual(record["status"], "FAILED")
            self.assertEqual(record["exit_code"], 1)
            self.assertEqual(record["attempts"], 1)
            self.assertTrue((root / "failed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_launcher_failure_rejects_task_duplicated_in_terminal_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            running = root / "running/daily-2026-09-22.json"
            completed = dict(record, status="COMPLETED")
            daily.atomic_create(
                root / "completed/daily-2026-09-22.json",
                (json.dumps(completed) + "\n").encode(),
            )

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.fail_launch(root, record["task_id"], 19)

            self.assertTrue(running.exists())
            self.assertFalse((root / "failed/daily-2026-09-22.json").exists())

    def test_invalid_run_id_output_preserves_task_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(
                base,
                'for arg in "$@"; do previous="$current"; current="$arg"; '
                '[ "$previous" = "--run-id-file" ] && printf "\\377" > "$current"; '
                "done\nexit 0\n",
            )

            record = daily.launch_next(
                root, dt.date(2026, 9, 22), 3, base, launcher, "safe check"
            )

            self.assertEqual(record["status"], "FAILED")
            self.assertEqual(record["exit_code"], 1)
            self.assertTrue((root / "failed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "running/daily-2026-09-22.json").exists())

    def test_catch_up_launches_only_oldest_missed_task_with_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = base / "repo"
            runtime = repo / ".captain/runtime/fm-catch-up"
            runtime.mkdir(parents=True)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            for day in (22, 20, 21):
                daily.enqueue(root, prompt, dt.date(2026, 9, day), None)
            launcher = self.make_launcher(
                base,
                'for arg in "$@"; do previous="$current"; current="$arg"; '
                '[ "$previous" = "--run-id-file" ] && printf "fm-catch-up\\n" > "$current"; '
                "done\nexit 0\n",
            )

            records = daily.catch_up(
                root,
                dt.date(2026, 9, 22),
                3,
                1,
                repo,
                launcher,
                "safe check",
            )

            self.assertEqual(
                [record["task_id"] for record in records], ["daily-2026-09-20"]
            )
            self.assertTrue((root / "pending/daily-2026-09-21.json").exists())
            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())
            self.assertTrue((root / "running/daily-2026-09-20.json").exists())

    def test_catch_up_never_launches_todays_task(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            launcher = self.make_launcher(base, "exit 99\n")

            records = daily.catch_up(
                root,
                dt.date(2026, 9, 22),
                3,
                1,
                base / "repo",
                launcher,
                "safe check",
            )

            self.assertEqual(records, [])
            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())

    def test_retry_returns_failed_task_to_pending_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.update_running(
                root,
                record["task_id"],
                {
                    "run_id": "fm-failed",
                    "branch": "gnhf/failed-attempt",
                    "commit": "a" * 40,
                    "worktree": "/tmp/failed-worktree",
                    "evaluation": "FAIL",
                    "captain_status": "failed",
                    "launcher_exit_code": 0,
                },
            )
            daily.fail_launch(root, record["task_id"], 19)

            retried = daily.retry_failed(root, record["task_id"], 3)

            self.assertEqual(retried["status"], "PENDING")
            self.assertEqual(retried["attempts"], 1)
            self.assertIsNone(retried["run_id"])
            self.assertEqual(len(retried["failures"]), 1)
            self.assertEqual(retried["failures"][0]["attempt"], 1)
            self.assertIsNotNone(retried["failures"][0]["finished_at"])
            self.assertEqual(retried["failures"][0]["exit_code"], 19)
            self.assertEqual(retried["failures"][0]["run_id"], "fm-failed")
            self.assertEqual(
                retried["failures"][0]["branch"], "gnhf/failed-attempt"
            )
            self.assertEqual(retried["failures"][0]["commit"], "a" * 40)
            self.assertEqual(
                retried["failures"][0]["worktree"], "/tmp/failed-worktree"
            )
            self.assertEqual(retried["failures"][0]["evaluation"], "FAIL")
            self.assertEqual(retried["failures"][0]["captain_status"], "failed")
            self.assertEqual(retried["failures"][0]["launcher_exit_code"], 0)
            self.assertIsNone(retried["branch"])
            self.assertIsNone(retried["commit"])
            self.assertIsNone(retried["worktree"])
            self.assertIsNone(retried["evaluation"])
            self.assertNotIn("captain_status", retried)
            self.assertNotIn("launcher_exit_code", retried)
            self.assertTrue((root / "pending/daily-2026-09-22.json").exists())
            self.assertFalse((root / "failed/daily-2026-09-22.json").exists())

    def test_retry_rejects_task_at_max_attempts_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 1)
            daily.fail_launch(root, record["task_id"], 1)

            with self.assertRaisesRegex(daily.TaskError, "reached max attempts"):
                daily.retry_failed(root, record["task_id"], 1)

            self.assertTrue((root / "failed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "pending/daily-2026-09-22.json").exists())

    def test_retry_rejects_failed_task_duplicated_in_completed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            failed = daily.fail_launch(root, record["task_id"], 1)
            completed = dict(failed, status="COMPLETED")
            daily.atomic_create(
                root / "completed/daily-2026-09-22.json",
                (json.dumps(completed) + "\n").encode(),
            )

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.retry_failed(root, record["task_id"], 3)

            self.assertTrue((root / "failed/daily-2026-09-22.json").exists())
            self.assertTrue((root / "completed/daily-2026-09-22.json").exists())
            self.assertFalse((root / "pending/daily-2026-09-22.json").exists())

    def test_history_returns_recent_terminal_tasks_only_with_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("private prompt contents")
            root = base / "tasks"
            completions = (
                (20, "2026-09-20T20:00:00+00:00"),
                (21, "2026-09-21T20:00:00+00:00"),
            )
            for day, finished_at in completions:
                daily.enqueue(root, prompt, dt.date(2026, 9, day), None)
                record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
                failed = daily.fail_launch(root, record["task_id"], 1)
                failed["finished_at"] = finished_at
                daily.atomic_replace(
                    root / "failed" / f'{record["task_id"]}.json',
                    (json.dumps(failed) + "\n").encode(),
                )
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)

            history = daily.task_history(root, limit=1)

            self.assertEqual(
                [item["task_id"] for item in history], ["daily-2026-09-21"]
            )
            self.assertNotIn("private prompt contents", json.dumps(history))

    def test_history_orders_timezone_offsets_by_actual_completion_time(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            completions = (
                (20, "2026-09-22T02:00:00+02:00"),
                (21, "2026-09-22T01:00:00+00:00"),
            )
            for day, finished_at in completions:
                daily.enqueue(root, prompt, dt.date(2026, 9, day), None)
                record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
                failed = daily.fail_launch(root, record["task_id"], 1)
                failed["finished_at"] = finished_at
                daily.atomic_replace(
                    root / "failed" / f'{record["task_id"]}.json',
                    (json.dumps(failed) + "\n").encode(),
                )

            history = daily.task_history(root)

            self.assertEqual(
                [item["task_id"] for item in history],
                ["daily-2026-09-21", "daily-2026-09-20"],
            )

    def test_history_rejects_duplicate_state_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            failed = daily.fail_launch(root, record["task_id"], 1)
            duplicate = dict(failed, status="COMPLETED")
            daily.atomic_create(
                root / "completed/daily-2026-09-22.json",
                (json.dumps(duplicate) + "\n").encode(),
            )

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.task_history(root)

    def test_history_waits_for_atomic_queue_mutation_to_finish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            record = daily.claim_next(root, dt.date(2026, 9, 22), 3)
            daily.fail_launch(root, record["task_id"], 1)
            metadata_path = root / "failed/daily-2026-09-22.json"
            original = metadata_path.read_text()
            context = multiprocessing.get_context("fork")
            started = context.Event()
            results = context.Queue()
            process = context.Process(
                target=history_in_process, args=(str(root), started, results)
            )

            with daily.queue_lock(root):
                metadata = json.loads(original)
                metadata["status"] = "COMPLETED"
                metadata_path.write_text(json.dumps(metadata))
                process.start()
                self.assertTrue(started.wait(1))
                process.join(0.2)
                self.assertTrue(process.is_alive())
                metadata_path.write_text(original)

            process.join(5)
            self.assertEqual(process.exitcode, 0)
            history = results.get(timeout=1)
            self.assertEqual(history[0]["status"], "FAILED")

    def test_show_returns_metadata_without_prompt_contents(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("do not display this prompt")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), "Visible title")

            record = daily.show_task(root, "daily-2026-09-22")

            self.assertEqual(record["status"], "PENDING")
            self.assertEqual(record["title"], "Visible title")
            self.assertNotIn("do not display this prompt", json.dumps(record))

    def test_show_rejects_duplicate_state_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            pending = root / "pending/daily-2026-09-22.json"
            duplicate = json.loads(pending.read_text())
            duplicate["status"] = "FAILED"
            daily.atomic_create(
                root / "failed/daily-2026-09-22.json",
                (json.dumps(duplicate) + "\n").encode(),
            )

            with self.assertRaisesRegex(daily.TaskError, "multiple states"):
                daily.show_task(root, "daily-2026-09-22")

    def test_show_waits_for_atomic_queue_mutation_to_finish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            prompt = base / "prompt.md"
            prompt.write_text("work")
            root = base / "tasks"
            daily.enqueue(root, prompt, dt.date(2026, 9, 22), None)
            metadata_path = root / "pending/daily-2026-09-22.json"
            original = metadata_path.read_text()
            context = multiprocessing.get_context("fork")
            started = context.Event()
            results = context.Queue()
            process = context.Process(
                target=show_in_process, args=(str(root), started, results)
            )

            with daily.queue_lock(root):
                metadata = json.loads(original)
                metadata["status"] = "RUNNING"
                metadata_path.write_text(json.dumps(metadata))
                process.start()
                self.assertTrue(started.wait(1))
                process.join(0.2)
                self.assertTrue(process.is_alive())
                metadata_path.write_text(original)

            process.join(5)
            self.assertEqual(process.exitcode, 0)
            shown = results.get(timeout=1)
            self.assertEqual(shown["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
