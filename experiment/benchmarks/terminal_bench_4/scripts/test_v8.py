"""No-model, no-Docker tests for V8: task selection, failure sources, contamination rules and job scheduling."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import run_v8, v8_corpus, v8_sources


def task(task_id, *, gpus=0, mcp=None, steps=None, content="h"):
    return {"task_id": task_id, "revision": 1, "content_hash": content, "task_version_id": "tv-" + task_id,
            "environment_config": {"gpus": gpus, "mcp_servers": mcp or [], "docker_image": f"img/{task_id}-env@sha256:1"},
            "verifier_config": {"timeout_sec": 60, "environment_mode": "separate",
                                "environment": {"docker_image": f"img/{task_id}-ver@sha256:2"}},
            "agent_config": {"timeout_sec": 600}, "multi_step_reward_strategy": steps, "required_env": None}


def trial(task_id, reward, *, model="GPT-5.6 Sol", agent="Codex", effort="max", trial_id=None, content="h"):
    return {"model": model, "agent": agent, "effort": effort, "row_id": f"{model}-{effort}", "task_id": task_id,
            "trial_id": trial_id or f"{task_id}-{model}-{effort}-{reward}-{id(object())}", "job_id": "job",
            "task_content_hash": content, "reward": reward, "exception_type": None,
            "trajectory_path": "trials/x/trajectory.json", "status": "completed"}


class SelectionTests(unittest.TestCase):
    def test_selects_tasks_official_sol_fails_at_least_half_of_and_excludes_unsupported_environments(self):
        tasks = [task("easy"), task("hard"), task("gpu", gpus=1), task("sidecar", mcp=[{"name": "playwright"}]),
                 task("edge")]
        trials = ([trial("easy", 1) for _ in range(5)] + [trial("hard", 1)] + [trial("hard", 0) for _ in range(4)]
                  + [trial("gpu", 0) for _ in range(5)] + [trial("sidecar", 0) for _ in range(5)]
                  # 3/5 is not "below half"; other conditions never count toward the selection
                  + [trial("edge", 1) for _ in range(3)] + [trial("edge", 0) for _ in range(2)]
                  + [trial("easy", 0, model="Opus 5.5", agent="Claude Code") for _ in range(5)])
        rows = {row["task_id"]: row for row in v8_sources.select(trials, tasks)}
        self.assertEqual({k for k, r in rows.items() if r["selected"]}, {"hard"})
        self.assertEqual((rows["hard"]["official_sol_passes"], rows["hard"]["official_sol_scored"]), (1, 5))
        self.assertEqual((rows["gpu"]["excluded"], rows["sidecar"]["excluded"]),
                         ("needs_gpu_or_tpu", "needs_mcp_sidecar_service"))
        self.assertTrue(rows["gpu"]["hard"] and not rows["gpu"]["selected"])
        self.assertFalse(rows["edge"]["hard"])

    def test_failure_sources_keep_only_scored_failures_on_the_exact_task_version(self):
        tasks = [task("hard", content="current")]
        trials = [trial("hard", 0, trial_id="f1", content="current"), trial("hard", 1, trial_id="s1", content="current"),
                  trial("hard", None, trial_id="u1", content="current"), trial("hard", 0, trial_id="old", content="previous"),
                  trial("hard", 0, trial_id="f1", content="current"), trial("other", 0, trial_id="x", content="current")]
        rows, counts = v8_sources.failure_sources(trials, tasks, {"hard"})
        self.assertEqual([r["trial_id"] for r in rows], ["f1"])
        self.assertEqual(counts, {"failed": 1, "success_excluded": 1, "unscored": 1, "other_task_version": 1})


class ContaminationRuleTests(unittest.TestCase):
    def test_benchmark_access_pattern_ignores_task_image_names_and_task_ids(self):
        hits = ["curl https://www.tbench.ai/registry/x", "https://hub.harborframework.com/tasks/terminal-bench/x",
                "git clone https://github.com/harbor-framework/terminal-bench", "pip download terminal-bench-2",
                "https://huggingface.co/datasets/org/terminal-bench-4-mirror", "see original-tasks/foo"]
        for text in hits:
            self.assertTrue(v8_corpus.BENCHMARK_ACCESS.search(text), text)
        misses = ["image harborframework/terminal-bench:foo-environment-1@sha256:abc", "task terminal-bench/foo",
                  "pip install harbor", "github.com/numpy/numpy"]
        for text in misses:
            self.assertFalse(v8_corpus.BENCHMARK_ACCESS.search(text), text)

    def test_calibrated_rule_excludes_sources_harness_and_heavy_overlap_and_keeps_natural_overlap(self):
        base = {"benchmark_access": [], "harness_refs": [], "solution_coverage": 0.01, "tests_overlap_max_run": 12,
                "solution_overlap_max_run": 22, "fetch_hits": 1, "test_file_refs": 0, "frozen_rule_flagged": True}
        self.assertEqual(v8_corpus.calibrated_rule(base)[0], "keep")
        self.assertEqual(v8_corpus.calibrated_rule({**base, "frozen_rule_flagged": False}), ("", ""))
        cases = [({"benchmark_access": [{"line": 3, "match": "tbench.ai"}]}, "benchmark_source_access"),
                 ({"harness_refs": [{"line": 9, "match": "solve.sh"}]}, "harness files"),
                 ({"solution_coverage": 0.6}, "reference solution"), ({"tests_overlap_max_run": 61}, "hidden tests")]
        for change, reason in cases:
            decision, text = v8_corpus.calibrated_rule({**base, **change})
            self.assertEqual(decision, "exclude")
            self.assertIn(reason, text)


class FakeProcess:
    returncode = 0

    def poll(self):
        return 0


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        patcher = patch.object(run_v8, "BASE", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, tasks, rollouts=2):
        jobs = []
        for task_id in tasks:
            jobs.append({"job_id": f"{task_id}__oracle", "kind": "oracle", "task_id": task_id})
            jobs += [{"job_id": f"{task_id}__r{k}", "kind": "rollout", "task_id": task_id, "rollout": k}
                     for k in range(1, rollouts + 1)]
        return {"run_name": "sched", "tasks": [{"task_id": t, "images": [f"{t}-env", f"{t}-ver"]} for t in tasks],
                "jobs": jobs}

    def test_oracle_gates_rollouts_images_are_released_and_the_open_task_cap_holds(self):
        plan = self.plan(["a", "b", "c"])
        passes = {"a": True, "b": False, "c": True}
        launched, cleaned, open_now, peak = [], [], set(), [0]

        def spawn(plan, job, auth_file, log):
            launched.append(job["job_id"])
            open_now.add(job["task_id"])
            peak[0] = max(peak[0], len(open_now))
            root = run_v8.run_dir(plan["run_name"])
            if job["kind"] == "oracle":
                run_v8.write_json(root / "oracle" / job["job_id"] / "oracle.json", {"passed": passes[job["task_id"]]})
            else:
                run_v8.write_json(root / "trials" / job["job_id"] / "completion.json", {"valid": True})
            return FakeProcess()

        def cleanup(task):
            cleaned.append(task["task_id"])
            open_now.discard(task["task_id"])

        run_v8.execute(plan, workers=2, open_tasks=1, auth_file=Path("auth.json"), spawn=spawn,
                       sleep=lambda _: None, disk=lambda: 100.0, cleanup=cleanup)
        self.assertEqual(launched, ["a__oracle", "a__r1", "a__r2", "b__oracle", "c__oracle", "c__r1", "c__r2"])
        self.assertEqual(cleaned, ["a", "b", "c"])
        self.assertEqual(peak[0], 1)
        self.assertEqual(json.loads((run_v8.run_dir("sched") / "protocol.json").read_text()), plan)

    def test_a_changed_plan_is_refused_and_low_disk_stops_instead_of_waiting(self):
        plan = self.plan(["a"])
        root = run_v8.run_dir("sched")
        run_v8.write_json(root / "protocol.json", {**plan, "rollouts": 99})
        with self.assertRaisesRegex(ValueError, "changed since this run was frozen"):
            run_v8.execute(plan, workers=1, open_tasks=1, auth_file=Path("a"), spawn=None, sleep=lambda _: None,
                           disk=lambda: 100.0, cleanup=lambda task: None)
        (root / "protocol.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "GB free"):
            run_v8.execute(plan, workers=1, open_tasks=1, auth_file=Path("a"), spawn=None, sleep=lambda _: None,
                           disk=lambda: 1.0, cleanup=lambda task: None)

    def test_an_oracle_without_a_verdict_blocks_only_its_own_task(self):
        plan = self.plan(["a", "b"], rollouts=1)
        root = run_v8.run_dir("sched")
        launched = []

        def spawn(plan, job, auth_file, log):
            launched.append(job["job_id"])
            directory = root / ("oracle" if job["kind"] == "oracle" else "trials") / job["job_id"]
            directory.mkdir(parents=True)
            if job["job_id"] == "b__oracle":
                run_v8.write_json(directory / "oracle.json", {"passed": True})
            elif job["kind"] == "rollout":
                run_v8.write_json(directory / "completion.json", {"valid": True})
            return FakeProcess()  # a__oracle stops without a verdict, e.g. its image pull failed

        run_v8.execute(plan, workers=2, open_tasks=1, auth_file=Path("a"), spawn=spawn, sleep=lambda _: None,
                       disk=lambda: 100.0, cleanup=lambda task: None)
        self.assertEqual(launched, ["a__oracle", "b__oracle", "b__r1"])
        self.assertEqual(run_v8.job_state(plan, plan["jobs"][0]), "incomplete")

    def test_an_extension_adds_later_rollouts_under_the_parent_inputs_and_oracles(self):
        parent = {**self.plan(["a", "b", "c"], rollouts=3), "rollouts": 3, "settings": {"model": "m"},
                  "inputs_sha256": {"x": "1"}, "sources_sha256": {"runtime.py": "r", "experiment/benchmarks/terminal_bench_4/scripts/run_v8.py": "old"}}
        root = run_v8.run_dir("sched")
        run_v8.write_json(root / "protocol.json", parent)
        for task_id, passed in (("a", True), ("b", False), ("c", True)):
            run_v8.write_json(root / "oracle" / f"{task_id}__oracle" / "oracle.json", {"passed": passed})
        fresh = {**parent, "run_name": "sched_r45", "rollouts": 5,
                 "sources_sha256": {**parent["sources_sha256"], "experiment/benchmarks/terminal_bench_4/scripts/run_v8.py": "new"}}
        plan = run_v8.extend_plan(parent, fresh, 5)
        self.assertEqual([j["job_id"] for j in plan["jobs"]], ["a__r4", "a__r5", "c__r4", "c__r5"])
        self.assertEqual((plan["parent_run"], plan["excluded_by_parent_oracle"], plan["run_name"]), ("sched", ["b"], "sched_r45"))
        for change in ({"settings": {"model": "other"}}, {"sources_sha256": {"runtime.py": "changed"}}):
            with self.assertRaisesRegex(ValueError, "differ from the parent"):
                run_v8.extend_plan(parent, {**fresh, **change}, 5)
        with self.assertRaisesRegex(ValueError, "beyond"):
            run_v8.extend_plan(parent, fresh, 3)

    def test_interrupted_jobs_are_left_for_review_not_relaunched(self):
        plan = self.plan(["a"], rollouts=1)
        root = run_v8.run_dir("sched")
        run_v8.write_json(root / "oracle" / "a__oracle" / "oracle.json", {"passed": True})
        (root / "trials" / "a__r1").mkdir(parents=True)  # started earlier, never completed
        launched = []
        run_v8.execute(plan, workers=1, open_tasks=1, auth_file=Path("a"),
                       spawn=lambda *a: launched.append(a[1]["job_id"]) or FakeProcess(),
                       sleep=lambda _: None, disk=lambda: 100.0, cleanup=lambda task: None)
        self.assertEqual(launched, [])
        self.assertEqual(run_v8.job_state(plan, plan["jobs"][1]), "incomplete")

    def test_extensions_freeze_relocated_engine_shared_and_corpus_sources(self):
        controller = "experiment/benchmarks/terminal_bench_4/scripts/run_v8.py"
        protected = (
            "experiment/benchmarks/terminal_bench_4/engine/harbor_runtime.py",
            "experiment/shared/memory/tools.py",
            "experiment/benchmarks/terminal_bench_4/scripts/v8_corpus.py",
        )
        parent = {**self.plan(["a"], rollouts=3), "rollouts": 3, "settings": {"model": "m"}, "inputs_sha256": {},
                  "sources_sha256": {controller: "old", **{name: "frozen" for name in protected}}}
        root = run_v8.run_dir("sched")
        run_v8.write_json(root / "protocol.json", parent)
        run_v8.write_json(root / "oracle/a__oracle/oracle.json", {"passed": True})
        fresh = {**parent, "run_name": "sched_r45", "rollouts": 5,
                 "sources_sha256": {**parent["sources_sha256"], controller: "new"}}
        self.assertEqual(len(run_v8.extend_plan(parent, fresh, 5)["jobs"]), 2)
        for name in (*protected, "experiment/benchmarks/terminal_bench_4/engine/new_helper.py"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "differ from the parent"):
                run_v8.extend_plan(parent, {**fresh, "sources_sha256": {**fresh["sources_sha256"], name: "changed"}}, 5)


if __name__ == "__main__":
    unittest.main()
