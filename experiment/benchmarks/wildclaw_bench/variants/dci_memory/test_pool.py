"""V5 pool boundary tests using small synthetic versions of all 60 tasks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from experiment.benchmarks.wildclaw_bench.variants.dci_memory.pool import PoolError, prepare_pool, validate_pool


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.destination = self.root / "prepared/pool"
        self.split_path = self.root / "experiment/benchmarks/wildclaw_bench/manifests/split.json"
        self.task_path = self.root / "experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json"
        self.corpus_dir = self.root / "experiment/benchmarks/wildclaw_bench/corpus/frozen_build"
        self.corpus_path = self.corpus_dir / "manifest.json"
        self.score_path = self.root / "experiment/benchmarks/wildclaw_bench/reports/source_model_scores/scores.json"
        build = [f"build_task_{i:02d}" for i in range(36)]
        test = [f"test_task_{i:02d}" for i in range(24)]
        self.tasks = {"tasks": [{"task_id": t, "prompt": f"public request {t}",
                                 "prompt_sha256": sha(f"public request {t}".encode()),
                                 "rubric": "DO_NOT_EXPORT_RUBRIC", "private_result": "DO_NOT_EXPORT_GOLD"}
                                for t in build + test]}
        write_json(self.task_path, self.tasks)
        self.split = {"build_task_ids": build, "test_task_ids": test,
                      "manifest_sha256": sha(self.task_path.read_bytes())}
        write_json(self.split_path, self.split)
        self.corpus = {"view": "frozen_build", "task_ids": build, "dataset_revision": "fixture-rev",
                       "split_sha256": sha(self.split_path.read_bytes()), "episodes": []}
        self.scores = {"revision": "fixture-rev", "metric": "official score.json overall_score",
                       "validated_build_scores": 432, "records": []}
        asset = b"test image bytes"
        image_path = f"images/{sha(asset)}.png"
        (self.corpus_dir / "images").mkdir(parents=True)
        (self.corpus_dir / image_path).write_bytes(asset)
        for task_id in build:
            for index in range(12):
                alias, model, run = f"model_{index}", f"Model {index}", f"run_{task_id}_{index}"
                episode_id = f"episode_{task_id}_{index}"
                text = f"Task {task_id}; model {model}; original content\n".encode()
                path = f"episodes/{task_id}/{episode_id}.txt"
                source = self.corpus_dir / path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(text)
                score = 0.1234567890123456 if index == 0 else 0.5
                provenance = {"archive": f"{alias}.tar.gz", "archive_sha256": "a" * 64,
                              "member": f"{alias}/{task_id}/{run}/score.json", "sha256": "b" * 64}
                self.corpus["episodes"].append({
                    "task_id": task_id, "model_alias": alias, "model": model, "run_id": run,
                    "episode_id": episode_id, "split": "build", "path": path, "content_hash": sha(text),
                    "images": [{"path": image_path, "sha256": sha(asset)}] if index == 0 else [],
                    "outcome": {"status": "official_metrics_available", "metrics": {"overall_score": score},
                                "source": provenance},
                    "sources": [{"kind": "archive_chat", "archive": provenance["archive"],
                                 "archive_sha256": provenance["archive_sha256"], "revision": "fixture-rev",
                                 "member": f"{alias}/{task_id}/{run}/chat.jsonl"}],
                })
                self.scores["records"].append({
                    "task_id": task_id, "model_alias": alias, "model": model, "run_id": run,
                    "split": "build", "overall_score": score, "metrics": {"private_rubric": "NOT_EXPORTED"},
                    "archive": provenance["archive"], "archive_sha256": provenance["archive_sha256"],
                    "member": provenance["member"], "score_sha256": provenance["sha256"],
                })
        self.scores["records"].extend({"task_id": t, "split": "test", "overall_score": 0.987654321}
                                      for t in test)
        write_json(self.corpus_path, self.corpus)
        write_json(self.score_path, self.scores)

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_boundaries_scores_assets_and_idempotence(self):
        manifest = prepare_pool(self.root, self.destination)
        self.assertEqual((manifest["task_count"], manifest["trajectory_count"], manifest["asset_count"]),
                         (36, 432, 1))
        self.assertEqual(prepare_pool(self.root, self.destination), manifest)
        for row in manifest["trajectories"]:
            target = self.destination / row["path"]
            source = self.corpus_dir / row["source_path"]
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
            self.assertEqual(target.name, f"{row['task_id']}_{row['score_text']}.txt")
        self.assertEqual(manifest["trajectories"][0]["score_text"], "0.1234567890123456")
        self.assertEqual(len(list((self.destination / "trajectories/build_task_00").glob("*/*_0.5.txt"))), 11)
        exported = "\n".join(p.read_text() for p in self.destination.rglob("*") if p.suffix in {".md", ".json", ".txt"})
        for forbidden in ("test_task_", "DO_NOT_EXPORT_RUBRIC", "DO_NOT_EXPORT_GOLD", "NOT_EXPORTED", "0.987654321"):
            self.assertNotIn(forbidden, exported)

    def test_refuses_test_episode(self):
        self.corpus["episodes"][0]["task_id"] = "test_task_00"
        write_json(self.corpus_path, self.corpus)
        with self.assertRaisesRegex(PoolError, "12 trajectories"):
            prepare_pool(self.root, self.destination)
        self.assertFalse(self.destination.exists())

    def test_refuses_wrong_run_join(self):
        self.scores["records"][0]["run_id"] = "other_run"
        write_json(self.score_path, self.scores)
        with self.assertRaisesRegex(PoolError, "No matching task/model/run"):
            prepare_pool(self.root, self.destination)

    def test_refuses_score_provenance_mismatch(self):
        self.scores["records"][0]["archive_sha256"] = "c" * 64
        write_json(self.score_path, self.scores)
        with self.assertRaisesRegex(PoolError, "Score provenance"):
            prepare_pool(self.root, self.destination)

    def test_refuses_score_change(self):
        self.scores["records"][0]["overall_score"] = 0.4
        write_json(self.score_path, self.scores)
        with self.assertRaisesRegex(PoolError, "score value mismatch"):
            prepare_pool(self.root, self.destination)

    def test_refuses_source_tamper(self):
        (self.corpus_dir / self.corpus["episodes"][0]["path"]).write_text("tampered")
        with self.assertRaisesRegex(PoolError, "Trajectory hash mismatch"):
            prepare_pool(self.root, self.destination)

    def test_refuses_duplicate_model_episode(self):
        self.corpus["episodes"][1] = self.corpus["episodes"][0]
        write_json(self.corpus_path, self.corpus)
        with self.assertRaisesRegex(PoolError, "Duplicate trajectory/model/episode"):
            prepare_pool(self.root, self.destination)

    def test_existing_export_rejects_additions_and_tamper(self):
        manifest = prepare_pool(self.root, self.destination)
        injected = self.destination / "test_answers.txt"
        injected.write_text("unexpected")
        with self.assertRaisesRegex(PoolError, "unlisted files"):
            prepare_pool(self.root, self.destination)
        injected.unlink()
        (self.destination / manifest["tasks"][0]["path"]).write_text("tampered")
        with self.assertRaisesRegex(PoolError, "Export hash mismatch"):
            validate_pool(self.destination)

    def test_refuses_source_path_escape(self):
        self.corpus["episodes"][0]["path"] = "../../manifests/task_manifest.json"
        write_json(self.corpus_path, self.corpus)
        with self.assertRaisesRegex(PoolError, "Unsafe relative path"):
            prepare_pool(self.root, self.destination)

    def test_refuses_symlinked_export(self):
        manifest = prepare_pool(self.root, self.destination)
        path = self.destination / manifest["tasks"][0]["path"]
        original = path.read_bytes()
        path.unlink()
        other = self.root / "other.md"
        other.write_bytes(original)
        path.symlink_to(other)
        with self.assertRaisesRegex(PoolError, "symlink"):
            validate_pool(self.destination)


if __name__ == "__main__":
    unittest.main()
