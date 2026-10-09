"""Offline release checks: original prompts, standalone paths and frozen overrides."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from . import run_v8, v8_corpus
from ..engine import harbor_runtime, protocol, runtime, v7_memory


class ReleaseRuntimeTests(unittest.TestCase):
    def test_prompts_match_the_original_experiment_byte_for_byte(self):
        # Recorded from the original pre-publication source, with the same
        # synthetic one-task / one-trajectory fixture below.
        expected = {
            "unseeded": "7cb972712df0a2c87505e2de73d17fdeb1b6dd8d0f259aaecd8cef0335e11ae4",
            "seeded": "73786f0fac9a6c5d81e00153bc55ca6961b167f2cf023825a59940ea7343a830",
            "review": "942d63062cb4af044880dbb787be386d76281fae07404da587376181f1c5f579",
            "builder_template": "561b264d2696fdbe357d426ea1227c3fa369a852bfd91996958162e564ebaca7",
        }
        with tempfile.TemporaryDirectory() as temporary:
            pool = Path(temporary)
            (pool / "trajectories/task/model").mkdir(parents=True)
            (pool / "tasks").mkdir()
            (pool / "tasks/task.md").write_text("Task\n")
            (pool / "trajectories/task/model/task_0.txt").write_text("Failure\n")
            actual = {
                "unseeded": harbor_runtime.solver_instructions_v7(pool, "/app"),
                "seeded": harbor_runtime.solver_instructions_v7(pool, "/app", initial_memory={"entry_count": 3}),
                "review": harbor_runtime.review_instructions_v7(),
                "builder_template": v7_memory.PROMPT_PATH.read_text(),
            }
        self.assertEqual({key: hashlib.sha256(value.encode()).hexdigest() for key, value in actual.items()}, expected)

    def test_runtime_paths_and_source_inventory_are_independent_of_tb21(self):
        base = Path(__file__).resolve().parents[1]
        for module in (run_v8, v8_corpus, harbor_runtime, protocol, runtime, v7_memory):
            self.assertEqual(module.BASE, base)
        sources = protocol.sources(base, "v7")
        self.assertTrue(sources)
        self.assertTrue(all("terminal_bench_2_1" not in name for name in sources))
        self.assertIn("experiment/benchmarks/terminal_bench_4/engine/package_data.py", sources)
        self.assertEqual(runtime.HARNESS_IMPORT, "experiment.benchmarks.terminal_bench_4.engine.harbor_runtime")

    def test_plan_freezes_model_and_effort_overrides_without_running_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            files = {
                "config.json": {"model": "default-model", "reasoning_effort": "medium", "cli_version": "0.153.4",
                                "review_image": "python@sha256:fixture", "verifier_policy": "official"},
                "manifests/v8_tasks.json": {"selected": [], "tasks": []},
                "manifests/tb4_tasks.json": {"tasks": []},
                "manifests/v8_failure_sources.json": {},
                "manifests/v8_download_audit.json": {},
                "prepared/v8_corpus/fixture/index.json": {"tasks": {}},
                "prepared/v8_corpus/fixture/seeds_index.json": {"tasks": {}},
                "prepared/v8_corpus/decontamination_decisions.json": {},
            }
            for name, value in files.items():
                target = base / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(value))
            with patch.object(run_v8, "BASE", base), patch.object(run_v8, "SCRIPTS", ()), \
                    patch.object(v8_corpus, "verify"), patch.object(protocol, "sources", return_value={}):
                plan = run_v8.build_plan("new-model-run", "fixture", rollouts=5,
                                         model="chosen-model", effort="high")
            self.assertEqual(plan["settings"]["model"], "chosen-model")
            self.assertEqual(plan["settings"]["reasoning_effort"], "high")
            self.assertEqual(plan["rollouts"], 5)
            self.assertFalse((base / "runs").exists())

    def test_child_job_applies_frozen_overrides_before_calling_the_solver(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            task_dir, pool = base / "prepared/tasks/fixture", base / "prepared/pool"
            task_dir.mkdir(parents=True)
            pool.mkdir()
            task = {"task_id": "fixture", "source_path": "prepared/tasks/fixture", "files_sha256": {},
                    "pool": {"path": "prepared/pool", "files_sha256": {}}, "memory_seed": None,
                    "official_agent_timeout_seconds": 30}
            settings = {"model": "chosen-model", "reasoning_effort": "high", "cli_version": "0.153.4",
                        "review_image": "fixture-image", "verifier_policy": "official"}
            plan = {"run_name": "override-check", "settings": settings, "tasks": [task]}
            seen = []

            async def solver(*args, **kwargs):
                seen.append((harbor_runtime.MODEL, harbor_runtime.EFFORT, runtime.MODEL, runtime.EFFORT))

            with patch.object(run_v8, "BASE", base), \
                    patch.object(run_v8.health_guard, "ensure_auth_lifetime"), \
                    patch.object(run_v8.engine_run, "resolve_image", new=AsyncMock(return_value={"image_id": "fixture"})), \
                    patch.object(harbor_runtime, "run_trial", new=solver), \
                    patch.object(harbor_runtime, "python_installation", return_value=base), \
                    patch.object(runtime, "environment_for", return_value={}), \
                    patch.object(run_v8, "summary", return_value={"valid": True}), \
                    patch.multiple(harbor_runtime, MODEL=harbor_runtime.MODEL, EFFORT=harbor_runtime.EFFORT,
                                   CLI_VERSION=harbor_runtime.CLI_VERSION), \
                    patch.multiple(runtime, MODEL=runtime.MODEL, EFFORT=runtime.EFFORT, CLI_VERSION=runtime.CLI_VERSION):
                asyncio.run(run_v8.rollout_job(plan, {"task_id": "fixture", "job_id": "fixture__r1"}, base / "auth.json"))
            self.assertEqual(seen, [("chosen-model", "high", "chosen-model", "high")])

    def test_zero_concurrency_is_rejected_instead_of_waiting_forever(self):
        with self.assertRaisesRegex(ValueError, "at least 1"):
            run_v8.execute({}, workers=0, open_tasks=2, auth_file=Path("unused"))
        with self.assertRaisesRegex(ValueError, "at least 1"):
            run_v8.execute({}, workers=1, open_tasks=0, auth_file=Path("unused"))


if __name__ == "__main__":
    unittest.main()
