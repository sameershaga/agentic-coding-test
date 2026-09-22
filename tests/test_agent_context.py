import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

memory_spec = importlib.util.spec_from_file_location("agent_memory", SCRIPTS / "agent_memory.py")
assert memory_spec and memory_spec.loader
memory = importlib.util.module_from_spec(memory_spec)
memory_spec.loader.exec_module(memory)

context_spec = importlib.util.spec_from_file_location("agent_context", SCRIPTS / "agent_context.py")
assert context_spec and context_spec.loader
context = importlib.util.module_from_spec(context_spec)
context_spec.loader.exec_module(context)


class AgentContextTest(unittest.TestCase):
    def test_context_is_ranked_reproducible_and_within_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)
            tagged = store.add(memory_type="COMMAND", summary="Run the suite", tags=["testing"])
            store.add(memory_type="DISCOVERY", summary="Testing is deterministic")
            store.add(memory_type="ARCHITECTURE", summary="Unrelated worker layout")

            first = context.build_context(store, "improve testing", 120)
            second = context.build_context(store, "improve testing", 120)

            self.assertEqual(first, second)
            self.assertLessEqual(first["estimated_tokens"], 120)
            self.assertIn(f"Approximate tokens: {first['estimated_tokens']}", first["packet"])
            self.assertEqual(first["included_ids"][0], tagged["id"])
            self.assertNotIn("Unrelated worker layout", first["packet"])

    def test_budget_excludes_records_that_do_not_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = memory.MemoryStore(directory)
            store.add(memory_type="DISCOVERY", summary="Testing " + "detail " * 50)
            result = context.build_context(store, "testing", 40)
            self.assertLessEqual(context.estimate_tokens(result["packet"]), 40)
            self.assertEqual(result["included_ids"], [])

    def test_empty_or_missing_store_and_tiny_budget_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            result = context.build_context(memory.MemoryStore(directory), "testing", 1)
            self.assertLessEqual(result["estimated_tokens"], 1)
            self.assertEqual(result["included_ids"], [])

    def test_cli_json_matches_packet_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [str(SCRIPTS / "agent-context"), "--root", directory, "--json",
                 "--task", "test evaluation", "--budget", "100"],
                check=True, capture_output=True, text=True,
            )
            value = json.loads(completed.stdout)
            self.assertEqual(value["estimated_tokens"], context.estimate_tokens(value["packet"]))
            self.assertLessEqual(value["estimated_tokens"], 100)

    def test_first_mate_passes_one_context_packet_to_every_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "control"
            scripts = control / "scripts"
            runtime = control / ".captain" / "runtime" / "run-1"
            worktree = root / "worktree"
            binaries = root / "bin"
            for path in (scripts, runtime, worktree, binaries):
                path.mkdir(parents=True)
            shutil.copy(SCRIPTS / "first-mate-runner", scripts)
            (scripts / "agent_telemetry.py").write_text("", encoding="utf-8")
            (scripts / "agent-context").write_text(
                "#!/bin/sh\nprintf '%s\\n' 'PROJECT CONTEXT' 'Budget: 73'\n",
                encoding="utf-8",
            )
            (scripts / "agent-context").chmod(0o755)
            capture = root / "prompts"
            for name in ("opencode", "codex"):
                executable = binaries / name
                executable.write_text(
                    '#!/bin/sh\nfor argument do last="$argument"; done\n'
                    'printf "%s\\0" "$last" >> "$PROMPT_CAPTURE"\nexit 1\n',
                    encoding="utf-8",
                )
                executable.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            (runtime / "task.txt").write_text("improve tests", encoding="utf-8")
            (runtime / "check.txt").write_text("true", encoding="utf-8")
            (runtime / "status").write_text("starting", encoding="utf-8")
            environment = os.environ.copy()
            environment["PATH"] = f"{binaries}:{environment['PATH']}"
            environment["PROMPT_CAPTURE"] = str(capture)
            environment["AGENT_CONTEXT_BUDGET"] = "73"

            completed = subprocess.run(
                [str(scripts / "first-mate-runner"), "run-1", str(control), str(worktree)],
                env=environment,
                check=False,
            )

            self.assertEqual(completed.returncode, 1)
            prompts = capture.read_bytes().decode().split("\0")[:-1]
            self.assertEqual(len(prompts), 3)
            self.assertEqual(len(set(prompts)), 1)
            self.assertEqual(
                prompts[0],
                "PROJECT CONTEXT\nBudget: 73\n\nWORKER TASK\nimprove tests",
            )


if __name__ == "__main__":
    unittest.main()
