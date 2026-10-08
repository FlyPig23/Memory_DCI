"""No-model tests for V6's evidence and per-task memory isolation boundary."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from .seed import create_seed, initialize_seeded_memory, validate_seed
from .tools import Tools, audit_memory


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool = self.root / "pool"
        self.trajectory = self.pool / "trajectories" / "train-one" / "model" / "train-one_0.txt"
        self.trajectory.parent.mkdir(parents=True)
        self.trajectory.write_text("Tool: compile\nObservation: missing header\nFinal reward: 0\n")
        (self.pool / "manifest.json").write_text('{"training_task_ids":["train-one"]}\n')
        self.training = ["train-one"]
        self.provenance = {"builder_model": "gpt-5.6-luna", "builder_reasoning_effort": "xhigh",
                           "source_selection": "failed_training_trajectories", "model_audit_verified": True}
        self.entry = {"slug": "train-one-missing-header", "title": "Check the observed missing header",
                      "keywords": ["compile", "header"], "applicability": "A compiler reports a missing header.",
                      "procedure": ["Inspect the reported header path before retrying the same command."],
                      "pitfalls": ["A failed compilation is not evidence that a proposed repair works."],
                      "verification": "Run the affected compile command after a repair; no successful repair was observed.",
                      "status": "source_observed", "sources": [{"path": "/pool/trajectories/train-one/model/train-one_0.txt",
                                                                  "line_start": 1, "line_end": 3}],
                      "evidence_summary": "The historical command failed with a missing header; root cause is unconfirmed."}
        self.seed = self.root / "seed"

    def freeze(self):
        return create_seed(self.seed, [self.entry], pool=self.pool, training_task_ids=self.training,
                           provenance=self.provenance)

    def initialize(self, name):
        return initialize_seeded_memory(self.root / name, name, self.seed,
                                        pool=self.pool, training_task_ids=self.training)

    def test_exact_entry_clones_have_fresh_identity_zero_events_and_pin_seed(self):
        manifest = self.freeze()
        self.assertEqual(validate_seed(self.seed, pool=self.pool, training_task_ids=self.training), manifest)
        first, second = self.initialize("test-a"), self.initialize("test-b")
        self.assertEqual(first["entry_count"], 1)
        self.assertTrue(first["valid"])
        self.assertEqual(first["event_count"], 0)
        self.assertEqual(second["event_count"], 0)
        self.assertEqual(first["inherited_entries"], 1)
        digest = hashlib.sha256((self.seed / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(first["seed_manifest_sha256"], digest)
        self.assertEqual(second["seed_manifest_sha256"], digest)
        for name in ("entries/train-one-missing-header.md", ".entry_metadata.json"):
            expected = (self.seed / name).read_bytes()
            self.assertEqual((self.root / "test-a" / name).read_bytes(), expected)
            self.assertEqual((self.root / "test-b" / name).read_bytes(), expected)
        for task in ("test-a", "test-b"):
            identity = json.loads((self.root / task / "identity.json").read_text())
            self.assertEqual(identity["task_id"], task)
            self.assertEqual(identity["scope"], "single_test_task")
            self.assertEqual(identity["inherited_entries"], 1)
            self.assertFalse((self.root / task / "events.jsonl").exists())
        self.assertFalse((self.seed / "identity.json").exists())
        self.assertFalse((self.seed / "events.jsonl").exists())

    def test_online_update_changes_only_its_own_copy(self):
        self.freeze()
        self.initialize("test-a")
        self.initialize("test-b")
        seed_before = (self.seed / "entries/train-one-missing-header.md").read_bytes()
        changed = copy.deepcopy(self.entry)
        changed["evidence_summary"] = "Refined this task's applicability; no claim of a verified repair."
        toolbox = Tools(self.pool, self.root / "test-a", "test-a", workspace_root=self.root / "workspace")
        result = toolbox.call("distill", {"decision": "write", "stage": "online", "reason": "Clarify evidence",
                                          "entries": [changed]})
        self.assertFalse(result["isError"], result)
        self.assertTrue(audit_memory(self.root / "test-a")["valid"])
        self.assertNotEqual((self.root / "test-a/entries/train-one-missing-header.md").read_bytes(), seed_before)
        self.assertEqual((self.root / "test-b/entries/train-one-missing-header.md").read_bytes(), seed_before)
        self.assertEqual((self.seed / "entries/train-one-missing-header.md").read_bytes(), seed_before)
        validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)

    def test_rejects_heldout_sources_even_when_file_exists(self):
        heldout = self.pool / "trajectories/test-a/model/test-a_0.txt"
        heldout.parent.mkdir(parents=True)
        heldout.write_bytes(self.trajectory.read_bytes())
        self.entry["sources"][0]["path"] = "/pool/trajectories/test-a/model/test-a_0.txt"
        with self.assertRaisesRegex(ValueError, "not a training task"):
            self.freeze()
        self.assertFalse(self.seed.exists())

    def test_rejects_source_paths_outside_pool_or_with_traversal(self):
        for path in (str(self.trajectory), "/evidence/failure.txt", "/pool/tasks/train-one.md",
                     "/pool/trajectories/train-one/../test-a/result.txt", "/pool//trajectories/train-one/result.txt"):
            with self.subTest(path=path):
                self.entry["sources"][0]["path"] = path
                with self.assertRaises(ValueError):
                    self.freeze()

    def test_rejects_bad_ranges_and_binary_sources(self):
        for start, end in ((1, 4), (3, 2), (0, 1)):
            with self.subTest(start=start, end=end):
                self.entry["sources"][0].update(line_start=start, line_end=end)
                with self.assertRaises(ValueError):
                    self.freeze()
        self.entry["sources"][0].update(line_start=1, line_end=1)
        self.trajectory.write_bytes(b"binary\x00\n")
        with self.assertRaisesRegex(ValueError, "binary"):
            self.freeze()

    def test_rejects_success_or_unscored_sources_even_if_named_as_failures(self):
        success = self.trajectory.with_name("train-one_1.txt")
        success.write_bytes(self.trajectory.read_bytes())
        self.entry["sources"][0]["path"] = "/pool/trajectories/train-one/model/train-one_1.txt"
        with self.assertRaisesRegex(ValueError, "score-zero filename"):
            self.freeze()
        self.entry["sources"][0]["path"] = "/pool/trajectories/train-one/model/train-one_0.txt"
        for header in ("Official reward: 1; trial ID: x\n", "Official scored execution: False; attempt: 1\n"):
            with self.subTest(header=header):
                self.trajectory.write_text(header + "Tool: compile\nObservation: failed\n")
                with self.assertRaises(ValueError):
                    self.freeze()

    def test_historical_terminal_nuls_outside_citation_preserve_raw_bytes_and_windows(self):
        # Include CRLF so no newline normalization can silently alter hashes.
        data = (b"# Historical training trajectory: train-one\r\n"
                b"Official reward: 0; trial ID: frozen-run\r\n"
                b"Official scored execution: True; attempt: 1\r\n"
                b"Tool output: record=[A\x00\x00]\r\n"
                b"Tool: compile\r\nObservation: missing header\r\n")
        self.trajectory.write_bytes(data)
        self.entry["sources"][0].update(line_start=5, line_end=6)
        self.freeze()
        validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)
        self.initialize("test-a")
        self.assertEqual(self.trajectory.read_bytes(), data)
        catalog = json.loads((self.seed / ".entry_metadata.json").read_text())
        source = catalog[self.entry["slug"]]["sources"][0]
        self.assertEqual((source["line_start"], source["line_end"]), (5, 6))
        self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(source["lines_sha256"], hashlib.sha256(
            b"Tool: compile\r\nObservation: missing header\r\n").hexdigest())
        self.assertEqual(len(self.trajectory.read_text().splitlines()), len(data.splitlines()))
        self.assertEqual((self.root / "test-a/entries/train-one-missing-header.md").read_bytes(),
                         (self.seed / "entries/train-one-missing-header.md").read_bytes())
        # Even an uncited byte remains bound by the full source digest.
        self.trajectory.write_bytes(data.replace(b"A\x00\x00", b"B\x00\x00"))
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)

    def test_historical_header_does_not_permit_nuls_inside_cited_window(self):
        self.trajectory.write_bytes(
            b"# Historical training trajectory: train-one\n"
            b"Official reward: 0\nOfficial scored execution: True\n"
            b"Tool output: file contents\x00\nObservation: missing header\n")
        self.entry["sources"][0].update(line_start=4, line_end=5)
        with self.assertRaisesRegex(ValueError, "cited trajectory window"):
            self.freeze()

    def test_uncited_nuls_require_matching_historical_task_and_explicit_scored_failure(self):
        correct = (b"# Historical training trajectory: train-one\n"
                   b"Official reward: 0\nOfficial scored execution: True\n")
        headers = [b"Arbitrary binary artifact\nHeader two\nHeader three\n",
                   correct.replace(b"trajectory: train-one", b"trajectory: other-task"),
                   correct.replace(b"Official reward: 0", b"Reward not available"),
                   correct.replace(b"Official scored execution: True", b"Execution not scored")]
        self.entry["sources"][0].update(line_start=5, line_end=5)
        for header in headers:
            with self.subTest(header=header):
                self.trajectory.write_bytes(header + b"binary\x00\nReadable observation\n")
                with self.assertRaisesRegex(ValueError, "explicitly scored historical"):
                    self.freeze()

    def test_uncited_invalid_utf8_still_rejected_even_with_historical_header(self):
        self.trajectory.write_bytes(
            b"# Historical training trajectory: train-one\n"
            b"Official reward: 0\nOfficial scored execution: True\n"
            b"Invalid raw bytes: \xff\x00\nReadable observation\n")
        self.entry["sources"][0].update(line_start=5, line_end=5)
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.freeze()

    def test_source_window_hash_and_catalog_cannot_be_forged_by_rehashing_file(self):
        self.freeze()
        catalog_path = self.seed / ".entry_metadata.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["train-one-missing-header"]["sources"][0]["lines_sha256"] = "0" * 64
        catalog_path.write_text(json.dumps(catalog))
        manifest_path = self.seed / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files_sha256"][".entry_metadata.json"] = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)

    def test_rejects_symlink_source_root_seed_or_memory_parent(self):
        original = self.trajectory.with_suffix(".original")
        self.trajectory.rename(original)
        self.trajectory.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.freeze()
        self.trajectory.unlink()
        original.rename(self.trajectory)
        self.freeze()
        link = self.root / "linked"
        link.symlink_to(self.seed, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            validate_seed(link, pool=self.pool, training_task_ids=self.training)
        holder = self.root / "real-parent"
        holder.mkdir()
        alias = self.root / "alias-parent"
        alias.symlink_to(holder, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            initialize_seeded_memory(alias / "memory", "test-a", self.seed,
                                     pool=self.pool, training_task_ids=self.training)

    def test_seed_and_source_tampering_are_detected(self):
        self.freeze()
        entry_path = self.seed / "entries/train-one-missing-header.md"
        original = entry_path.read_bytes()
        entry_path.write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "inventory/hash"):
            validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)
        entry_path.write_bytes(original)
        self.trajectory.write_text("Tool: compile\nObservation: a different error\nFinal reward: 0\n")
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)

    def test_seed_rejects_unlisted_files_and_different_split(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError, "membership"):
            validate_seed(self.seed, pool=self.pool, training_task_ids=["train-two"])
        (self.seed / "identity.json").write_text('{"task_id":"previous-test"}')
        with self.assertRaisesRegex(ValueError, "inventory/hash"):
            validate_seed(self.seed, pool=self.pool, training_task_ids=self.training)

    def test_empty_library_duplicate_slugs_and_inflated_status_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            create_seed(self.seed, [], pool=self.pool, training_task_ids=self.training, provenance=self.provenance)
        with self.assertRaisesRegex(ValueError, "duplicate seed slug"):
            create_seed(self.seed, [self.entry, self.entry], pool=self.pool,
                        training_task_ids=self.training, provenance=self.provenance)
        self.entry["status"] = "locally_verified"
        with self.assertRaisesRegex(ValueError, "unseen tasks"):
            self.freeze()

    def test_missing_or_unverified_builder_provenance_rejected(self):
        for mutation in ({"builder_model": ""}, {"model_audit_verified": False},
                         {"model_audit_verified": 1}, {"source_selection": "all_test_failures"}):
            with self.subTest(mutation=mutation):
                provenance = {**self.provenance, **mutation}
                with self.assertRaises(ValueError):
                    create_seed(self.seed, [self.entry], pool=self.pool,
                                training_task_ids=self.training, provenance=provenance)
        self.provenance.pop("builder_reasoning_effort")
        with self.assertRaisesRegex(ValueError, "provenance requires"):
            self.freeze()

    def test_initialization_never_overwrites_or_reuses_training_identity(self):
        self.freeze()
        self.initialize("test-a")
        before = (self.root / "test-a/identity.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "fresh empty"):
            self.initialize("test-a")
        self.assertEqual((self.root / "test-a/identity.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "non-training"):
            self.initialize("train-one")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.freeze()


if __name__ == "__main__":
    unittest.main()
