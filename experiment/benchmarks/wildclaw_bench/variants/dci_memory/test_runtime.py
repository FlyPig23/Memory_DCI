"""Offline resource checks and optional synthetic Docker lifecycle test.

No LLM, credentials, benchmark tasks, or official graders are used.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from experiment.shared.codex_backend import CodexSettings, MCPServer, render_config
from experiment.shared.task_runtime import Mount, RuntimeConfig, TaskSpec
from .memory_runtime import MemoryTaskRuntime, grading_create_args


ROOT = Path(__file__).resolve().parents[5]
IMAGE = "sha256:37d61bbb96578b73dc4017f48b46fec26231b5afb15f65386951ca2612c7d87e"
CONFIG = render_config(CodexSettings(mcp_servers=(MCPServer("v5", "python3", ("/opt/v5/server.py",)),)))


def resources(base: Path, run: Path) -> tuple[Mount, ...]:
    return (Mount(base / "pool", "/pool"), Mount(run / "memory", "/memory", False),
            Mount(base / "code", "/opt/v5"), Mount(base / "rg", "/usr/local/bin/rg"))


def runtime(base: Path, *, config: RuntimeConfig | None = None) -> MemoryTaskRuntime:
    run = base / "run"
    task = TaskSpec("synthetic", "test", "probe", base / "inputs", base / "official-skills")
    return MemoryTaskRuntime(config or RuntimeConfig(base, IMAGE, support_mounts=resources(base, run)),
                             task, run, "dci5-runtime-unit", pool_manifest_sha256="0" * 64,
                             memory_initial_sha256="0" * 64)


class ArgumentTests(unittest.TestCase):
    def setUp(self):
        self.args = ("run", "--detach", "--name", "example", "--env", "EXAMPLE=some value",
                     "--mount", "type=bind,src=/host/pool,dst=/pool,readonly",
                     "--mount", "type=bind,src=/host/run/memory,dst=/memory",
                     "--mount", "type=bind,src=/host/code,dst=/opt/v5,readonly",
                     "--mount", "type=bind,src=/host/rg,dst=/usr/local/bin/rg,readonly",
                     "--mount", "type=bind,src=/host/work,dst=/tmp_workspace",
                     "--entrypoint", "/bin/sh", IMAGE, "-c", "exec sleep infinity")

    def test_exact_removal_preserves_native_tools_and_workspace(self):
        expected = ("create", "--name", "example", "--env", "EXAMPLE=some value", *self.args[12:])
        self.assertEqual(grading_create_args(self.args), expected)

    def test_missing_duplicate_and_wrong_mode_resources_fail_closed(self):
        cases = [("create", *self.args[1:]), ("run", "--mount"), ("run", IMAGE),
                 (*self.args, "--mount", self.args[7]),
                 tuple(item.replace("dst=/pool,readonly", "dst=/pool") for item in self.args),
                 tuple(item.replace("dst=/memory", "dst=/memory,readonly") for item in self.args),
                 tuple(item.replace("dst=/opt/v5,readonly", "dst=/opt/v5") for item in self.args),
                 tuple(item.replace("dst=/pool", "dst=/pool,dst=/memory") for item in self.args)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                grading_create_args(args)

    def test_bad_mcp_or_global_memory_rejected_before_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bad = [render_config(CodexSettings()),
                   render_config(CodexSettings(mcp_servers=(MCPServer("other", "python3"),))),
                   CONFIG.replace("memories = false", "memories = true"),
                   CONFIG.replace("[features]", "[features]\nshell_tool = false")]
            for config in bad:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    runtime(base).prepare(config)
            self.assertFalse((base / "run").exists())

    def test_outside_run_memory_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = RuntimeConfig(base, IMAGE, support_mounts=(Mount(base / "shared", "/memory", False),))
            with self.assertRaises(ValueError):
                runtime(base, config=config)

    def test_legacy_nested_and_overlapping_resource_mounts_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            good = RuntimeConfig(base, IMAGE, support_mounts=resources(base, base / "run"))
            bad = [replace(good, support_mounts=(*good.support_mounts, Mount(base / "legacy", target)))
                   for target in ("/skills", "/corpus", "/root/.codex/memories", "/pool/shadow")]
            bad.append(replace(good, support_mounts=(good.support_mounts[0],
                       Mount(base / "run/workspace", "/memory", False), *good.support_mounts[2:])))
            for config in bad:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    runtime(base, config=config).prepare(CONFIG)
            self.assertFalse((base / "run").exists())


@unittest.skipUnless(os.environ.get("V5_DOCKER_TEST") == "1", "Explicit no-model Docker check")
class DockerTests(unittest.TestCase):
    def test_pool_readonly_memory_private_and_resources_absent_during_grading(self):
        archive = ROOT / "experiment/benchmarks/wildclaw_bench/runs/dci_memory/smoke"
        archive.mkdir(parents=True, exist_ok=True)
        # Retain the compact probe and its mount evidence for the formal audit.
        base = Path(tempfile.mkdtemp(prefix="runtime-probe-", dir=archive))
        inputs = base / "inputs"
        (inputs / "exec").mkdir(parents=True)
        (inputs / "gt").mkdir()
        (inputs / "gt/answer").write_text("hidden")
        (base / "official-skills").mkdir()
        pool = base / "pool"
        pool.mkdir()
        (pool / "synthetic-task_0.5.md").write_text("operation alias\nresult observed\n")
        code = base / "code"
        code.mkdir()
        (code / "server.py").write_text("# Synthetic fixture; never launched.\n")
        loader = base / "loader.py"
        loader.write_text("def load_transcript(path):\n    return []\n")
        rg = ROOT / "experiment/benchmarks/wildclaw_bench/runtime/bin/rg"
        checks = '''
def grade(transcript, workspace_path):
    from pathlib import Path
    p = Path(workspace_path)
    assert not Path('/pool').exists()
    assert not Path('/memory').exists()
    assert not Path('/opt/v5').exists()
    assert Path('/usr/local/bin/rg').is_file()
    assert (p / 'artifact').read_text() == 'done'
    assert (p / 'gt/answer').read_text() == 'hidden'
    assert (p / 'warmup_count').read_text() == 'one\\n'
    assert (p / 'startup_count').read_text() == 'start\\nstart\\n'
    assert not Path('/tmp/solver-ephemeral').exists()
    return {'overall_score': 1.0}
'''
        task = TaskSpec("v5-synthetic-probe", "test", "probe", inputs, base / "official-skills",
                        warmup="printf 'one\\n' >> /tmp_workspace/warmup_count", automated_checks=checks)
        runtimes = []
        try:
            for i in range(2):
                run = base / f"run-{i}"
                memory = run / "memory"
                memory.mkdir(parents=True)
                (memory / "MEMORY.md").write_text("# Empty task memory\n")
                config = RuntimeConfig(ROOT, IMAGE, support_mounts=(Mount(pool, "/pool"),
                    Mount(memory, "/memory", False), Mount(code, "/opt/v5"), Mount(rg, "/usr/local/bin/rg")),
                    startup_commands=("printf 'start\\n' >> /tmp_workspace/startup_count",))
                rt = MemoryTaskRuntime(config, task, run, f"dci5-probe-{base.name.lower()}-{i}",
                                       pool_manifest_sha256="0" * 64, memory_initial_sha256="0" * 64)
                runtimes.append(rt)
                rt.prepare(CONFIG)
                rt.exec(["sh", "-c", f"printf task-{i} > /memory/private-{i}"])
                self.assertNotEqual(rt.exec(["test", "-e", f"/memory/private-{1 - i}"], check=False).returncode, 0)
                self.assertNotEqual(rt.exec(["test", "-e", "/tmp_workspace/gt"], check=False).returncode, 0)
            rt = runtimes[0]
            self.assertNotEqual(rt.exec(["test", "-e", "/memory/private-1"], check=False).returncode, 0)
            self.assertEqual(rt.exec(["rg", "operation", "/pool/synthetic-task_0.5.md"]).stdout, "operation alias\n")
            self.assertNotEqual(rt.exec(["sh", "-c", "echo bad >> /pool/synthetic-task_0.5.md"], check=False).returncode, 0)
            self.assertNotEqual(rt.exec(["sh", "-c", "echo bad >> /opt/v5/server.py"], check=False).returncode, 0)
            rt.exec(["sh", "-c", "printf done > /tmp_workspace/artifact; touch /tmp/solver-ephemeral"])
            transcript = rt.run_dir / "transcript.jsonl"
            transcript.write_text("{}\n")
            rt.freeze_agent()
            result = rt.grade(transcript_path=transcript, transcript_loader=loader)
            self.assertEqual(result["status"], "graded")
            self.assertEqual(result["overall_score"], 1.0)
            evidence = json.loads((rt.run_dir / "native_environment.json").read_text())
            self.assertTrue(evidence["solver"]["memory_task_scoped"])
            self.assertTrue(evidence["grader"]["same_nonresource_configuration"])
            self.assertTrue(evidence["grader"]["resources_absent"])
            self.assertNotEqual(evidence["grader"]["container_id_before"], evidence["grader"]["container_id_after"])
            self.assertEqual((pool / "synthetic-task_0.5.md").read_text(), "operation alias\nresult observed\n")
            self.assertEqual((rt.run_dir / "memory/private-0").read_text(), "task-0")
        finally:
            for rt in reversed(runtimes):
                rt.cleanup()


if __name__ == "__main__":
    unittest.main()
