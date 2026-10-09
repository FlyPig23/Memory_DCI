"""Offline checks of the publication trust boundaries and score reconstruction."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from experiment.shared.dataset_io import restore
from . import download_data as data


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.base = self.root / data.PREFIX

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self):
        return {"schema_version": 1, "dataset_repo": data.REPO, "dataset_subdirectory": data.SUBDIRECTORY,
                "archive_prefix": str(data.PREFIX), "files": [],
                "archives": {name + ".tar.gz": {"file_count": 0} for name in (*data.DEFAULT_BUNDLES, "evidence")}}

    def receipts(self):
        manifest = self.manifest()
        tasks = [f"task-{i:02d}" for i in range(35)]
        manifest.update(evaluated_task_ids=tasks, selected_task_ids=tasks + ["cad-model"], source_snapshot={"files": []})
        trial_records, official, per_task = [], [], {}
        for i, task in enumerate(tasks):
            rewards = []
            pool = []
            for k in range(1, 6):
                number = i * 5 + k - 1
                reward = int(number < 66)
                official_reward = int(number < 17)
                rewards.append(reward)
                run = data.RUNS[0 if k <= 3 else 1]
                directory = Path("runs") / run / "trials" / f"{task}__r{k}"
                completion = {"reward": reward, "valid": True, "task_id": task, "rollout": k}
                result = {"harbor": {"verifier_result": {"rewards": {"reward": reward}}}}
                row = {"task_id": task, "rollout": k, "reward": reward, "valid": True, "audit_sha256": {}}
                for field, value in (("completion", completion), ("result", result)):
                    relative = directory / (field + ".json")
                    digest = write(self.base / relative, value)
                    row[field + "_path"], row[field + "_sha256"] = str(relative), digest
                    manifest["files"].append({"path": str(data.PREFIX / relative), "source_sha256": digest})
                trial_records.append(row)
                official.append({"model": "GPT-5.6 Sol", "task_id": task, "task_content_hash": "version",
                                 "reward": official_reward, "trial_id": f"official-{number}"})
                if official_reward == 0:
                    pool.append({"trial_id": f"official-{number}", "pool_file": "body.txt"})
            per_task[task] = {"v8_rollouts": rewards, "official_sol_passes": sum(r["reward"] for r in official if r["task_id"] == task)}
            write(self.base / f"prepared/v8_corpus/{data.CORPUS}/audit/{task}.json", {"records": pool})
        write(self.base / f"prepared/v8_corpus/{data.CORPUS}/audit/cad-model.json", {"records": []})
        write(self.base / "manifests/v8_report_audit_20261008.json", {"metrics": {"trial_records": trial_records, "per_task": per_task}})
        write(self.base / "manifests/official_trials.json", {"trials": official})
        write(self.base / "manifests/tb4_tasks.json", {"tasks": [{"task_id": t, "content_hash": "version"} for t in tasks]})
        digest = write(self.base / "runs" / data.RUNS[0] / "protocol.json", {})
        write(self.base / "runs" / data.RUNS[1] / "protocol.json", {"parent_protocol_sha256": digest, "excluded_by_parent_oracle": ["cad-model"]})
        write(self.base / "runs" / data.RUNS[0] / "oracle/cad-model__oracle/oracle.json", {"passed": False, "reward": 0})
        return manifest

    def test_manifest_rejects_namespace_escape(self):
        manifest = self.manifest()
        manifest["files"] = [{"path": str(data.PREFIX / "../../../secret"), "bundle": "metadata"}]
        with self.assertRaises(ValueError):
            data.validate_manifest(manifest)

    def test_manifest_rejects_duplicate_paths(self):
        manifest = self.manifest()
        row = {"path": str(data.PREFIX / "same.json"), "bundle": "metadata"}
        manifest["files"] = [row, row]
        with self.assertRaises(ValueError):
            data.validate_manifest(manifest)

    def test_restore_refuses_to_replace_existing_data(self):
        archive = self.root / "bundle.tar.gz"
        name = str(data.PREFIX / "input.txt")
        with tarfile.open(archive, "w:gz") as output:
            member = tarfile.TarInfo(name)
            member.size = 4
            output.addfile(member, io.BytesIO(b"data"))
        target = self.root / name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"mine")
        with self.assertRaises(ValueError):
            restore(archive, self.root, [{"path": name, "bytes": 4, "sha256": hashlib.sha256(b"data").hexdigest()}])
        self.assertEqual(target.read_bytes(), b"mine")

    def test_harbor_digest_uses_posix_path_order(self):
        directory = self.root / "task"
        hashes = {}
        for name in ("entry/child.js", "entry.js"):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            hashes[str(path.relative_to(self.root))] = hashlib.sha256(b"x").hexdigest()
        digest = hashlib.sha256(b"x").hexdigest()
        expected = hashlib.sha256((f"entry.js\0{digest}\nentry/child.js\0{digest}\n").encode()).hexdigest()
        self.assertEqual(data.task_content_hash(directory, self.root, hashes), expected)

    def test_score_reconstruction_uses_all_five_repeats_and_overlap(self):
        result = data.verify_receipts(self.root, self.receipts())
        self.assertEqual((result["v8_passes"], result["v8_trials"]), (66, 175))
        self.assertEqual((result["official_sol_passes"], result["official_sol_trials"]), (17, 175))
        self.assertEqual(result["official_sol_failures_in_query_pool"], 158)

    def test_changed_score_is_rejected(self):
        manifest = self.receipts()
        path = self.base / "runs" / data.RUNS[0] / "trials/task-00__r1/result.json"
        write(path, {"harbor": {"verifier_result": {"rewards": {"reward": 0}}}})
        with self.assertRaises(ValueError):
            data.verify_receipts(self.root, manifest)

    def test_duplicate_official_trial_is_rejected(self):
        manifest = self.receipts()
        path = self.base / "manifests/official_trials.json"
        value = data.load(path)
        value["trials"][1]["trial_id"] = value["trials"][0]["trial_id"]
        write(path, value)
        with self.assertRaises(ValueError):
            data.verify_receipts(self.root, manifest)


if __name__ == "__main__":
    unittest.main()
