import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "agent_memory.py"
SPEC = importlib.util.spec_from_file_location("agent_memory", MODULE_PATH)
assert SPEC and SPEC.loader
memory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory)


def create_private_store_directories(store):
    store.directory.mkdir(mode=0o700)
    store.records_directory.mkdir(mode=0o700)


class AgentMemoryTest(unittest.TestCase):
    def test_foreign_owned_store_objects_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)
            record = store.add(
                memory_type="DISCOVERY", summary="Validated memory ownership"
            )

            with mock.patch.object(memory.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaisesRegex(
                    memory.MemoryError, "unsafe memory directory"
                ):
                    store.list()
                with self.assertRaisesRegex(
                    memory.MemoryError, "unsafe memory directory"
                ):
                    store.add(
                        memory_type="DISCOVERY",
                        summary="Reject a foreign owned store",
                    )

            record_path = store.records_directory / f"{record['id']}.json"
            original_check = memory._owned_by_current_user
            with mock.patch.object(
                memory,
                "_owned_by_current_user",
                side_effect=lambda metadata: (
                    False
                    if metadata.st_ino == record_path.stat().st_ino
                    else original_check(metadata)
                ),
            ):
                self.assertEqual(store.list(), ([], 1))

            lock_inode = store.lock_path.stat().st_ino
            with mock.patch.object(
                memory,
                "_owned_by_current_user",
                side_effect=lambda metadata: (
                    False
                    if metadata.st_ino == lock_inode
                    else original_check(metadata)
                ),
            ):
                with self.assertRaisesRegex(memory.MemoryError, "unsafe memory lock"):
                    store.add(
                        memory_type="DISCOVERY",
                        summary="Reject a foreign owned lock",
                    )

    def test_write_repairs_lock_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            store.directory.mkdir(parents=True)
            store.lock_path.write_text("", encoding="utf-8")
            store.lock_path.chmod(0o666)

            store.add(memory_type="DISCOVERY", summary="Validated lock behavior")

            self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o600)

    def test_permissive_record_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)
            record = store.add(
                memory_type="DISCOVERY", summary="Validated private record permissions"
            )
            record_path = store.records_directory / f"{record['id']}.json"
            record_path.chmod(0o644)

            records, malformed = store.list()

            self.assertEqual(records, [])
            self.assertEqual(malformed, 1)

    def test_permissive_memory_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)
            store.add(
                memory_type="DISCOVERY",
                summary="Validated private directory permissions",
            )

            for path in (store.directory, store.records_directory):
                with self.subTest(path=path):
                    path.chmod(0o755)
                    with self.assertRaisesRegex(
                        memory.MemoryError, "unsafe memory directory"
                    ):
                        store.list()
                    path.chmod(0o700)

    def test_control_characters_are_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)
            for summary in (
                "terminal\x1b[31mspoof",
                "embedded\x00value",
                "bidirectional\u202erecord",
            ):
                with self.subTest(summary=repr(summary)):
                    with self.assertRaisesRegex(
                        memory.MemoryError, "unsafe control characters"
                    ):
                        store.add(memory_type="DISCOVERY", summary=summary)

            records, malformed = store.list()
            self.assertEqual(records, [])
            self.assertEqual(malformed, 0)

    def test_invalid_unicode_is_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)

            with self.assertRaisesRegex(memory.MemoryError, "valid UTF-8 text"):
                store.add(
                    memory_type="DISCOVERY",
                    summary="invalid lone surrogate \ud800",
                )

            self.assertEqual(store.list(), ([], 0))

    def test_non_list_tags_are_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)

            with self.assertRaisesRegex(memory.MemoryError, "tags must be a list"):
                store.add(
                    memory_type="DISCOVERY",
                    summary="Validated repository detail",
                    tags="testing",
                )

            self.assertEqual(store.list(), ([], 0))

    def test_add_preserves_structure_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            record = memory.MemoryStore(temporary_directory).add(
                memory_type="command", summary="Run tests with python -m unittest",
                tags=["Testing", "python"], confidence=0.9, run_id="run-1",
                commit="abc123", branch="feature/memory", created_at="2026-01-02T03:04:05Z",
            )
            self.assertEqual(record["type"], "COMMAND")
            self.assertEqual(record["tags"], ["python", "testing"])
            self.assertEqual(record["status"], "active")
            self.assertEqual(record["provenance"], {"source_run": "run-1", "source_commit": "abc123", "branch": "feature/memory"})
            self.assertEqual(record["created_at"], "2026-01-02T03:04:05Z")

    def test_normalized_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            first = store.add(memory_type="COMMAND", summary="Run  Tests", tags=["Python", "testing"])
            with self.assertRaisesRegex(memory.MemoryError, "duplicate memory"):
                store.add(memory_type="command", summary=" run tests ", tags=["testing", "python"])
            self.assertEqual(store.list()[0][0]["id"], first["id"])

    def test_lifecycle_preserves_historical_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            invalid = store.add(memory_type="FAILURE", summary="Old failure")
            old = store.add(memory_type="DECISION", summary="Use old command")
            new = store.add(memory_type="DECISION", summary="Use new command")
            store.invalidate(invalid["id"])
            store.supersede(old["id"], new["id"])
            records, malformed = store.list()
            by_id = {item["id"]: item for item in records}
            self.assertEqual(malformed, 0)
            self.assertEqual(by_id[invalid["id"]]["status"], "invalid")
            self.assertEqual(by_id[old["id"]]["status"], "superseded")
            self.assertEqual(by_id[old["id"]]["superseded_by"], new["id"])
            self.assertEqual(store.list(include_inactive=False)[0], [new])

    def test_malformed_records_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            create_private_store_directories(store)
            (store.records_directory / "mem-0000000000000000.json").write_text("not json")
            self.assertEqual(store.list(), ([], 1))

    def test_duplicate_json_members_are_malformed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            record = store.add(
                memory_type="DISCOVERY", summary="Validated repository detail"
            )
            path = store.records_directory / f"{record['id']}.json"
            payload = path.read_text(encoding="utf-8").rstrip()
            path.write_text(
                payload[:-1] + ',"status":"active"}\n', encoding="utf-8"
            )

            self.assertEqual(store.list(), ([], 1))

    def test_excessively_nested_json_is_malformed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            create_private_store_directories(store)
            path = store.records_directory / "mem-0000000000000000.json"
            path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")

            self.assertEqual(store.list(), ([], 1))

    def test_noncanonical_json_filenames_are_counted_as_malformed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            create_private_store_directories(store)
            (store.records_directory / "manually-added.json").write_text("{}")
            (store.records_directory / ".mem-write-in-progress.tmp").write_text("partial")

            self.assertEqual(store.list(), ([], 1))
            self.assertEqual(store.stats()["malformed"], 1)

    def test_unsafe_record_file_types_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            create_private_store_directories(store)
            dangling = store.records_directory / "mem-0000000000000000.json"
            dangling.symlink_to(store.records_directory / "missing.json")
            fifo = store.records_directory / "mem-0000000000000001.json"
            os.mkfifo(fifo)

            self.assertEqual(store.list(), ([], 2))

    def test_unsafe_memory_directories_are_rejected_on_read(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            external = root / "external"
            (external / "records").mkdir(parents=True)

            store = memory.MemoryStore(root / "dangling-store")
            store.root.mkdir()
            store.directory.symlink_to(root / "missing", target_is_directory=True)
            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory directory"):
                store.list()

            store = memory.MemoryStore(root / "symlinked-store")
            store.root.mkdir()
            store.directory.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory directory"):
                store.list()

            store = memory.MemoryStore(root / "symlinked-records")
            store.root.mkdir()
            store.directory.mkdir()
            store.records_directory.symlink_to(external / "records", target_is_directory=True)
            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory directory"):
                store.search("testing")

    def test_missing_records_directory_is_rejected_after_store_is_opened(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            store.directory.mkdir()

            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory directory"):
                store.list()

            missing_store = memory.MemoryStore(
                Path(temporary_directory) / "not-created"
            )
            self.assertEqual(missing_store.list(), ([], 0))

    def test_record_enumeration_stays_anchored_during_directory_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = memory.MemoryStore(root)
            record = store.add(
                memory_type="DISCOVERY", summary="Validated repository detail"
            )
            original_records = store.directory / "original-records"
            redirected_records = root / "redirected-records"
            redirected_records.mkdir()
            real_listdir = os.listdir

            def replace_directory(directory):
                names = real_listdir(directory)
                store.records_directory.rename(original_records)
                store.records_directory.symlink_to(
                    redirected_records, target_is_directory=True
                )
                return names

            with mock.patch.object(os, "listdir", side_effect=replace_directory):
                records, malformed = store.list()

            self.assertEqual(records, [record])
            self.assertEqual(malformed, 0)

    def test_record_write_stays_anchored_during_directory_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = memory.MemoryStore(root)
            store.add(memory_type="DISCOVERY", summary="Initial validated detail")
            original_records = store.directory / "original-records"
            redirected_records = root / "redirected-records"
            redirected_records.mkdir()
            real_replace = os.replace

            def replace_directory(source, destination, **kwargs):
                store.records_directory.rename(original_records)
                store.records_directory.symlink_to(
                    redirected_records, target_is_directory=True
                )
                return real_replace(source, destination, **kwargs)

            with mock.patch.object(os, "replace", side_effect=replace_directory):
                record = store.add(
                    memory_type="DISCOVERY", summary="New validated detail"
                )

            self.assertTrue((original_records / f"{record['id']}.json").is_file())
            self.assertEqual(list(redirected_records.iterdir()), [])

    def test_unsafe_lock_file_types_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            store.directory.mkdir()
            target = Path(temporary_directory) / "unrelated"
            target.write_text("must remain unchanged", encoding="utf-8")
            store.lock_path.symlink_to(target)

            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory lock"):
                store.add(memory_type="DISCOVERY", summary="Validated repository detail")
            self.assertEqual(target.read_text(encoding="utf-8"), "must remain unchanged")

            store.lock_path.unlink()
            os.mkfifo(store.lock_path)
            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory lock"):
                store.add(memory_type="DISCOVERY", summary="Validated repository detail")

            store.lock_path.unlink()
            os.link(target, store.lock_path)
            with self.assertRaisesRegex(memory.MemoryError, "unsafe memory lock"):
                store.add(memory_type="DISCOVERY", summary="Validated repository detail")
            self.assertEqual(target.read_text(encoding="utf-8"), "must remain unchanged")

    def test_valid_json_with_invalid_shape_is_skipped_by_search_and_stats(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            create_private_store_directories(store)
            path = store.records_directory / "mem-0000000000000000.json"
            path.write_text(json.dumps({"id": path.stem, "status": "active"}))

            self.assertEqual(store.search("testing"), ([], 1))
            self.assertEqual(store.stats()["malformed"], 1)

    def test_tampered_record_content_or_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            record = store.add(memory_type="DISCOVERY", summary="Testing is deterministic")
            path = store.records_directory / f"{record['id']}.json"

            tampered = dict(record)
            tampered["summary"] = "Testing is nondeterministic"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertEqual(store.list(), ([], 1))

            tampered["summary"] = record["summary"]
            tampered["fingerprint"] = "0" * 64
            path.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertEqual(store.list(), ([], 1))

    def test_noncanonical_record_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            record = store.add(
                memory_type="DISCOVERY", summary="Testing is deterministic",
                tags=["testing"], run_id="run-1",
            )
            variants = (
                {**record, "summary": "Testing  is deterministic"},
                {**record, "scope": " repository "},
                {**record, "tags": ["Testing"]},
                {**record, "tags": ["testing", "testing"]},
                {**record, "provenance": {"source_run": " run-1 "}},
            )
            for index, variant in enumerate(variants):
                with self.subTest(index=index):
                    variant["fingerprint"] = memory._fingerprint(
                        variant["type"], variant["scope"], variant["tags"],
                        variant["summary"],
                    )
                    variant["id"] = memory._memory_id(variant["fingerprint"])
                    path = store.records_directory / f"{variant['id']}.json"
                    for existing in store.records_directory.glob("mem-*.json"):
                        existing.unlink()
                    path.write_text(json.dumps(variant), encoding="utf-8")
                    self.assertEqual(store.list(), ([], 1))

    def test_malformed_lifecycle_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            record = store.add(memory_type="DECISION", summary="Use the validated command")
            path = store.records_directory / f"{record['id']}.json"

            malformed_records = (
                {**record, "status": "superseded"},
                {**record, "status": "superseded", "superseded_by": "not-an-id"},
                {**record, "status": "superseded", "superseded_by": record["id"]},
                {**record, "superseded_by": "mem-0000000000000000"},
                {**record, "unexpected": "metadata"},
            )
            for malformed in malformed_records:
                with self.subTest(record=malformed):
                    path.write_text(json.dumps(malformed), encoding="utf-8")
                    self.assertEqual(store.list(), ([], 1))

    def test_dangling_supersession_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            old = store.add(memory_type="DECISION", summary="Use the old command")
            replacement = store.add(
                memory_type="DECISION", summary="Use the replacement command"
            )
            store.supersede(old["id"], replacement["id"])

            replacement_path = store.records_directory / f"{replacement['id']}.json"
            replacement_path.unlink()

            self.assertEqual(store.list(), ([], 1))
            self.assertEqual(store.list(include_inactive=False), ([], 1))

    def test_supersession_link_to_invalid_historical_record_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            old = store.add(memory_type="DECISION", summary="Use the old command")
            replacement = store.add(
                memory_type="DECISION", summary="Use the replacement command"
            )
            store.supersede(old["id"], replacement["id"])
            store.invalidate(replacement["id"])

            records, malformed = store.list()
            self.assertEqual(malformed, 0)
            self.assertEqual(
                {record["id"] for record in records},
                {old["id"], replacement["id"]},
            )
            self.assertEqual(store.list(include_inactive=False), ([], 0))

    def test_cyclic_supersession_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            first = store.add(memory_type="DECISION", summary="Use command one")
            second = store.add(memory_type="DECISION", summary="Use command two")
            for record, replacement in ((first, second), (second, first)):
                record["status"] = "superseded"
                record["superseded_by"] = replacement["id"]
                path = store.records_directory / f"{record['id']}.json"
                path.write_text(json.dumps(record), encoding="utf-8")

            self.assertEqual(store.list(), ([], 2))

    def test_secret_is_rejected_without_echoing_value(self):
        secret = "sk-live-0123456789abcdefghijkl"
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            with self.assertRaises(memory.MemoryError) as raised:
                store.add(memory_type="DISCOVERY", summary=f"API key is {secret}")
            self.assertNotIn(secret, str(raised.exception))
            self.assertEqual(store.list(), ([], 0))

    def test_common_credential_assignments_and_authorization_tokens_are_rejected(self):
        secret_values = (
            "-----BEGIN PGP PRIVATE KEY BLOCK----- opaque",
            "AWS_SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwxyz1234567890",
            "PASSWORD=1234",
            "DEPLOY_TOKEN=abcdefghijklmnopqrstuvwxyz123456",
            "SERVICE_SECRET=abcdefghijklmnopqrstuvwxyz123456",
            "DB_CREDENTIAL=abcdefghijklmnopqrstuvwxyz123456",
            "DATABASE_URL=postgresql://memory-user:credential@db.invalid/project",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            "Authorization: Basic bWVtb3J5LXVzZXI6b3BhcXVlLXBhc3N3b3Jk",
            "Basic bWVtb3J5LXVzZXI6b3BhcXVlLXBhc3N3b3Jk",
            "Authorization: Basic YTpiYg==",
            "Authorization: ApiKey opaque-integration-credential",
            "Authorization: Token opaque-integration-credential",
            (
                'Authorization: Digest username="memory-user", realm="private", '
                'nonce="opaque-nonce", response="opaque-response"'
            ),
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtZW1vcnkifQ.signature123",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            for index, secret_value in enumerate(secret_values):
                with self.subTest(secret_value=secret_value.split("=", 1)[0]):
                    with self.assertRaises(memory.MemoryError) as raised:
                        store.add(
                            memory_type="DISCOVERY",
                            summary=f"Credential fixture {index}: {secret_value}",
                        )
                    self.assertNotIn(secret_value, str(raised.exception))
            self.assertEqual(store.list(), ([], 0))

    def test_bare_credential_and_signed_urls_are_rejected_without_leaking_value(self):
        credential_urls = (
            "postgresql://memory-user:opaque-password@db.invalid/project",
            "https://memory-user:x@example.invalid/private",
            "https://blob.invalid/file?sv=2026-01-01&sig=" + "a" * 32,
            "https://bucket.invalid/object?X-Amz-Signature=" + "b" * 64,
            "https://storage.invalid/object?X-Goog-Signature=" + "c" * 64,
        )
        for credential_url in credential_urls:
            with self.subTest(credential_url=credential_url):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    result = subprocess.run(
                        [
                            str(ROOT / "scripts" / "agent-memory"),
                            "--root", temporary_directory,
                            "add", "--type", "DISCOVERY",
                            "--summary", f"Database endpoint is {credential_url}",
                        ],
                        text=True, capture_output=True,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(credential_url, result.stdout)
                    self.assertNotIn(credential_url, result.stderr)
                    self.assertEqual(
                        memory.MemoryStore(temporary_directory).list(), ([], 0)
                    )

    def test_bare_provider_tokens_are_rejected_without_leaking_values(self):
        provider_tokens = (
            "sk-" + "o" * 32,
            "sk-ant-api03-" + "f" * 32,
            "AIza" + "G" * 35,
            "ya29." + "h" * 32,
            "ghu_" + "c" * 36,
            "ghs_" + "d" * 36,
            "ghr_" + "e" * 36,
            "glpat-" + "a" * 24,
            "xoxb-" + "1" * 12 + "-" + "a" * 24,
            "npm_" + "b" * 36,
            "hf_" + "i" * 36,
            "pypi-AgEI" + "j" * 32,
            "ASIA" + "A1B2C3D4E5F6G7H8",
            "sk_live_" + "f" * 24,
            "rk_test_" + "g" * 24,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            for provider_token in provider_tokens:
                with self.subTest(prefix=provider_token[:5]):
                    with self.assertRaises(memory.MemoryError) as raised:
                        store.add(
                            memory_type="DISCOVERY",
                            summary=f"Validated integration value {provider_token}",
                        )
                    self.assertNotIn(provider_token, str(raised.exception))
            self.assertEqual(store.list(), ([], 0))

    def test_cli_secret_rejection_does_not_leak_value(self):
        secret = "PASSWORD=1234"
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "agent-memory"), "--root", temporary_directory,
                    "add", "--type", "DISCOVERY", "--summary", f"Found {secret}",
                ],
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)
            self.assertEqual(memory.MemoryStore(temporary_directory).list(), ([], 0))

    def test_multiline_dotenv_content_is_rejected_without_leaking_values(self):
        dotenv = "PUBLIC_MODE=enabled\nINTERNAL_CREDENTIAL=opaque-value"
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            with self.assertRaises(memory.MemoryError) as raised:
                store.add(memory_type="DISCOVERY", summary=dotenv)

            self.assertNotIn("opaque-value", str(raised.exception))
            self.assertEqual(store.list(), ([], 0))

    def test_single_environment_assignment_command_remains_supported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            record = memory.MemoryStore(temporary_directory).add(
                memory_type="COMMAND",
                summary="Run AGENT_CONTEXT_BUDGET=1000 ./scripts/first-mate",
            )
            self.assertEqual(record["status"], "active")

    def test_benign_one_line_configuration_assignment_remains_supported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            record = memory.MemoryStore(temporary_directory).add(
                memory_type="COMMAND",
                summary="Run LOG_LEVEL=debug ./scripts/first-mate",
            )
            self.assertEqual(record["status"], "active")

    def test_concurrent_writes_are_not_corrupted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            def add(index):
                return store.add(memory_type="DISCOVERY", summary=f"Finding number {index}")
            with ThreadPoolExecutor(max_workers=8) as workers:
                records = list(workers.map(add, range(40)))
            stored, malformed = store.list()
            self.assertEqual(malformed, 0)
            self.assertEqual(len(stored), len(records))
            self.assertEqual({item["id"] for item in stored}, {item["id"] for item in records})

    def test_missing_store_lists_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertEqual(memory.MemoryStore(temporary_directory).list(), ([], 0))

    def test_search_ranks_tags_above_summary_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            summary_match = store.add(
                memory_type="COMMAND", summary="Run testing with unittest", confidence=1.0,
            )
            tag_match = store.add(
                memory_type="DISCOVERY", summary="The suite is fast", tags=["testing"], confidence=0.5,
            )

            first, malformed = store.search("testing")
            second, _ = store.search("testing")

            self.assertEqual(malformed, 0)
            self.assertEqual(first, second)
            self.assertEqual([item["record"]["id"] for item in first], [tag_match["id"], summary_match["id"]])
            self.assertEqual(first[0]["relevance"]["tag_terms"], ["testing"])
            self.assertEqual(first[1]["relevance"]["summary_terms"], ["testing"])

    def test_search_matches_each_term_in_multiword_tags(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            record = store.add(
                memory_type="DISCOVERY",
                summary="The suite covers orchestration",
                tags=["agent evaluation"],
            )

            results, malformed = store.search("improve agent evaluation tests")

            self.assertEqual(malformed, 0)
            self.assertEqual([result["record"]["id"] for result in results], [record["id"]])
            self.assertEqual(
                results[0]["relevance"],
                {
                    "score": 16,
                    "summary_terms": [],
                    "tag_terms": ["agent", "evaluation"],
                    "scope_terms": [],
                    "type_terms": [],
                },
            )

    def test_search_matches_natural_language_terms_in_underscored_tags(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            record = store.add(
                memory_type="DISCOVERY",
                summary="The suite covers orchestration",
                tags=["agent_evaluation"],
            )

            results, malformed = store.search("improve agent evaluation tests")

            self.assertEqual(malformed, 0)
            self.assertEqual([result["record"]["id"] for result in results], [record["id"]])
            self.assertEqual(results[0]["relevance"]["tag_terms"], ["agent", "evaluation"])
            self.assertEqual(results[0]["relevance"]["score"], 16)

    def test_unicode_equivalent_memories_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            composed = store.add(
                memory_type="DISCOVERY",
                summary="Caf\u00e9 tests are deterministic",
            )

            with self.assertRaisesRegex(memory.MemoryError, "duplicate memory"):
                store.add(
                    memory_type="DISCOVERY",
                    summary="Cafe\u0301 tests are deterministic",
                )

            self.assertEqual(composed["summary"], "Caf\u00e9 tests are deterministic")
            self.assertEqual(len(store.list()[0]), 1)

    def test_search_uses_recency_as_a_deterministic_tie_breaker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            older = store.add(
                memory_type="DISCOVERY", summary="Testing alpha behavior",
                created_at="2025-01-01T00:00:00Z",
            )
            newer = store.add(
                memory_type="DISCOVERY", summary="Testing beta behavior",
                created_at="2026-01-01T01:00:00+01:00",
            )

            results, malformed = store.search("testing")

            self.assertEqual(malformed, 0)
            self.assertEqual([result["record"]["id"] for result in results], [newer["id"], older["id"]])
            self.assertEqual(newer["created_at"], "2026-01-01T00:00:00Z")

    def test_created_at_requires_a_timezone_aware_iso_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            for timestamp in ("yesterday", "2026-01-01T00:00:00"):
                with self.subTest(timestamp=timestamp):
                    with self.assertRaisesRegex(memory.MemoryError, "created_at"):
                        store.add(
                            memory_type="DISCOVERY", summary=f"Finding {timestamp}",
                            created_at=timestamp,
                        )

    def test_search_excludes_inactive_memories_by_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            invalid = store.add(memory_type="FAILURE", summary="Testing command failed")
            store.invalidate(invalid["id"])
            self.assertEqual(store.search("testing"), ([], 0))
            self.assertEqual(store.search("testing", include_inactive=True)[0][0]["record"]["status"], "invalid")

    def test_stats_include_lifecycle_types_and_malformed_count(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = memory.MemoryStore(temporary_directory)
            active = store.add(memory_type="COMMAND", summary="Run tests")
            invalid = store.add(memory_type="FAILURE", summary="Old test command")
            store.invalidate(invalid["id"])
            (store.records_directory / "mem-0000000000000000.json").write_text("not json")

            stats = store.stats()

            self.assertEqual(stats["total"], 2)
            self.assertEqual(stats["malformed"], 1)
            self.assertEqual(stats["by_status"]["active"], 1)
            self.assertEqual(stats["by_status"]["invalid"], 1)
            self.assertEqual(stats["by_type"][active["type"]], 1)

    def test_linked_worktree_resolves_to_shared_primary_store(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            worktree = root / "worktree"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
            (repository / "tracked").write_text("content", encoding="utf-8")
            subprocess.run(["git", "add", "tracked"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            subprocess.run(["git", "worktree", "add", "-q", "-b", "linked", str(worktree)], cwd=repository, check=True)

            self.assertEqual(memory.shared_repository_root(worktree), repository)

    def test_cli_json_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = subprocess.run(
                [str(ROOT / "scripts" / "agent-memory"), "--root", temporary_directory, "--json", "add", "--type", "COMMAND", "--tags", "testing", "--summary", "Run repository tests"],
                text=True, capture_output=True, check=True,
            )
            record = json.loads(result.stdout)
            self.assertEqual(record["type"], "COMMAND")
            listed = subprocess.run(
                [str(ROOT / "scripts" / "agent-memory"), "--root", temporary_directory, "--json", "list"],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(json.loads(listed.stdout)["records"], [record])

            searched = subprocess.run(
                [str(ROOT / "scripts" / "agent-memory"), "--root", temporary_directory, "--json", "search", "testing"],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(json.loads(searched.stdout)["results"][0]["record"], record)
            stats = subprocess.run(
                [str(ROOT / "scripts" / "agent-memory"), "--root", temporary_directory, "--json", "stats"],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(json.loads(stats.stdout)["total"], 1)


if __name__ == "__main__":
    unittest.main()
