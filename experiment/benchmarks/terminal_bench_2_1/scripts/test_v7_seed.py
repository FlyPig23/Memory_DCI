"""No-model tests for V7's same-task seed: single-task binding, sources, NUL and provenance rules."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from experiment.benchmarks.terminal_bench_2_1.scripts import protocol, v7_seed
from experiment.shared.memory.tools import audit_memory

TASK = "alpha-task"
VERSION = "00000000-0000-4000-8000-000000000000"
INSTRUCTION = "Write /app/out.txt containing the sorted words of /app/in.txt, one per line.\n"


def header(task: str, model: str, attempt: str) -> str:
    """The 8-line header v7_corpus writes on every pool trajectory (the last line is blank)."""
    return (f"# Historical failed attempt at this task: {task}\n"
            f"Model: {model}; agent: terminus-2 2.0.0\n"
            f"Official reward: 0; attempt ID: {attempt}\n"
            "Official scored execution: True; attempt: 1; parent scored trial: self\n"
            f"Source task compatibility: exact_task_version; target task version: {VERSION}\n"
            "The reward is a source-run outcome, not a guarantee for every historical action.\n"
            "All ATIF fields below are rendered without summarizing or distilling; embedded images become local files.\n"
            "\n")


def body(note: str) -> str:
    return ("schema_version:\n  ATIF-v1.7\nsteps:\n  [0]\n    message:\n      sort /app/in.txt > /app/out.txt\n"
            f"    observation:\n      error: {note}\n      process exited 1\n  [1]\n    message:\n"
            "      I believe the task is complete.\n")


def make_pool(pool: Path, task: str, attempts: list[dict], unavailable: list[dict] = ()) -> dict:
    """Write one pool in the layout v7_corpus builds and return its index.json task record.

    attempts: [{"model", "alias", "attempt", "trial", "data" (str or bytes body)}];
    unavailable: [{"model", "attempt", "trial"}] listed on the card without a body.
    """
    (pool / "trajectories" / task).mkdir(parents=True)  # always exists, may stay empty
    (pool / "tasks").mkdir()
    rows, records = [], []
    for item in attempts:
        relative = f"trajectories/{task}/{item['alias']}/{item['attempt']}/{task}_0.txt"
        path = pool / relative
        path.parent.mkdir(parents=True)
        data = item.get("data", body("missing header"))
        path.write_bytes(header(task, item["model"], item["attempt"]).encode() +
                         (data if isinstance(data, bytes) else data.encode()))
        path.chmod(0o444)
        rows.append((f"/pool/{relative}", f"- /pool/{relative} | score=0 | model={item['model']} | scored=True | "
                                          f"attempt={item['attempt']}"))
        digest = protocol.sha(path)
        records.append({"attempt_id": item["attempt"], "trial_id": item["trial"], "model_name": item["model"],
                        "status": "available", "pool_file": relative, "source_sha256": digest, "pool_sha256": digest})
    for item in unavailable:
        rows.append((item["attempt"], "- NO TRAJECTORY BODY AVAILABLE (official source confirmed) | score=0 | "
                                      f"model={item['model']} | scored=True | attempt={item['attempt']}"))
        records.append({"attempt_id": item["attempt"], "trial_id": item["trial"], "model_name": item["model"],
                        "status": "unavailable", "pool_file": None, "source_sha256": None, "pool_sha256": None})
    card = [f"# {task}", "", "## Preclassification (researcher-defined; not an official benchmark category)",
            "Class: C03 · Text processing and data transformation", "Fine family: text_sorting",
            "Transfer tags: sorting, text-files",
            "Use the current task constraints to judge applicability; shared category is not proof of transfer.", "",
            INSTRUCTION, "## Historical trajectories",
            f"{len(rows)} officially graded failed attempts at this task (official reward 0). Successful attempts are not included.",
            "Keep failures and partial runs. Check actual actions and observations before adopting a method.", ""]
    card += [line for _, line in sorted(rows)]
    (pool / "tasks" / f"{task}.md").write_text("\n".join(card) + "\n")
    manifest = {"schema_version": 1, "benchmark": "Terminal-Bench 2.1", "kind": "same_task_failed_official_trajectories",
                "task_id": task, "trajectory_count": len(attempts), "source_record_count": len(rows),
                "confirmed_unavailable_count": len(unavailable), "raw_unparsed_trajectory_count": 0,
                "successful_trajectories_included": False,
                "trajectory_format": "all-field ATIF text rendering; original raw/native fallbacks preserved byte-for-byte",
                "trajectory_path": "trajectories/<task_id>/<model_alias>/<attempt_id>/<task_id>_0.txt",
                "mutable_memory": "/memory", "cross_task_memory": False}
    (pool / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (pool / "README.md").write_text(
        "# TB2.1 V7 same-task pool\n\nOnly officially graded failed attempts at this task; no successes are included.\n"
        "The pool is immutable; mutable experience belongs in /memory for this task only.\n")
    for path in pool.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    return {"pool_manifest_sha256": protocol.sha(pool / "manifest.json"), "pool_files_sha256": protocol.inventory(pool),
            "trajectory_count": len(attempts), "source_record_count": len(rows), "excluded_count": 0,
            "records": records}


def rewrite(path: Path, data: bytes) -> None:
    path.chmod(0o644)
    path.write_bytes(data)


def provenance(task: str = TASK, **changes) -> dict:
    value = {"builder_model_requested": "claude-opus-5-5", "builder_effort_requested": "high",
             "models_used": ["claude-opus-5-5"], "refusal_fallbacks": [], "strict_model_audit_verified": True,
             "source_selection": "failed_same_task_official_trajectories", "task_id": task}
    value.update(changes)
    return value


class SameTaskSeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool = self.root / "pool"
        make_pool(self.pool, TASK, [{"model": "gpt-x", "alias": "gpt-x", "attempt": "attempt-001",
                                     "trial": "0f8fad5b-d9cb-469f-a165-70867728950e"}])
        self.trajectory = self.pool / "trajectories" / TASK / "gpt-x/attempt-001" / f"{TASK}_0.txt"
        self.source = f"/pool/trajectories/{TASK}/gpt-x/attempt-001/{TASK}_0.txt"
        self.entry = {"slug": f"{TASK}-1-check-sort-errors", "title": "Check the sort command's error output",
                      "keywords": ["sort", "exit status", TASK], "applicability": f"Only the current task {TASK}.",
                      "procedure": ["Run the sort and inspect its exit status before claiming completion."],
                      "pitfalls": ["The attempt claimed success after a failing command."],
                      "verification": "Compare /app/out.txt with the instruction's required order.",
                      "status": "source_observed",
                      "sources": [{"path": self.source, "line_start": 9, "line_end": 17},
                                  {"path": f"/pool/tasks/{TASK}.md", "line_start": 1, "line_end": 1}],
                      "evidence_summary": "The failed attempt printed an error and still declared success."}
        self.seed = self.root / "seeds" / TASK

    def freeze(self, entries=None, **kwargs):
        options = {"pool": self.pool, "task_id": TASK, "provenance": provenance(), **kwargs}
        return v7_seed.create_seed(self.seed, entries or [self.entry], **options)

    def test_seed_validates_and_initializes_fresh_same_task_memory(self):
        manifest = self.freeze()
        self.assertEqual(v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK), manifest)
        self.assertEqual(manifest["kind"], "same_task_failure_memory_seed")
        self.assertEqual(manifest["task_id"], TASK)
        self.assertNotIn("training_task_ids", manifest)
        self.assertEqual(manifest["pool_manifest_sha256"], protocol.sha(self.pool / "manifest.json"))
        self.assertNotIn("manifest.json", manifest["files_sha256"])
        memory = self.root / "memory"
        audit = v7_seed.initialize_same_task_memory(memory, TASK, self.seed, pool=self.pool)
        self.assertTrue(audit["valid"], audit["issues"])
        self.assertEqual(audit["event_count"], 0)
        self.assertEqual(audit["entry_count"], manifest["entry_count"])
        self.assertEqual(audit["inherited_entries"], manifest["entry_count"])
        digest = hashlib.sha256((self.seed / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(audit["seed_manifest_sha256"], digest)
        self.assertEqual(set(audit["initial_seed_file_hashes"]), {f"entries/{self.entry['slug']}.md"})
        independent = audit_memory(memory)
        self.assertTrue(independent["valid"])
        self.assertEqual((independent["event_count"], independent["entry_count"]), (0, manifest["entry_count"]))
        identity = json.loads((memory / "identity.json").read_text())
        self.assertEqual((identity["task_id"], identity["scope"]), (TASK, "single_test_task"))
        self.assertEqual(identity["seed_scope"], "frozen_same_task_failures")
        self.assertEqual(identity["seed_manifest_sha256"], digest)
        self.assertEqual(identity["inherited_entries"], 1)
        name = f"entries/{self.entry['slug']}.md"
        self.assertEqual((memory / name).read_bytes(), (self.seed / name).read_bytes())
        self.assertFalse((memory / "events.jsonl").exists())
        self.assertFalse((self.seed / "identity.json").exists())
        text = (self.seed / name).read_text()
        self.assertIn("Inherited from this task's frozen failure analysis, written before testing from its own "
                      "failed official attempts.", text)
        index = (self.seed / "MEMORY.md").read_text().splitlines()
        self.assertEqual(index[:4], ["# Frozen same-task failure analysis", "",
                                     "Written only from this task's officially failed attempts; contains no successful "
                                     "attempt and no test-task state.",
                                     "Observations and proposed checks do not prove a repair succeeds on the current task."])
        self.assertEqual((self.seed / "memory_summary.md").read_text().splitlines()[0],
                         "# Same-task failure analysis navigation")

    def test_seed_is_bound_to_its_own_task(self):
        self.freeze()
        with self.assertRaisesRegex(ValueError, "another task"):
            v7_seed.validate_seed(self.seed, pool=self.pool, task_id="beta-task")
        with self.assertRaisesRegex(ValueError, "another task"):
            v7_seed.initialize_same_task_memory(self.root / "beta-memory", "beta-task", self.seed, pool=self.pool)
        self.assertFalse((self.root / "beta-memory").exists())
        other_pool = self.root / "beta-pool"
        make_pool(other_pool, "beta-task", [{"model": "gpt-x", "alias": "gpt-x", "attempt": "attempt-001",
                                             "trial": "1f8fad5b-d9cb-469f-a165-70867728950e"}])
        with self.assertRaisesRegex(ValueError, "failure-only same-task pool"):
            v7_seed.validate_seed(self.seed, pool=other_pool, task_id=TASK)
        with self.assertRaisesRegex(ValueError, "another task"):
            v7_seed.create_seed(self.root / "x", [self.entry], pool=self.pool, task_id=TASK,
                                provenance=provenance("beta-task"))
        with self.assertRaises(ValueError):  # another task's pool cannot host this task's sources
            v7_seed.create_seed(self.root / "y", [self.entry], pool=other_pool, task_id="beta-task",
                                provenance=provenance("beta-task"))

    def test_sources_outside_this_task_pool_are_refused_even_if_present(self):
        planted = {f"trajectories/beta-task/gpt-x/attempt-001/beta-task_0.txt": "beta-task",
                   f"trajectories/beta-task/gpt-x/attempt-002/{TASK}_0.txt": TASK,
                   f"trajectories/{TASK}/gpt-x/attempt-001/{TASK}_1.txt": TASK,
                   f"trajectories/{TASK}/gpt-x/0f8fad5b-d9cb-469f-a165-70867728950e/{TASK}_0.txt": TASK,
                   "tasks/beta-task.md": "beta-task"}
        for relative, owner in planted.items():
            target = self.pool / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(header(owner, "gpt-x", "attempt-001").encode() + body("x").encode())
        paths = ["/evidence/out.txt", str(self.trajectory), "/pool/README.md", "/pool/manifest.json",
                 "/tmp_workspace/notes.txt", *("/pool/" + relative for relative in planted),
                 f"/pool/trajectories/{TASK}/attempt-001/{TASK}_0.txt",
                 f"/pool/trajectories/{TASK}/gpt-x/attempt-001/extra/{TASK}_0.txt",
                 f"/pool//trajectories/{TASK}/gpt-x/attempt-001/{TASK}_0.txt",
                 f"/pool/trajectories/{TASK}/gpt-x/../gpt-x/attempt-001/{TASK}_0.txt",
                 self.source[1:], f"/pool/tasks/{TASK}.md/", f"/pool/tasks/{TASK}.txt"]
        for path in paths:
            with self.subTest(path=path):
                entry = copy.deepcopy(self.entry)
                entry["sources"] = [{"path": path, "line_start": 1, "line_end": 1}]
                with self.assertRaises(ValueError):
                    self.freeze([entry])
                self.assertFalse(self.seed.exists())

    def test_card_citations_are_allowed_and_range_checked(self):
        entry = copy.deepcopy(self.entry)
        entry["sources"] = [{"path": f"/pool/tasks/{TASK}.md", "line_start": 1, "line_end": 9}]
        manifest = self.freeze([entry])
        catalog = json.loads((self.seed / ".entry_metadata.json").read_text())
        source = catalog[entry["slug"]]["sources"][0]
        card = (self.pool / "tasks" / f"{TASK}.md").read_bytes()
        self.assertEqual(source["sha256"], hashlib.sha256(card).hexdigest())
        self.assertEqual(source["lines_sha256"], hashlib.sha256(b"".join(card.splitlines(True)[:9])).hexdigest())
        self.assertEqual(manifest["entry_count"], 1)
        entry["sources"][0]["line_end"] = len(card.splitlines()) + 1
        with self.assertRaisesRegex(ValueError, "out of bounds"):
            v7_seed.create_seed(self.root / "other", [entry], pool=self.pool, task_id=TASK, provenance=provenance())

    def test_nul_bytes_are_allowed_only_outside_the_cited_window(self):
        original = self.trajectory.read_bytes()
        data = original + b"observation:\n  binary dump \x00\x00\x01\n"
        rewrite(self.trajectory, data)
        self.freeze()
        catalog = json.loads((self.seed / ".entry_metadata.json").read_text())
        source = catalog[self.entry["slug"]]["sources"][0]
        self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(source["lines_sha256"], hashlib.sha256(b"".join(data.splitlines(True)[8:17])).hexdigest())
        v7_seed.initialize_same_task_memory(self.root / "memory", TASK, self.seed, pool=self.pool)
        # Even an uncited byte stays bound by the whole-file digest.
        rewrite(self.trajectory, data.replace(b"\x00\x00\x01", b"\x00\x00\x02"))
        with self.assertRaisesRegex(ValueError, "provenance"):
            v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
        rewrite(self.trajectory, original.replace(b"process exited 1", b"process exited \x00"))
        with self.assertRaisesRegex(ValueError, "NUL"):
            v7_seed.create_seed(self.root / "nul", [self.entry], pool=self.pool, task_id=TASK, provenance=provenance())
        rewrite(self.trajectory, original + b"raw bytes \xff\xfe\n")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            v7_seed.create_seed(self.root / "utf8", [self.entry], pool=self.pool, task_id=TASK, provenance=provenance())

    def test_trajectory_header_must_be_this_tasks_scored_failure(self):
        original = self.trajectory.read_bytes()
        variants = {"training title": original.replace(b"Historical failed attempt at this task", b"Historical training trajectory"),
                    "other task": original.replace(f"this task: {TASK}".encode(), b"this task: beta-task"),
                    "success": original.replace(b"Official reward: 0;", b"Official reward: 1;"),
                    "unscored": original.replace(b"Official scored execution: True", b"Official scored execution: False"),
                    "no header": body("x").encode() * 3}
        for name, data in variants.items():
            with self.subTest(name=name):
                rewrite(self.trajectory, data)
                with self.assertRaisesRegex(ValueError, "scored failed attempt"):
                    self.freeze()
                self.assertFalse(self.seed.exists())

    def test_provenance_accepts_a_recorded_fallback_and_rejects_dishonest_records(self):
        fallback = provenance(models_used=["claude-opus-4-8", "claude-opus-5-5"], strict_model_audit_verified=False,
                              refusal_fallbacks=[{"original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8",
                                                  "api_refusal_category": "bio"}],
                              output_share_by_model={"claude-opus-5-5": 0.001, "claude-opus-4-8": 0.999})
        manifest = self.freeze(provenance=fallback)
        self.assertFalse(manifest["provenance"]["strict_model_audit_verified"])
        v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
        bad = [{key: value for key, value in provenance().items() if key != missing} for missing in provenance()]
        bad += [provenance(builder_model_requested="claude-sonnet-5-5"), provenance(builder_effort_requested="medium"),
                provenance(models_used=[]), provenance(models_used=["claude-opus-5-5", "claude-opus-5-5"]),
                provenance(models_used="claude-opus-5-5"), provenance(refusal_fallbacks={}),
                provenance(strict_model_audit_verified="yes"), provenance(strict_model_audit_verified=1),
                provenance(strict_model_audit_verified=True, refusal_fallbacks=fallback["refusal_fallbacks"]),
                provenance(models_used=["claude-opus-4-8"]),
                provenance(source_selection="failed_training_trajectories"), provenance(task_id="beta-task"),
                provenance(output_tokens=float("nan"))]
        for number, value in enumerate(bad):
            with self.subTest(number=number), self.assertRaises(ValueError):
                v7_seed.create_seed(self.root / f"bad-{number}", [self.entry], pool=self.pool, task_id=TASK,
                                    provenance=value)

    def test_tampering_and_unrelated_files_are_refused(self):
        self.freeze()
        manifest_path = self.seed / "manifest.json"
        pristine = {path: path.read_bytes() for path in self.seed.rglob("*") if path.is_file()}

        def restore():
            for path in list(self.seed.rglob("*")):
                if path.is_file() and path not in pristine:
                    path.unlink()
            for path, data in pristine.items():
                path.write_bytes(data)

        def rehash(name):
            manifest = json.loads(manifest_path.read_text())
            manifest["files_sha256"][name] = hashlib.sha256((self.seed / name).read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))

        for name, content in (("events.jsonl", "{}\n"), ("identity.json", "{}\n"), ("entries/extra.md", "# x\n")):
            with self.subTest(name=name):
                (self.seed / name).write_text(content)
                rehash(name)
                with self.assertRaisesRegex(ValueError, "unrelated files|catalog|differs"):
                    v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
                restore()
        entry = self.seed / f"entries/{self.entry['slug']}.md"
        entry.write_text(entry.read_text() + "\nUse the reference solution.\n")
        with self.assertRaises(ValueError):
            v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
        restore()
        catalog_path = self.seed / ".entry_metadata.json"
        catalog = json.loads(catalog_path.read_text())
        catalog[self.entry["slug"]]["sources"][0]["lines_sha256"] = "0" * 64
        catalog_path.write_text(json.dumps(catalog))
        rehash(".entry_metadata.json")
        with self.assertRaisesRegex(ValueError, "provenance"):
            v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
        restore()
        catalog[self.entry["slug"]]["sources"] = {"path": self.source}
        catalog_path.write_text(json.dumps(catalog))
        rehash(".entry_metadata.json")
        with self.assertRaisesRegex(ValueError, "metadata"):
            v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
        restore()
        v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)
        manifest = json.loads((self.pool / "manifest.json").read_text())
        manifest["successful_trajectories_included"] = True
        rewrite(self.pool / "manifest.json", json.dumps(manifest).encode())
        with self.assertRaisesRegex(ValueError, "failure-only"):
            v7_seed.validate_seed(self.seed, pool=self.pool, task_id=TASK)

    def test_status_must_not_claim_local_verification(self):
        for status in ("locally_verified", "contradicted"):
            with self.subTest(status=status):
                entry = {**copy.deepcopy(self.entry), "status": status}
                with self.assertRaisesRegex(ValueError, "status"):
                    self.freeze([entry])

    def test_initialization_requires_a_fresh_memory_and_never_overwrites_a_seed(self):
        self.freeze()
        memory = self.root / "memory"
        memory.mkdir()
        (memory / "notes.txt").write_text("previous run")
        with self.assertRaisesRegex(ValueError, "fresh empty"):
            v7_seed.initialize_same_task_memory(memory, TASK, self.seed, pool=self.pool)
        v7_seed.initialize_same_task_memory(self.root / "fresh", TASK, self.seed, pool=self.pool)
        with self.assertRaisesRegex(ValueError, "fresh empty"):
            v7_seed.initialize_same_task_memory(self.root / "fresh", TASK, self.seed, pool=self.pool)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.freeze()

    def test_carriage_returns_in_model_text_survive_cloning_byte_for_byte(self):
        entry = copy.deepcopy(self.entry)
        entry["evidence_summary"] = "Progress bar output:\r50%\r100%\r\nthen the command failed."
        self.freeze([entry])
        memory = self.root / "memory"
        audit = v7_seed.initialize_same_task_memory(memory, TASK, self.seed, pool=self.pool)
        self.assertTrue(audit["valid"])
        name = f"entries/{entry['slug']}.md"
        self.assertEqual((memory / name).read_bytes(), (self.seed / name).read_bytes())
        self.assertIn(b"\r50%", (memory / name).read_bytes())

    def test_attempt_ids_follow_the_zero_padded_counter(self):
        for attempt, accepted in (("attempt-1000", True), ("attempt-12", False)):
            with self.subTest(attempt=attempt):
                path = self.pool / "trajectories" / TASK / "gpt-x" / attempt / f"{TASK}_0.txt"
                path.parent.mkdir(parents=True)
                path.write_bytes(header(TASK, "gpt-x", attempt).encode() + body("x").encode())
                entry = copy.deepcopy(self.entry)
                entry["sources"] = [{"path": f"/pool/trajectories/{TASK}/gpt-x/{attempt}/{TASK}_0.txt",
                                     "line_start": 9, "line_end": 12}]
                destination = self.root / f"seed-{attempt}"
                if accepted:
                    v7_seed.create_seed(destination, [entry], pool=self.pool, task_id=TASK, provenance=provenance())
                else:
                    with self.assertRaisesRegex(ValueError, "seed sources must be"):
                        v7_seed.create_seed(destination, [entry], pool=self.pool, task_id=TASK, provenance=provenance())

    def test_symlinked_sources_or_seed_paths_are_refused(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(self.trajectory.read_bytes())
        link = self.pool / "trajectories" / TASK / "gpt-y" / "attempt-002"
        link.mkdir(parents=True)
        os.symlink(outside, link / f"{TASK}_0.txt")
        entry = copy.deepcopy(self.entry)
        entry["sources"] = [{"path": f"/pool/trajectories/{TASK}/gpt-y/attempt-002/{TASK}_0.txt",
                             "line_start": 1, "line_end": 1}]
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.freeze([entry])


if __name__ == "__main__":
    unittest.main()
