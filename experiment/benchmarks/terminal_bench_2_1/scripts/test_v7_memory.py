"""No-model tests for the V7 Opus failure-analysis builder, its validation, conversion and export."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from experiment.benchmarks.terminal_bench_2_1.scripts import build_failure_memory as v6
from experiment.benchmarks.terminal_bench_2_1.scripts import protocol, v7_memory, v7_seed
from experiment.benchmarks.terminal_bench_2_1.scripts.test_v7_seed import body, make_pool, rewrite
from experiment.shared.memory.tools import _entry, _validate, audit_memory

BUILD = "test-build"
TOKEN = "fake-oauth-token-for-tests-only"
HUB_IDS = {"alpha-1": "7d9f3e7a-1111-4c7e-9a40-3b1e2c4d5e6f", "alpha-2": "7d9f3e7a-2222-4c7e-9a40-3b1e2c4d5e6f",
           "alpha-3": "7d9f3e7a-3333-4c7e-9a40-3b1e2c4d5e6f", "beta-1": "7d9f3e7a-4444-4c7e-9a40-3b1e2c4d5e6f"}
A1 = "/pool/trajectories/alpha-task/gpt-5.5/attempt-001/alpha-task_0.txt"
A2 = "/pool/trajectories/alpha-task/claude-x-1/attempt-002/alpha-task_0.txt"
B1 = "/pool/trajectories/beta-task/gpt-5.5/attempt-001/beta-task_0.txt"
LONG = body("missing header") + "".join(f"  [{n}]\n    observation:\n      step {n} output\n" for n in range(2, 40))
HIDDEN_SOLUTION = "#!/bin/bash\n" + "".join(f"tr -s ' ' '\\n' < /app/in.txt | LC_ALL=C sort -k{n} -t: > /tmp/stage{n}\n"
                                            for n in range(1, 9))


def default_tasks() -> dict:
    return {"alpha-task": ([{"model": "gpt-5.5", "alias": "gpt-5.5", "attempt": "attempt-001", "trial": HUB_IDS["alpha-1"]},
                            {"model": "claude-x 1", "alias": "claude-x-1", "attempt": "attempt-002",
                             "trial": HUB_IDS["alpha-2"], "data": LONG}],
                           [{"model": "gpt-5.5", "attempt": "attempt-003", "trial": HUB_IDS["alpha-3"]}]),
            "beta-task": ([{"model": "gpt-5.5", "alias": "gpt-5.5", "attempt": "attempt-001", "trial": HUB_IDS["beta-1"]}], []),
            "empty-task": ([], [])}


def make_corpus(base: Path, tasks: dict | None = None) -> Path:
    """A synthetic v7_corpus build: index.json, audit/ and per-task pools."""
    corpus = base / "prepared/v7_corpus" / BUILD
    records = {}
    for task, (attempts, unavailable) in (tasks or default_tasks()).items():
        record = make_pool(corpus / "pools" / task, task, attempts, unavailable)
        records[task] = {"pool_path": f"prepared/v7_corpus/{BUILD}/pools/{task}", **record}
        (corpus / "audit").mkdir(parents=True, exist_ok=True)
        (corpus / "audit" / f"{task}.json").write_text(json.dumps({"task_id": task, "records": record["records"]}))
    index = {"schema_version": 1, "kind": "v7_same_task_corpus", "build_id": BUILD,
             "created_at": "2026-10-07T00:00:00+00:00", "split_sha256": "a" * 64, "source_manifest_sha256": "b" * 64,
             "download_audit_sha256": "c" * 64, "decisions_sha256": None, "tasks": records}
    (corpus / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return corpus


def hidden_task(base: Path, task: str) -> None:
    """The host-only task directory the export gate compares analyses against; no builder ever sees it."""
    root = base / "prepared/tasks" / task
    (root / "tests").mkdir(parents=True)
    (root / "solution").mkdir()
    (root / "instruction.md").write_text("Sort the words of /app/in.txt into /app/out.txt, one per line.\n")
    (root / "tests/test_outputs.py").write_text("def test_sorted():\n    assert read_words() == sorted(WORDS)\n")
    (root / "solution/solve.sh").write_text(HIDDEN_SOLUTION)


def reindex(corpus: Path, task: str) -> None:
    """Make index.json agree with a mutated pool so that only the content rule can refuse it."""
    pool = corpus / "pools" / task
    index = json.loads((corpus / "index.json").read_text())
    record = index["tasks"][task]
    record.update(pool_files_sha256=protocol.inventory(pool), pool_manifest_sha256=protocol.sha(pool / "manifest.json"))
    (corpus / "index.json").write_text(json.dumps(index, indent=2) + "\n")


def analysis(task: str = "alpha-task", first: str = A1, second: str = A2) -> dict:
    """A canned, valid builder output; two citations drop a prefix that validation must complete."""
    one = {"path": first, "line_start": 9, "line_end": 17}
    two = {"path": second[len("/pool/"):], "line_start": 9, "line_end": 12}
    card = {"path": f"/pool/tasks/{task}.md", "line_start": 1, "line_end": 3}
    return {
        "task_summary": "Sort the words of /app/in.txt into /app/out.txt, one per line.",
        "decision_guide": {"first_steps": ["Read /app/in.txt and confirm its encoding."],
                           "avoid": ["Declaring success after a non-zero exit."],
                           "before_finishing": ["Compare /app/out.txt with a fresh sort of the input."]},
        "requirements": [{"requirement": "Write /app/out.txt with one word per line.",
                          "how_attempts_handled_it": "Both attempts wrote the file; one crashed first.", "risk": "high"}],
        "approach_families": [{"name": "coreutils sort", "attempt_count": 2, "furthest_progress": "Ran sort once.",
                               "where_it_broke": "sort reported an error.", "verdict": "execution_likely_wrong",
                               "evidence": "Both transcripts show the failing command.", "sources": [one, two]}],
        "shared_assumptions": [{"assumption": "The default locale order is right.",
                                "why_suspect": "The instruction may require byte order.", "sources": [card]}],
        "verification_gaps": [{"check_used": "Viewing the first lines.", "what_it_misses": "Ordering errors later on."}],
        "lessons": [{"title": "Check sort's exit status", "keywords": ["sort", "exit status"],
                     "failure_mode": "verification_gap", "attempts_affected": 2,
                     "observed_failure": "An attempt printed an error and still declared success.",
                     "cause_hypothesis": "The failing command was never checked.", "cause_confidence": "observed",
                     "what_to_do_instead": "Check the exit status and rerun the sort.",
                     "check_that_would_catch_it": "Run `echo $?` right after sort.", "sources": [one]},
                    {"title": "Locale may change the order", "keywords": ["locale", "LC_ALL"],
                     "failure_mode": "spec_misread", "attempts_affected": 1,
                     "observed_failure": "Mixed-case words were ordered case-insensitively.",
                     "cause_hypothesis": "A UTF-8 locale collated the words.", "cause_confidence": "hypothesis",
                     "what_to_do_instead": "Decide the required order from the instruction, then set LC_ALL.",
                     "check_that_would_catch_it": "Check the order of 'B' and 'a' in the output.",
                     "sources": [{"path": f"pool/tasks/{task}.md", "line_start": 9, "line_end": 9}]}],
        "reusable_details": [{"what": "Input path", "detail": "/app/in.txt is the only input.", "caveat": "",
                              "sources": [one]},
                             {"what": "Exit check", "detail": "sort exits 2 on a read error.",
                              "caveat": "Seen in one attempt only.", "sources": [two]}],
        "unexplained": "Why the second attempt stopped early.",
        "reviewed_sources": [first, second]}


def beta_analysis() -> dict:
    value = analysis("beta-task", B1, B1)
    value["approach_families"][0]["attempt_count"] = 1
    return value


def invalid_analysis() -> dict:
    value = analysis()
    value["lessons"][0]["sources"] = [{"path": B1, "line_start": 1, "line_end": 2}]  # another task's attempt
    return value


def fake_support(root: Path, **changes) -> dict:
    return {"schema_version": 1, "image": v6.DEFAULT_IMAGE, "image_id": "sha256:" + "1" * 64,
            "python_root": str(root / "python"), "claude_version": "2.1.292 (Claude Code)", "ca_sha256": "2" * 64,
            **changes}


class FakeEngine:
    """Writes a canned Claude Code stream-json transcript per task; never calls a model or Docker."""

    def __init__(self, **plans):
        self.plans = {task: list(items) for task, items in plans.items()}
        self.requests = []
        self.observed = []
        self.lock = threading.Lock()

    def __call__(self, request: v7_memory.EngineRequest) -> dict:
        with self.lock:
            self.requests.append(request)
            plan = self.plans[request.task_id].pop(0)
        # What the container would see at /work before the model starts.
        self.observed.append({"task_id": request.task_id, "work": sorted(p.name for p in request.work.iterdir()),
                              "schema": (request.work / "output_schema.json").read_text()})
        events = [{"type": "system", "subtype": "init", "model": plan.get("init_model", "claude-opus-5-5"), "cwd": "/work"}]
        events += [{"type": "system", "subtype": "model_refusal_fallback", "direction": "retry", **item}
                   for item in plan.get("fallbacks", [])]
        for model in plan.get("writers", ["claude-opus-5-5"]):
            events.append({"type": "assistant", "message": {"model": model, "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": plan.get("command", "cd /pool && grep -rn error .")}}]}})
        if "error_result" in plan:
            events.append({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                           "result": plan["error_result"], "api_error_status": plan.get("api_error_status"),
                           "usage": {"input_tokens": 0, "output_tokens": 0}, "total_cost_usd": 0})
        if "result" in plan:
            events.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": 5,
                           "structured_output": plan["result"], "result": "done",
                           "modelUsage": plan.get("usage", {"claude-opus-5-5": {"outputTokens": 900, "costUSD": 0.75}}),
                           "usage": {"input_tokens": 12, "output_tokens": 900}, "total_cost_usd": plan.get("cost", 0.75)})
        (request.attempt / "claude.events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
        (request.attempt / "builder.stderr.log").write_text("")
        if "summary" in plan:
            (request.work / "summary.json").write_text(json.dumps(plan["summary"]))
            if plan.get("stale_summary"):
                os.utime(request.work / "summary.json", (1000.0, 1000.0))
        if plan.get("tamper"):
            (request.work / "output_schema.json").write_text("{}")
        return {"container_name": request.container_name, "exit_code": plan.get("exit_code", 0),
                "timed_out": plan.get("timed_out", False), "execution_error": None, "cleanup_error": None,
                "elapsed_seconds": 1.5}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "benchmark"
        self.corpus = make_corpus(self.base)
        self.work = self.base / "runs/v7_memory_build_test"
        for task in default_tasks():
            hidden_task(self.base, task)
        self.token_requests = []
        verifier = patch.object(v7_memory, "_corpus_verifier", return_value=None)
        verifier.start()
        self.addCleanup(verifier.stop)

    def tokens(self, seconds):
        self.token_requests.append(seconds)
        return TOKEN

    def build(self, engine, **options):
        options = {"base": self.base, "engine": engine, "support_fn": lambda: fake_support(self.root),
                   "token_provider": self.tokens, "workers": 2, **options}
        with contextlib.redirect_stdout(io.StringIO()):
            return v7_memory.build(self.corpus, self.work, **options)

    def job(self, task="alpha-task"):
        plan = v7_memory.build_plan(self.corpus, base=self.base)
        return next(job for job in plan["jobs"] if job["task_id"] == task)

    def attempt(self, task: str, number: int) -> dict:
        return json.loads((self.work / f"jobs/{task}/attempt-{number:03d}/attempt.json").read_text())

    def all_text(self, root: Path) -> str:
        return "\n".join(path.read_bytes().decode("utf-8", errors="replace")
                         for path in sorted(root.rglob("*")) if path.is_file())

    # ----- schema, plan and pool verification -----

    def test_output_schema_is_closed_and_adds_bounded_lesson_keywords(self):
        schema = v7_memory.output_schema()

        def walk(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(node["required"], sorted(node["properties"]))
                for child in node["properties"].values():
                    walk(child)
            elif node.get("type") == "array":
                walk(node["items"])
        walk(schema)
        self.assertEqual(set(schema["properties"]), {
            "task_summary", "decision_guide", "requirements", "approach_families", "shared_assumptions",
            "verification_gaps", "lessons", "reusable_details", "unexplained", "reviewed_sources"})
        lessons = schema["properties"]["lessons"]
        self.assertEqual(lessons["maxItems"], 8)
        self.assertEqual(lessons["items"]["properties"]["keywords"],
                         {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 60},
                          "minItems": 1, "maxItems": 8})
        self.assertEqual(json.loads(v7_memory.schema_text()), schema)

    def test_plan_prints_anonymized_jobs_and_writes_nothing(self):
        before = {path: path.stat().st_mtime_ns for path in self.root.rglob("*")}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(v7_memory.main(["plan", "--corpus", f"prepared/v7_corpus/{BUILD}", "--base", str(self.base)]), 0)
        self.assertEqual({path: path.stat().st_mtime_ns for path in self.root.rglob("*")}, before)
        printed = json.loads(output.getvalue())
        self.assertEqual([job["task_id"] for job in printed["jobs"]], ["alpha-task", "beta-task"])
        self.assertEqual(printed["tasks_without_trajectories"], ["empty-task"])
        self.assertEqual(printed["jobs"][0]["models"], "claude-x 1 x1, gpt-5.5 x1")
        plan = v7_memory.build_plan(self.corpus, base=self.base)
        alpha = plan["jobs"][0]
        self.assertEqual(alpha["attempt_count"], 2)
        for record in alpha["records"]:
            self.assertEqual(list(record), ["path", "trial_id", "status", "line_count", "pool_file", "sha256", "bytes"])
            self.assertEqual(record["status"], "available")
            self.assertRegex(record["trial_id"], r"^attempt-\d{3}$")
            data = (self.corpus / "pools/alpha-task" / record["pool_file"]).read_bytes()
            self.assertEqual(record["line_count"], len(data.splitlines()))
            self.assertEqual(record["path"], "/pool/" + record["pool_file"])
        self.assertEqual(v6.job_budget(alpha["records"])["timeout_seconds"], alpha["timeout_seconds"])
        self.assertEqual(alpha["card"]["path"], "/pool/tasks/alpha-task.md")
        dumped = json.dumps(plan)
        for hub_id in HUB_IDS.values():
            self.assertNotIn(hub_id, dumped)

    def test_pool_verification_refuses_success_test_and_cross_task_material(self):
        pool = self.corpus / "pools/alpha-task"
        trajectory = pool / A1[len("/pool/"):]
        good = trajectory.read_bytes()
        card_path = pool / "tasks/alpha-task.md"
        card = card_path.read_text()

        def plant(relative, data=good):
            target = pool / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        # (mutation, rewrite manifest/card/index to agree with the planted files, expected refusal)
        cases = {
            "success file": (lambda: plant("trajectories/alpha-task/gpt-5.5/attempt-001/alpha-task_1.txt"), True,
                             "unexpected pool file"),
            "unscored file": (lambda: plant("trajectories/alpha-task/gpt-5.5/attempt-001/alpha-task_unscored.txt"),
                              True, "unexpected pool file"),
            "hub trial directory": (lambda: plant(f"trajectories/alpha-task/gpt-5.5/{HUB_IDS['alpha-1']}/alpha-task_0.txt"),
                                    True, "unexpected pool file"),
            "other task directory": (lambda: plant("trajectories/beta-task/gpt-5.5/attempt-001/beta-task_0.txt"), True,
                                     "only this task's card and trajectory directory"),
            "other task card": (lambda: plant("tasks/beta-task.md", b"# beta-task\n"), True, "only this task's card"),
            "hidden tests": (lambda: plant("tests/test_outputs.py", b"def test_x(): pass\n"), True,
                             "unexpected pool top level"),
            "empty solution directory": (lambda: (pool / "solution").mkdir(), True, "unexpected pool top level"),
            "special file": (lambda: os.mkfifo(pool / "trajectories/alpha-task/gpt-5.5/pipe"), False,
                             "link or special file"),
            "symlink": (lambda: os.symlink(trajectory, pool / "trajectories/alpha-task/gpt-5.5/link_0.txt"), False,
                        "link or special file"),
            "training header": (lambda: rewrite(trajectory, good.replace(b"Historical failed attempt at this task",
                                                                        b"Historical training trajectory")), True,
                                "trajectory header"),
            "trial id header": (lambda: rewrite(trajectory, good.replace(b"attempt ID: attempt-001",
                                                                        f"trial ID: {HUB_IDS['alpha-1']}".encode())),
                                True, "trajectory header"),
            "success header": (lambda: rewrite(trajectory, good.replace(b"Official reward: 0;", b"Official reward: 1;")),
                               True, "trajectory header"),
            "unscored header": (lambda: rewrite(trajectory, good.replace(b"execution: True", b"execution: False")), True,
                                "trajectory header"),
            "card success row": (lambda: rewrite(card_path, card.replace("score=0 | model=claude-x 1",
                                                                         "score=1 | model=claude-x 1").encode()),
                                 False, "card row"),
            "card trial id": (lambda: rewrite(card_path, card.replace("attempt=attempt-002",
                                                                      f"attempt=attempt-002 | trial={HUB_IDS['alpha-2']}").encode()),
                              False, "card row"),
            "card success count": (lambda: rewrite(card_path, card.replace(
                "Successful attempts are not included.", "110 version-compatible source records.").encode()),
                                   False, "failed-attempt count"),
            "card lists a missing file": (lambda: rewrite(card_path, card.replace("claude-x-1/attempt-002",
                                                                                  "claude-x-1/attempt-005").encode()),
                                          False, "rows differ"),
            "manifest success flag": (lambda: self._manifest(pool, successful_trajectories_included=True), False,
                                      "failure-only manifest"),
            "manifest extra key": (lambda: self._manifest(pool, success_count=4), False, "failure-only manifest"),
            "manifest count": (lambda: self._manifest(pool, trajectory_count=3), False, "count differs"),
        }
        pristine = self.root / "pristine"
        shutil.copytree(self.corpus, pristine)
        for name, (mutate, consistent, message) in cases.items():
            with self.subTest(name=name):
                mutate()
                if consistent:
                    self._consistent(pool)
                elif name not in {"special file", "symlink"}:
                    reindex(self.corpus, "alpha-task")
                with self.assertRaisesRegex(ValueError, message):
                    v7_memory.build_plan(self.corpus, base=self.base)
                shutil.rmtree(self.corpus)
                shutil.copytree(pristine, self.corpus)
        v7_memory.build_plan(self.corpus, base=self.base)
        index = json.loads((self.corpus / "index.json").read_text())
        index["tasks"]["alpha-task"]["pool_manifest_sha256"] = "0" * 64
        (self.corpus / "index.json").write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "frozen corpus index"):
            v7_memory.build_plan(self.corpus, base=self.base)

    def _consistent(self, pool: Path) -> None:
        """Rewrite manifest counts, card rows and the index to agree with every planted file,
        so that only the content rule under test can refuse the pool."""
        files = sorted(path.relative_to(pool).as_posix() for path in (pool / "trajectories").rglob("*") if path.is_file())
        lines = (pool / "tasks/alpha-task.md").read_text().split("\n")
        heading = max(n for n, line in enumerate(lines) if line == "## Historical trajectories")
        rows = [line for line in lines[heading + 1:] if line.startswith("- NO TRAJECTORY BODY")]
        rows += [f"- /pool/{name} | score=0 | model=gpt-5.5 | scored=True | attempt=attempt-00{n}"
                 for n, name in enumerate(files, 4)]
        lines = lines[:heading + 1] + [
            f"{len(rows)} officially graded failed attempts at this task (official reward 0). Successful attempts are not included.",
            "Keep failures and partial runs. Check actual actions and observations before adopting a method.", "",
            *sorted(rows), ""]
        rewrite(pool / "tasks/alpha-task.md", "\n".join(lines).encode())
        self._manifest(pool, trajectory_count=len(files), source_record_count=len(rows))
        index = json.loads((self.corpus / "index.json").read_text())
        index["tasks"]["alpha-task"]["trajectory_count"] = len(files)
        (self.corpus / "index.json").write_text(json.dumps(index))
        reindex(self.corpus, "alpha-task")

    def _manifest(self, pool: Path, **changes):
        manifest = json.loads((pool / "manifest.json").read_text())
        manifest.update(changes)
        rewrite(pool / "manifest.json", json.dumps(manifest).encode())

    def test_extracted_images_are_allowed_and_part_a_verifier_is_also_applied(self):
        image = self.corpus / "pools/alpha-task/images/attempt-001" / ("ab" * 32 + ".png")
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG\r\n")
        reindex(self.corpus, "alpha-task")
        calls = []
        with patch.object(v7_memory, "_corpus_verifier", return_value=lambda *args: calls.append(args)):
            v7_memory.build_plan(self.corpus, base=self.base)
        self.assertEqual([(args[0].name, args[1]) for args in calls],
                         [("alpha-task", "alpha-task"), ("beta-task", "beta-task"), ("empty-task", "empty-task")])
        self.assertEqual(calls[0][2]["pool_path"], f"prepared/v7_corpus/{BUILD}/pools/alpha-task")

    # ----- validation -----

    def test_validation_completes_only_missing_prefixes_and_never_edits_the_input(self):
        job = self.job()
        raw = analysis()
        frozen = copy.deepcopy(raw)
        checked, completions = v7_memory.validate_analysis(raw, job)
        self.assertEqual(raw, frozen)
        self.assertEqual([(item["section"], item["original"], item["completed"]) for item in completions],
                         [("approach_families", A2[len("/pool/"):], A2),
                          ("lessons", "pool/tasks/alpha-task.md", "/pool/tasks/alpha-task.md"),
                          ("reusable_details", A2[len("/pool/"):], A2)])
        self.assertEqual(checked["approach_families"][0]["sources"][1]["path"], A2)
        self.assertEqual(checked["lessons"][1]["sources"][0]["path"], "/pool/tasks/alpha-task.md")
        self.assertEqual(v7_memory.validate_analysis(json.loads(json.dumps(raw)), job), (checked, completions))
        count = next(record["line_count"] for record in job["records"] if record["path"] == A1)
        bad_paths = [B1, "/tests/test_outputs.py", "/solution/solve.sh", "/pool/README.md", "/pool/manifest.json",
                     "alpha-task_0.txt", "gpt-5.5/attempt-001/alpha-task_0.txt", "//pool" + A1[len("/pool"):],
                     "/pool/pool" + A1[len("/pool"):], "/pool/tasks/beta-task.md", A1.replace("_0.txt", "_1.txt")]
        for path in bad_paths:
            with self.subTest(path=path):
                value = analysis()
                value["lessons"][0]["sources"][0]["path"] = path
                with self.assertRaises(ValueError):
                    v7_memory.validate_analysis(value, job)
        mutations = [lambda v: v["lessons"][0]["sources"][0].update(line_end=count + 1),
                     lambda v: v["lessons"][0]["sources"][0].update(line_start=0),
                     lambda v: v["lessons"][0]["sources"][0].update(line_start=5, line_end=4),
                     lambda v: v["lessons"][0].pop("keywords"),
                     lambda v: v["lessons"][0].update(keywords=[]),
                     lambda v: v["lessons"][0].update(keywords=["k"] * 9),
                     lambda v: v["lessons"][0].update(keywords=["x" * 61]),
                     lambda v: v["lessons"][0].update(confidence="high"),
                     lambda v: v.update(lessons=v["lessons"] * 5),
                     lambda v: v.update(extra="field"),
                     lambda v: v["lessons"][0].update(failure_mode="grader_bug")]
        for number, mutate in enumerate(mutations):
            with self.subTest(mutation=number):
                value = analysis()
                mutate(value)
                with self.assertRaises(ValueError):
                    v7_memory.validate_analysis(value, job)

    def test_binary_lines_are_uncitable_and_never_reach_the_prompt(self):
        pool = self.corpus / "pools/alpha-task"
        path = pool / A1[len("/pool/"):]
        rewrite(path, path.read_bytes() + b"observation:\n  dump \x00\x01\n")
        reindex(self.corpus, "alpha-task")
        job = self.job()
        lines = next(record["line_count"] for record in job["records"] if record["path"] == A1)
        self.assertEqual(job["nul_lines"], {A1: [lines]})
        value = analysis()
        value["lessons"][0]["sources"][0].update(line_start=lines - 1, line_end=lines)
        # The window keeps its longest NUL-free run; the model's text and the input object stay untouched.
        checked, completions = v7_memory.validate_analysis(value, job)
        self.assertEqual((checked["lessons"][0]["sources"][0]["line_start"], checked["lessons"][0]["sources"][0]["line_end"]),
                         (lines - 1, lines - 1))
        trim = {"section": "lessons", "item": 0, "source": 0, "nul_trim": {"cited": [lines - 1, lines], "kept": [lines - 1, lines - 1]}}
        self.assertIn(trim, completions)
        self.assertEqual(value["lessons"][0]["sources"][0]["line_end"], lines)
        self.assertEqual(checked["lessons"][0]["observed_failure"], value["lessons"][0]["observed_failure"])
        only_binary = copy.deepcopy(value)
        only_binary["lessons"][0]["sources"][0].update(line_start=lines, line_end=lines)
        with self.assertRaisesRegex(ValueError, "only binary NUL lines"):
            v7_memory.validate_analysis(only_binary, job)
        engine = FakeEngine(**{"alpha-task": [{"result": value}]})
        self.build(engine, tasks=["alpha-task"])
        attempt = json.loads((self.work / "jobs/alpha-task/attempt-001/attempt.json").read_text())
        self.assertEqual(attempt["status"], "complete")
        self.assertIn(trim, attempt["path_completions"])
        self.assertGreater(attempt["packet_nul_replacements"], 0)
        self.assertNotIn("\x00", engine.requests[0].prompt)
        # The trimmed citation converts into a seed whose source windows are pure text.
        with contextlib.redirect_stdout(io.StringIO()):
            v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)
        v7_seed.validate_seed(self.corpus / "seeds/alpha-task", pool=self.corpus / "pools/alpha-task", task_id="alpha-task")

    def test_non_utf8_trajectory_is_mounted_and_counted_but_not_citable(self):
        tasks = default_tasks()
        tasks["alpha-task"][0].append({"model": "gpt-5.5", "alias": "gpt-5.5", "attempt": "attempt-004",
                                       "trial": "7d9f3e7a-5555-4c7e-9a40-3b1e2c4d5e6f", "data": b"raw \xff\xfe bytes\n"})
        shutil.rmtree(self.base)
        self.corpus = make_corpus(self.base, tasks)
        job = self.job()
        self.assertEqual(job["attempt_count"], 3)
        self.assertEqual(len(job["records"]), 2)
        self.assertEqual([item["reason"] for item in job["unreadable_trajectories"]], ["not_utf8"])
        self.assertEqual(job["models_text"], "gpt-5.5 x2, claude-x 1 x1")
        value = analysis()
        value["lessons"][0]["sources"][0]["path"] = "/pool/trajectories/alpha-task/gpt-5.5/attempt-004/alpha-task_0.txt"
        with self.assertRaisesRegex(ValueError, "outside"):
            v7_memory.validate_analysis(value, job)

    # ----- the full job flow with a fake engine -----

    def test_build_retries_invalid_output_then_freezes_an_immutable_completion(self):
        engine = FakeEngine(**{"alpha-task": [{"result": invalid_analysis()}, {"result": analysis()}]})
        result = self.build(engine, tasks=["alpha-task"])
        self.assertEqual(len(engine.requests), 2)
        self.assertEqual(result["this_run"], [{"task_id": "alpha-task", "status": "complete", "attempt": "attempt-002"}])
        job_dir = self.work / "jobs/alpha-task"
        first = json.loads((job_dir / "attempt-001/attempt.json").read_text())
        second = json.loads((job_dir / "attempt-002/attempt.json").read_text())
        self.assertEqual(first["status"], "output_invalid")
        self.assertIn("outside this task", first["error"])
        self.assertTrue(first["model_call_performed"])
        self.assertFalse((job_dir / "attempt-001/analysis.json").exists())
        self.assertEqual(second["status"], "complete")
        timeout = self.job()["timeout_seconds"]
        self.assertEqual(self.token_requests, [timeout + 900, timeout + 900])
        # Prompt rendering: the lead's template verbatim, every placeholder filled, no Hub id.
        request = engine.requests[1]
        prompt = (job_dir / "attempt-002/prompt.txt").read_text()
        self.assertEqual(request.prompt, prompt)
        packet = (job_dir / "attempt-002/evidence_packet.txt").read_text()
        template = v7_memory.PROMPT_PATH.read_text()
        self.assertEqual(prompt, template.format(task_id="alpha-task", attempt_count=2, models="claude-x 1 x1, gpt-5.5 x1",
                                                 max_lessons=8, timeout_seconds=timeout, packet=packet))
        self.assertIn("/pool/trajectories/alpha-task/ holds 2 transcripts", prompt)
        self.assertIn(f"Time budget: {timeout} seconds", prompt)
        self.assertIn("trial=attempt-001; reward=0", packet)
        for marker in ("{task_id}", "{packet}", "{models}", "{max_lessons}", "{attempt_count}", "{timeout_seconds}"):
            self.assertNotIn(marker, prompt)
        # The schema file the container sees equals the --json-schema argument and the validator's schema.
        self.assertEqual(engine.observed[1], {"task_id": "alpha-task", "work": ["output_schema.json"],
                                              "schema": v7_memory.schema_text()})
        self.assertEqual(request.schema_text, v7_memory.schema_text())
        self.assertEqual(request.pool, self.corpus / "pools/alpha-task")
        self.assertEqual(request.work, job_dir / "attempt-002/work")
        self.assertNotIn(TOKEN, repr(request))
        # Recorded audit, completions and immutable completion.
        self.assertEqual(second["model_audit"]["models_used"], ["claude-opus-5-5"])
        self.assertTrue(second["model_audit"]["strict_model_audit_verified"])
        self.assertEqual((second["model_audit"]["effort_requested"], second["model_audit"]["effort_verified"]), ("high", False))
        self.assertEqual(second["usage"]["total_cost_usd"], 0.75)
        self.assertEqual(second["result_origin"]["source"], "structured_output")
        self.assertEqual(len(second["path_completions"]), 3)
        done = json.loads((job_dir / "completion.json").read_text())
        self.assertEqual({key: value for key, value in done.items() if key != "attempt_sha256"}, second)
        saved = json.loads((job_dir / "attempt-002/analysis.json").read_text())
        self.assertEqual(saved["approach_families"][0]["sources"][1]["path"], A2)
        self.assertEqual(json.loads((job_dir / "attempt-002/result.json").read_text()), analysis())
        published = self.corpus / "analyses/alpha-task"
        self.assertEqual((published / "analysis.json").read_bytes(), (job_dir / "attempt-002/analysis.json").read_bytes())
        self.assertIn("requested from claude-opus-5-5 (high effort); written by claude-opus-5-5 (100.0% of output tokens)",
                      (published / "analysis.md").read_text())
        everything = self.all_text(self.work) + self.all_text(self.corpus / "analyses")
        self.assertNotIn(TOKEN, everything)
        for hub_id in HUB_IDS.values():
            self.assertNotIn(hub_id, everything)
        # Re-running reuses the completion after re-hashing and never calls a model.
        idle = FakeEngine()
        rerun = self.build(idle, tasks=["alpha-task"])
        self.assertEqual(idle.requests, [])
        self.assertTrue(rerun["this_run"][0]["reused"])
        # Any change to a completed artifact or record is refused before a model could run.
        target = job_dir / "attempt-002/analysis.json"
        original = target.read_bytes()
        target.write_bytes(original.replace(b"coreutils sort", b"coreutils sorT"))
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.build(idle, tasks=["alpha-task"])
        target.write_bytes(original)
        record = job_dir / "completion.json"
        kept = record.read_bytes()
        record.write_text(json.dumps({**done, "lesson_count": 99}))
        with self.assertRaisesRegex(ValueError, "disagrees"):
            self.build(idle, tasks=["alpha-task"])
        record.write_bytes(kept)
        self.build(idle, tasks=["alpha-task"])
        # Even a consistent forgery (derived analysis rewritten and every recorded hash updated) is refused,
        # because the analysis is re-derived from the raw model output.
        forged = json.loads(original)
        forged["lessons"][0]["what_to_do_instead"] = "Read the hidden tests first."
        v6.write_json(target, forged)
        attempt_record = json.loads((job_dir / "attempt-002/attempt.json").read_text())
        attempt_record["artifact_sha256"]["analysis.json"] = v6.sha(target)
        v6.write_json(job_dir / "attempt-002/attempt.json", attempt_record)
        v6.write_json(record, {**attempt_record, "attempt_sha256": v6.sha(job_dir / "attempt-002/attempt.json")})
        with self.assertRaisesRegex(ValueError, "disagrees with the raw builder output"):
            self.build(idle, tasks=["alpha-task"])

    def test_retry_cap_preserves_failures_and_later_builds_do_not_call_the_model(self):
        engine = FakeEngine(**{"alpha-task": [{"result": invalid_analysis()}, {"result": {"broken": True}}]})
        result = self.build(engine, tasks=["alpha-task"], max_attempts=2)
        self.assertEqual(result["this_run"][0]["status"], "failed")
        self.assertEqual(len(engine.requests), 2)
        statuses = [json.loads(path.read_text())["status"]
                    for path in sorted((self.work / "jobs/alpha-task").glob("attempt-*/attempt.json"))]
        self.assertEqual(statuses, ["output_invalid", "output_invalid"])
        self.assertFalse((self.work / "jobs/alpha-task/completion.json").exists())
        idle = FakeEngine()
        self.assertEqual(self.build(idle, tasks=["alpha-task"])["this_run"][0]["status"], "failed")
        self.assertEqual(idle.requests, [])
        summary = v7_memory.status(self.work, base=self.base)
        self.assertEqual((summary["failed"], summary["pending"], summary["complete"]), (1, 1, 0))
        self.assertEqual(summary["jobs"][0]["attempts"], 2)
        self.assertEqual(summary["total_cost_usd"], 1.5)

    def test_parallel_workers_build_every_job(self):
        engine = FakeEngine(**{"alpha-task": [{"result": analysis()}], "beta-task": [{"result": beta_analysis()}]})
        result = self.build(engine, workers=6)
        self.assertEqual(sorted(row["task_id"] for row in result["this_run"]), ["alpha-task", "beta-task"])
        self.assertEqual((result["complete"], result["status"]), (2, "complete"))
        self.assertEqual(json.loads((self.work / "status.json").read_text())["complete"], 2)

    def test_refusal_fallback_is_accepted_and_recorded_through_the_seed(self):
        fallback = {"original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8", "api_refusal_category": "bio"}
        engine = FakeEngine(**{"alpha-task": [{"result": analysis(), "fallbacks": [fallback],
                                               "writers": ["claude-opus-5-5", "claude-opus-4-8"],
                                               "usage": {"claude-opus-5-5": {"outputTokens": 71},
                                                         "claude-opus-4-8": {"outputTokens": 52519}}}]})
        self.build(engine, tasks=["alpha-task"])
        attempt = json.loads((self.work / "jobs/alpha-task/attempt-001/attempt.json").read_text())
        audit = attempt["model_audit"]
        self.assertEqual(attempt["status"], "complete")
        self.assertEqual(audit["refusal_fallbacks"], [fallback])
        self.assertFalse(audit["strict_model_audit_verified"])
        self.assertEqual(audit["models_used"], ["claude-opus-4-8", "claude-opus-5-5"])
        self.assertEqual(audit["output_tokens_by_model"], {"claude-opus-5-5": 71, "claude-opus-4-8": 52519})
        self.assertEqual(audit["output_share_by_model"], {"claude-opus-5-5": 0.001, "claude-opus-4-8": 0.999})
        self.assertIn("after a Claude Code safeguard-refusal fallback",
                      (self.corpus / "analyses/alpha-task/analysis.md").read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)
        seed = self.corpus / "seeds/alpha-task"
        manifest = v7_seed.validate_seed(seed, pool=self.corpus / "pools/alpha-task", task_id="alpha-task")
        self.assertFalse(manifest["provenance"]["strict_model_audit_verified"])
        self.assertEqual(manifest["provenance"]["refusal_fallbacks"], [fallback])
        self.assertEqual(manifest["provenance"]["models_used"], ["claude-opus-4-8", "claude-opus-5-5"])
        summary = v7_memory.status(self.work, base=self.base)
        self.assertEqual(summary["jobs_with_refusal_fallbacks"], 1)
        self.assertEqual(summary["jobs"][0]["fallback_models"], ["claude-opus-4-8"])

    def test_wrong_initial_model_tampered_inputs_and_late_checkpoints_fail_attempts(self):
        engine = FakeEngine(**{"alpha-task": [{"result": analysis(), "init_model": "claude-sonnet-5-5"},
                                              {"result": analysis(), "tamper": True}],
                               "beta-task": [{"summary": beta_analysis(), "timed_out": True, "stale_summary": True},
                                             {"summary": beta_analysis(), "timed_out": True, "exit_code": None}]})
        self.build(engine, max_attempts=2)
        self.assertEqual(self.attempt("alpha-task", 1)["status"], "model_audit_failed")
        self.assertEqual(self.attempt("alpha-task", 2)["status"], "inputs_modified")
        self.assertEqual(self.attempt("beta-task", 1)["status"], "budget_exhausted")
        self.assertIn("outside_attempt_window", self.attempt("beta-task", 1)["error"])
        # A checkpoint written within the window survives a timeout without a final result event.
        recovered = self.attempt("beta-task", 2)
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["result_origin"]["file"], "work/summary.json")
        self.assertFalse(recovered["usage"]["usage_available"])
        self.assertFalse(recovered["model_audit"]["strict_model_audit_verified"])
        self.assertEqual(recovered["model_audit"]["models_used"], ["claude-opus-5-5"])

    def test_token_wait_fails_the_job_without_a_model_call(self):
        credentials = self.root / "credentials.json"
        clock = [1_000_000.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": (clock[0] + 600) * 1000}}))

        def provider(seconds):
            return v7_memory.claude_access_token(credentials, seconds, now=lambda: clock[0], sleep=sleep)
        idle = FakeEngine()
        result = self.build(idle, tasks=["alpha-task"], token_provider=provider)
        self.assertEqual(idle.requests, [])
        self.assertEqual(result["this_run"][0]["status"], "token_unavailable")
        self.assertEqual((len(sleeps), sum(sleeps)), (45, 2700))
        attempt = self.attempt("alpha-task", 1)
        self.assertEqual(attempt["status"], "token_unavailable")
        self.assertFalse(attempt["model_call_performed"])
        self.assertNotIn(TOKEN, self.all_text(self.work))
        # A token-less attempt does not consume the model-attempt budget.
        engine = FakeEngine(**{"alpha-task": [{"result": invalid_analysis()}, {"result": analysis()}]})
        self.assertEqual(self.build(engine, tasks=["alpha-task"])["this_run"][0]["status"], "complete")
        self.assertEqual(len(engine.requests), 2)

    def test_token_lifetime_rule_waits_for_a_host_refresh(self):
        credentials = self.root / "credentials.json"
        clock, sleeps, refresh_after = [5_000_000.0], [], [None]

        def write(token, lifetime):
            credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "never-read",
                                                                 "expiresAt": (clock[0] + lifetime) * 1000}}))

        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            if len(sleeps) == refresh_after[0]:
                write("refreshed", 8 * 3600)

        def get(seconds):
            return v7_memory.claude_access_token(credentials, seconds, now=lambda: clock[0], sleep=advance)
        write("long-lived", 3 * 3600)
        self.assertEqual(get(3600 + 900), "long-lived")
        self.assertEqual(sleeps, [])
        write("short-lived", 1800)
        refresh_after[0] = 3
        self.assertEqual(get(3600 + 900), "refreshed")
        self.assertEqual(sleeps, [60, 60, 60])
        refresh_after[0] = None
        sleeps.clear()
        credentials.unlink()
        with self.assertRaisesRegex(v7_memory.TokenUnavailable, "missing or unreadable"):
            get(600)
        self.assertEqual((len(sleeps), sum(sleeps)), (45, 2700))
        write("expiring-token-value", 100)
        with self.assertRaises(v7_memory.TokenUnavailable) as caught:
            get(600)
        self.assertIn("below the required 600 s", str(caught.exception))
        self.assertNotIn("expiring-token-value", str(caught.exception))

    # ----- support, plan freezing and audits -----

    def test_support_and_plan_changes_refuse_to_continue_a_build(self):
        self.build(FakeEngine(**{"alpha-task": [{"result": analysis()}]}), tasks=["alpha-task"])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.build(FakeEngine(), tasks=["beta-task"],
                       support_fn=lambda: fake_support(self.root, claude_version="2.1.300 (Claude Code)"))
        plan_path = self.work / "plan.json"
        plan = json.loads(plan_path.read_text())
        kept = plan_path.read_bytes()
        plan["prompt_template_sha256"] = "0" * 64
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "plan differs"):
            self.build(FakeEngine(), tasks=["alpha-task"])
        plan_path.write_bytes(kept)
        plan = json.loads(kept)
        name = next(iter(plan["source_code_sha256"]))
        plan["source_code_sha256"][name] = "0" * 64
        plan_path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, "accept-code-update"):
            self.build(FakeEngine(), tasks=["alpha-task"])
        self.build(FakeEngine(), tasks=["alpha-task"], accept_code_update=True)
        receipt = json.loads((self.work / "plan_history/amendment-001.json").read_text())
        self.assertEqual(receipt["changed_source_files"], [name])
        self.assertEqual(v7_memory.load(plan_path)["source_code_sha256"][name], v6.sha(v7_memory.ROOT / name))

    def test_tool_audit_records_network_commands_without_gating(self):
        engine = FakeEngine(**{"alpha-task": [{"result": analysis(), "command": "cd /tmp && curl -s https://example.org/x"}],
                               "beta-task": [{"result": beta_analysis(), "command": "grep -rnE 'curl|pip install' /pool"}]})
        self.build(engine)
        alpha, beta = (self.attempt(task, 1)["tool_audit"] for task in ("alpha-task", "beta-task"))
        self.assertEqual(alpha["network_command_count"], 1)
        self.assertEqual(alpha["network_commands"], ["cd /tmp && curl -s https://example.org/x"])
        self.assertEqual(beta["network_command_count"], 0)
        self.assertEqual(beta["tool_use_counts"], {"Bash": 1})
        self.assertEqual(self.attempt("alpha-task", 1)["status"], "complete")

    def test_audit_events_reads_init_fallbacks_and_model_usage(self):
        path = self.root / "events.jsonl"
        events = [{"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
                  {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}},
                  {"type": "assistant", "message": {"model": "<synthetic>", "content": []}},
                  {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1.2, "num_turns": 3,
                   "modelUsage": {"claude-opus-5-5": {"outputTokens": 10}, "claude-haiku-5": {"outputTokens": 0}},
                   "usage": {"output_tokens": 10}, "result": "a b"}]
        path.write_bytes(b"not json\n" + "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode())
        usage, audit, final = v7_memory.audit_events(path)
        self.assertEqual(final["result"], "a b")
        self.assertTrue(audit["strict_model_audit_verified"])
        self.assertEqual(audit["models_used"], ["claude-haiku-5", "claude-opus-5-5"])
        self.assertEqual((usage["total_cost_usd"], usage["num_turns"], usage["usage_available"]), (1.2, 3, True))
        events[3]["modelUsage"]["claude-haiku-5"]["outputTokens"] = 5
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        self.assertFalse(v7_memory.audit_events(path)[1]["strict_model_audit_verified"])

    # ----- docker command and real-engine cleanup (patched; no Docker) -----

    def support_with_binary(self):
        binary = self.root / "claude"
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        return fake_support(self.root, claude_binary_path=str(binary), claude_binary_sha256=v6.sha(binary))

    def test_docker_command_mounts_only_the_task_pool_and_passes_the_token_by_name(self):
        support = self.support_with_binary()
        pool = self.corpus / "pools/alpha-task"
        argv = v7_memory.docker_argv(self.base, support, pool, self.root / "work", self.root / "home", "v7-test",
                                     v7_memory.schema_text())
        mounts = [argv[n + 1] for n, value in enumerate(argv) if value == "--mount"]
        targets = {mount.split(",dst=")[1]: mount for mount in mounts}
        self.assertEqual(set(targets), {"/pool,readonly", "/work", "/opt/builder-home", "/opt/builder-python,readonly",
                                        "/opt/claude/claude,readonly", "/opt/builder-ca.crt,readonly",
                                        "/opt/builder-bin/rg,readonly"})
        self.assertIn(f"src={(self.base / 'runtime/bin/rg').resolve()}", targets["/opt/builder-bin/rg,readonly"])
        tools = argv[argv.index("--disallowedTools") + 1:]
        for blocked in ("WebSearch", "WebFetch", "Task", "Workflow", "RemoteTrigger", "SendMessage", "Skill"):
            self.assertIn(blocked, tools)
        self.assertIn(f"src={pool.resolve()}", targets["/pool,readonly"])
        for forbidden in ("tests", "solution", "README", "registry", ".csv", "prepared/tasks", "audit", "index.json"):
            self.assertFalse(any(forbidden in mount for mount in mounts), forbidden)
        for flag in ("--read-only", "--init", "--interactive"):
            self.assertIn(flag, argv)
        joined = " ".join(argv)
        for fragment in ("--cap-drop ALL", "--security-opt no-new-privileges", "--tmpfs /tmp:rw,nosuid,size=512m",
                         "--workdir /work", "--env HOME=/opt/builder-home", "--env DISABLE_AUTOUPDATER=1",
                         "--env CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "--env DISABLE_TELEMETRY=1",
                         "--env SSL_CERT_FILE=/opt/builder-ca.crt", "--env NODE_EXTRA_CA_CERTS=/opt/builder-ca.crt"):
            self.assertIn(fragment, joined)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", argv)
        self.assertFalse(any(value.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for value in argv))
        tail = argv[argv.index(support["image_id"]):]
        self.assertEqual(tail, [support["image_id"], "/opt/claude/claude", "-p", "--model", "claude-opus-5-5",
                                "--effort", "high", "--output-format", "stream-json", "--verbose",
                                "--dangerously-skip-permissions", "--json-schema", v7_memory.schema_text(),
                                "--disallowedTools", *v7_memory.DISALLOWED_TOOLS])
        self.assertEqual(tail[-1], v7_memory.DISALLOWED_TOOLS[-1])  # variadic list stays last; prompt goes on stdin

    def engine_request(self, support):
        attempt = self.root / "attempt"
        (attempt / "work").mkdir(parents=True)
        (self.root / "home").mkdir()
        return v7_memory.EngineRequest(task_id="alpha-task", container_name="v7-test", base=self.base,
                                       pool=self.corpus / "pools/alpha-task", attempt=attempt, work=attempt / "work",
                                       home=self.root / "home", prompt="PROMPT", schema_text="{}", timeout_seconds=600,
                                       support=support, token=TOKEN)

    def test_real_engine_passes_token_in_env_and_always_removes_the_container(self):
        request = self.engine_request(self.support_with_binary())
        calls = {}

        class Process:
            def __init__(self, argv, **kwargs):
                calls["popen"] = (argv, kwargs)
                self.returncode = None

            def communicate(self, input=None, timeout=None):
                if input is not None:
                    calls["input"] = input
                    if behaviour == "timeout":
                        raise subprocess.TimeoutExpired("docker", timeout)
                self.returncode = 0
                return None, None

            def poll(self):
                return self.returncode

            def kill(self):
                calls["killed"] = calls.get("killed", 0) + 1

        def run(argv, **kwargs):
            calls.setdefault("rm", []).append(argv)
            return subprocess.CompletedProcess(argv, 0)

        def missing_docker(*args, **kwargs):
            raise FileNotFoundError("docker")
        for behaviour in ("exit", "timeout", "missing-docker"):
            calls.clear()
            popen = missing_docker if behaviour == "missing-docker" else Process
            with self.subTest(behaviour=behaviour), patch.object(v7_memory.subprocess, "Popen", popen), \
                    patch.object(v7_memory.subprocess, "run", run):
                outcome = v7_memory.claude_engine(request)
                self.assertEqual(calls["rm"], [["docker", "rm", "--force", "v7-test"]])
                self.assertEqual(v7_memory._ACTIVE_CONTAINERS, set())
                if behaviour == "exit":
                    argv, kwargs = calls["popen"]
                    self.assertEqual(kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
                    self.assertEqual(calls["input"], "PROMPT")
                    self.assertNotIn(TOKEN, " ".join(argv))
                    self.assertEqual(argv[:2], ["docker", "run"])
                    self.assertEqual((outcome["exit_code"], outcome["timed_out"], calls.get("killed")), (0, False, None))
                elif behaviour == "timeout":
                    self.assertEqual((outcome["exit_code"], outcome["timed_out"], calls["killed"]), (None, True, 1))
                else:
                    self.assertEqual(outcome["execution_error"], "FileNotFoundError")
        self.assertTrue((request.attempt / "claude.events.jsonl").exists())

    def test_engine_launch_failure_is_infrastructure_and_does_not_consume_attempts(self):
        def broken(request):
            return {"container_name": request.container_name, "exit_code": None, "timed_out": False,
                    "execution_error": "FileNotFoundError", "cleanup_error": None, "elapsed_seconds": 0.1}
        result = self.build(broken, tasks=["alpha-task"])
        self.assertEqual(result["this_run"][0]["status"], "infrastructure_failed")
        attempt = json.loads((self.work / "jobs/alpha-task/attempt-001/attempt.json").read_text())
        self.assertFalse(attempt["model_call_performed"])
        engine = FakeEngine(**{"alpha-task": [{"result": invalid_analysis()}, {"result": analysis()}]})
        self.assertEqual(self.build(engine, tasks=["alpha-task"])["this_run"][0]["status"], "complete")

    def test_api_unavailability_stops_the_build_without_consuming_attempts(self):
        engine = FakeEngine(**{"alpha-task": [{"error_result": "API Error: 429 rate_limit_error", "exit_code": 1,
                                               "api_error_status": 429}]})
        result = self.build(engine, workers=1, max_attempts=1)
        self.assertEqual({row["task_id"]: row["status"] for row in result["this_run"]},
                         {"alpha-task": "api_unavailable", "beta-task": "interrupted"})
        self.assertEqual(len(engine.requests), 1)
        self.assertEqual(self.attempt("alpha-task", 1)["status"], "api_unavailable")
        self.assertFalse((self.work / "jobs/beta-task").exists())
        # Neither job spent its single model attempt, so the next build completes both.
        later = FakeEngine(**{"alpha-task": [{"result": analysis()}], "beta-task": [{"result": beta_analysis()}]})
        self.assertEqual(self.build(later, max_attempts=1)["complete"], 2)

    def test_a_stopped_build_marks_the_killed_attempt_interrupted_and_uncounted(self):
        def killed(request):
            outcome = FakeEngine(**{request.task_id: [{"exit_code": 130}]})(request)
            v7_memory._STOP.set()  # what the SIGINT/SIGTERM handler does while a container runs
            return outcome
        result = self.build(killed, tasks=["alpha-task"], max_attempts=1)
        self.assertEqual(result["this_run"][0]["status"], "interrupted")
        self.assertEqual(self.attempt("alpha-task", 1)["status"], "interrupted")
        engine = FakeEngine(**{"alpha-task": [{"result": analysis()}]})
        self.assertEqual(self.build(engine, tasks=["alpha-task"], max_attempts=1)["this_run"][0]["status"], "complete")

    def test_an_access_token_printed_inside_the_container_is_scrubbed(self):
        def leaky(request):
            outcome = FakeEngine(**{request.task_id: [{"result": analysis(), "command": f"echo {request.token}"}]})(request)
            (request.home / ".claude").mkdir(parents=True, exist_ok=True)
            (request.home / ".claude/debug.log").write_text(f"token={request.token}\n")
            return outcome
        self.build(leaky, tasks=["alpha-task"])
        attempt = self.attempt("alpha-task", 1)
        self.assertEqual((attempt["status"], attempt["token_scrubbed_occurrences"]), ("complete", 2))
        self.assertNotIn(TOKEN, self.all_text(self.work))
        self.assertIn("[REDACTED_CLAUDE_ACCESS_TOKEN]",
                      (self.work / "jobs/alpha-task/attempt-001/claude.events.jsonl").read_text())

    def test_binary_change_during_build_stops_before_the_model(self):
        support = self.support_with_binary()
        (self.root / "claude").write_bytes(b"#!/bin/sh\n# updated\n")
        idle = FakeEngine()
        result = self.build(idle, tasks=["alpha-task"], support_fn=lambda: support)
        self.assertEqual(idle.requests, [])
        self.assertEqual(result["this_run"][0]["status"], "infrastructure_failed")

    # ----- conversion and export -----

    def test_conversion_produces_schema_valid_entries_with_exact_fields(self):
        checked, _ = v7_memory.validate_analysis(analysis(), self.job())
        entries, report = v7_memory.convert_analysis("alpha-task", checked, 2)
        self.assertEqual([entry["slug"] for entry in entries],
                         ["alpha-task-0-overview", "alpha-task-1-check-sort-s-exit-status",
                          "alpha-task-2-locale-may-change-the-order", "alpha-task-9-reusable-details"])
        for entry in entries:
            _validate(entry, _entry, entry["slug"])
        overview, first, second, details = entries
        self.assertEqual(overview["title"], "Failure analysis overview: alpha-task")
        self.assertEqual(overview["keywords"], ["alpha-task", "overview", "decision guide", "requirements",
                                                "approach families", "shared assumptions", "verification gaps"])
        self.assertEqual(overview["applicability"], "Only the current task alpha-task. Summarises its 2 officially "
                                                    "failed attempts. Read this first, then the lesson entries.")
        self.assertEqual(overview["procedure"], ["First: Read /app/in.txt and confirm its encoding.",
                                                 "Before finishing: Compare /app/out.txt with a fresh sort of the input."])
        self.assertEqual(overview["pitfalls"], ["Avoid: Declaring success after a non-zero exit.",
                                                "Shared assumption (suspect): The default locale order is right. — "
                                                "The instruction may require byte order."])
        self.assertEqual(overview["verification"], "1. Viewing the first lines. — misses: Ordering errors later on.")
        self.assertEqual(overview["status"], "uncertain")
        self.assertEqual(overview["sources"], [{"path": A1, "line_start": 9, "line_end": 17},
                                               {"path": A2, "line_start": 9, "line_end": 12},
                                               {"path": "/pool/tasks/alpha-task.md", "line_start": 1, "line_end": 3},
                                               {"path": "/pool/tasks/alpha-task.md", "line_start": 9, "line_end": 9}])
        evidence = overview["evidence_summary"]
        self.assertTrue(evidence.startswith("Sort the words of /app/in.txt"))
        self.assertIn("- [high] Write /app/out.txt with one word per line. — Both attempts wrote the file; one crashed first.", evidence)
        self.assertIn("- coreutils sort (2 attempts; execution likely wrong): furthest Ran sort once.; broke sort "
                      "reported an error.; evidence Both transcripts show the failing command.", evidence)
        self.assertTrue(evidence.endswith("Unexplained: Why the second attempt stopped early."))
        self.assertEqual(first["keywords"], ["sort", "exit status", "alpha-task", "verification gap"])
        self.assertEqual(first["applicability"], "Only the current task alpha-task; lesson 1 of 2 from its 2 failed attempts.")
        self.assertEqual((first["procedure"], first["pitfalls"]), (["Check the exit status and rerun the sort."],
                                                                    ["An attempt printed an error and still declared success."]))
        self.assertEqual(first["verification"], "Run `echo $?` right after sort.")
        self.assertEqual((first["status"], second["status"]), ("source_observed", "uncertain"))
        self.assertEqual(first["evidence_summary"].splitlines(), [
            "Observed failure: An attempt printed an error and still declared success.", "Failure mode: verification gap",
            "Attempts affected (analyst count, unverified): 2 of 2", "Cause (observed): The failing command was never checked.",
            "Proposed fix (unverified): Check the exit status and rerun the sort.",
            "All cited attempts at this same task received reward 0; nothing here was verified on this task."])
        self.assertEqual(details["title"], "Reusable details: alpha-task")
        self.assertEqual(details["keywords"], ["alpha-task", "reusable details", "commands", "parameters", "environment traps"])
        self.assertEqual(details["procedure"], ["Input path: /app/in.txt is the only input.",
                                                "Exit check: sort exits 2 on a read error. (caveat: Seen in one attempt only.)"])
        self.assertEqual((details["pitfalls"], details["status"]), ([], "uncertain"))
        self.assertEqual(details["verification"], "Re-check each detail in the current environment before relying on "
                                                  "it; details may come from failed attempts.")
        self.assertEqual(report, {"caps": [], "truncations": []})
        unknown = copy.deepcopy(checked)
        unknown["lessons"][0]["failure_mode"] = "unknown"
        unknown["reusable_details"] = []
        unknown["verification_gaps"] = []
        entries, _ = v7_memory.convert_analysis("alpha-task", unknown, 2)
        self.assertEqual(entries[1]["status"], "uncertain")
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["verification"], v7_memory.NO_GAPS)

    def test_conversion_caps_sources_and_truncates_long_text_with_a_record(self):
        job = self.job()
        value = analysis()
        many = [{"path": A2, "line_start": n, "line_end": n} for n in range(1, 37)]
        family = copy.deepcopy(value["approach_families"][0])
        family.update(name="n" * 120, furthest_progress="f" * 500, where_it_broke="w" * 600, evidence="e" * 500)
        value["approach_families"] = [{**family, "sources": many[n * 6:(n + 1) * 6]} for n in range(6)]
        value["requirements"] = [{"requirement": "r" * 300, "how_attempts_handled_it": "h" * 500, "risk": "low"}] * 10
        checked, _ = v7_memory.validate_analysis(value, job)
        entries, report = v7_memory.convert_analysis("alpha-task", checked, 2)
        overview = entries[0]
        self.assertEqual(len(overview["sources"]), 20)
        self.assertEqual(overview["sources"], many[:20])
        self.assertEqual(report["caps"], [{"slug": "alpha-task-0-overview", "field": "sources", "available": 39, "kept": 20}])
        self.assertEqual(len(overview["evidence_summary"]), 12000)
        self.assertTrue(overview["evidence_summary"].endswith("\n[truncated]"))
        self.assertEqual(report["truncations"][0]["field"], "evidence_summary")
        self.assertGreater(report["truncations"][0]["original_chars"], 12000)
        for entry in entries:
            _validate(entry, _entry, entry["slug"])

    def test_export_refuses_missing_jobs_then_writes_valid_seeds_and_index(self):
        self.build(FakeEngine(**{"alpha-task": [{"result": analysis()}]}), tasks=["alpha-task"])
        with self.assertRaisesRegex(ValueError, "beta-task"):
            v7_memory.export(self.corpus, self.work, base=self.base)
        self.assertFalse((self.corpus / "seeds").exists())
        self.assertFalse((self.corpus / "seeds_index.json").exists())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(v7_memory.main(["export", "--corpus", f"prepared/v7_corpus/{BUILD}", "--work-dir",
                                             "runs/v7_memory_build_test", "--base", str(self.base), "--allow-missing"]), 0)
        exported = json.loads(output.getvalue())
        self.assertEqual(exported["seeds_created"], ["alpha-task"])
        index = json.loads((self.corpus / "seeds_index.json").read_text())
        seed_path = f"prepared/v7_corpus/{BUILD}/seeds/alpha-task"
        seed = self.base / seed_path
        manifest = v7_seed.validate_seed(seed, pool=self.corpus / "pools/alpha-task", task_id="alpha-task")
        self.assertEqual(index, {"schema_version": 1, "tasks": {
            "alpha-task": {"seed_path": seed_path, "manifest_sha256": v6.sha(seed / "manifest.json"),
                           "entry_count": manifest["entry_count"]},
            "beta-task": {"seed_path": None, "reason": "builder_failed"},
            "empty-task": {"seed_path": None, "reason": "no_available_failed_trajectories"}}})
        self.assertEqual(manifest["entry_count"], 4)
        provenance = manifest["provenance"]
        self.assertEqual((provenance["builder_model_requested"], provenance["builder_effort_requested"]),
                         ("claude-opus-5-5", "high"))
        self.assertEqual(provenance["source_selection"], "failed_same_task_official_trajectories")
        self.assertTrue(provenance["strict_model_audit_verified"])
        self.assertEqual(provenance["completion_sha256"], v6.sha(self.work / "jobs/alpha-task/completion.json"))
        memory = self.root / "memory"
        initial = v7_seed.initialize_same_task_memory(memory, "alpha-task", seed, pool=self.corpus / "pools/alpha-task")
        self.assertTrue(initial["valid"])
        self.assertEqual((initial["event_count"], initial["entry_count"]), (0, manifest["entry_count"]))
        independent = audit_memory(memory)
        self.assertTrue(independent["valid"])
        self.assertEqual((independent["event_count"], independent["entry_count"]), (0, manifest["entry_count"]))
        text = self.all_text(seed)
        for hub_id in HUB_IDS.values():
            self.assertNotIn(hub_id, text)
        self.assertNotIn(TOKEN, text)
        with contextlib.redirect_stdout(io.StringIO()):
            again = v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)
        self.assertEqual((again["seeds_reused"], again["seeds_created"], again["seeds_index_changed"]),
                         (["alpha-task"], [], False))
        # Later completing the missing job adds its seed without touching the frozen one.
        before = v6.sha(seed / "manifest.json")
        self.build(FakeEngine(**{"beta-task": [{"result": beta_analysis()}]}), tasks=["beta-task"])
        final = v7_memory.export(self.corpus, self.work, base=self.base)
        self.assertEqual(final["seeds_created"], ["beta-task"])
        self.assertEqual(v6.sha(seed / "manifest.json"), before)
        self.assertEqual(json.loads((self.corpus / "seeds_index.json").read_text())["tasks"]["beta-task"]["entry_count"], 4)

    def test_export_gate_refuses_outside_access_or_hidden_overlap_until_reviewed(self):
        leaked = beta_analysis()
        leaked["unexplained"] = HIDDEN_SOLUTION
        engine = FakeEngine(**{"alpha-task": [{"result": analysis(), "command": "curl -s https://www.tbench.ai/tasks/alpha"}],
                               "beta-task": [{"result": leaked}]})
        self.build(engine)
        with self.assertRaisesRegex(ValueError, "Isolation gate flagged") as caught:
            v7_memory.export(self.corpus, self.work, base=self.base)
        message = str(caught.exception)
        for expected in ("alpha-task", "curl", "tbench.ai", "beta-task", "of the reference solution"):
            self.assertIn(expected, message)
        self.assertFalse((self.corpus / "seeds_index.json").exists())
        self.assertFalse((self.corpus / "seeds").exists())
        reviews = {"alpha-task": "reviewed: the fetch failed and the analysis cites only pool lines",
                   "beta-task": "reviewed for the test"}
        (self.work / "export_reviews.json").write_text(json.dumps(reviews))
        with contextlib.redirect_stdout(io.StringIO()):
            v7_memory.export(self.corpus, self.work, base=self.base)
        gate = v7_seed.validate_seed(self.corpus / "seeds/alpha-task", pool=self.corpus / "pools/alpha-task",
                                     task_id="alpha-task")["provenance"]["isolation_gate"]
        self.assertEqual((gate["flagged"], gate["outside_access"], gate["review"]), (True, ["curl"], reviews["alpha-task"]))
        beta = v7_seed.validate_seed(self.corpus / "seeds/beta-task", pool=self.corpus / "pools/beta-task",
                                     task_id="beta-task")["provenance"]["isolation_gate"]
        self.assertGreaterEqual(beta["solution_coverage"], 0.5)

    def test_export_gate_ignores_search_patterns_and_written_text(self):
        self.assertEqual(v7_memory.executed_outside('grep -rnE "curl|pip install|wget" /pool/trajectories'), [])
        self.assertEqual(v7_memory.executed_outside(r'grep -n "pip3 install\|apt-get \"x\"" f | cut -c1-200 | head'), [])
        self.assertEqual(v7_memory.executed_outside("cat > /work/summary.json <<'EOF'\n{\"x\": \"urllib\"}\nEOF"), [])
        self.assertEqual(v7_memory.executed_outside("cd /pool && wget -q http://x"), ["wget"])
        self.assertEqual(v7_memory.executed_outside("python3 - <<'EOF'\nimport urllib.request\nEOF"),
                         ["network code in an executed heredoc"])
        engine = FakeEngine(**{"alpha-task": [{"result": analysis(), "command": 'grep -c "curl " /pool/tasks/alpha-task.md'}]})
        self.build(engine, tasks=["alpha-task"])
        with contextlib.redirect_stdout(io.StringIO()):
            v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)
        gate = json.loads((self.corpus / "seeds/alpha-task/manifest.json").read_text())["provenance"]["isolation_gate"]
        self.assertEqual((gate["flagged"], gate["reasons"], gate["review"]), (False, [], None))

    def test_export_refuses_changed_corpus_or_a_foreign_seed(self):
        self.build(FakeEngine(**{"alpha-task": [{"result": analysis()}]}), tasks=["alpha-task"])
        v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)
        manifest = self.corpus / "seeds/alpha-task/manifest.json"
        data = json.loads(manifest.read_text())
        data["provenance"]["completion_sha256"] = "0" * 64
        manifest.write_text(json.dumps(data))
        index = json.loads((self.corpus / "seeds_index.json").read_text())
        with self.assertRaisesRegex(ValueError, "another analysis"):
            v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)
        self.assertEqual(json.loads((self.corpus / "seeds_index.json").read_text()), index)
        published = self.corpus / "analyses/alpha-task/analysis.json"
        published.chmod(0o644)
        published.write_text("{}")
        with self.assertRaisesRegex(ValueError, "different published analysis"):
            self.build(FakeEngine(), tasks=["alpha-task"])
        index = json.loads((self.corpus / "index.json").read_text())
        index["build_id"] = "another-build"
        (self.corpus / "index.json").write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "changed since the plan was frozen"):
            v7_memory.export(self.corpus, self.work, base=self.base, allow_missing=True)

    def test_status_cli_summarises_jobs_attempts_fallbacks_and_cost(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            v7_memory.main(["status", "--work-dir", str(self.work)])
        self.assertEqual(json.loads(output.getvalue())["status"], "not_started")
        fallback = {"original_model": "claude-opus-5-5", "fallback_model": "claude-opus-4-8", "api_refusal_category": "bio"}
        engine = FakeEngine(**{"alpha-task": [{"result": invalid_analysis(), "cost": 0.5},
                                              {"result": analysis(), "cost": 1.25, "fallbacks": [fallback],
                                               "writers": ["claude-opus-4-8"]}]})
        self.build(engine, tasks=["alpha-task"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(v7_memory.main(["status", "--work-dir", "runs/v7_memory_build_test", "--base", str(self.base)]), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual((summary["total_jobs"], summary["complete"], summary["failed"], summary["pending"]), (2, 1, 0, 1))
        self.assertEqual((summary["attempts"], summary["total_cost_usd"], summary["jobs_with_refusal_fallbacks"]), (2, 1.75, 1))
        alpha = summary["jobs"][0]
        self.assertEqual((alpha["status"], alpha["attempts"], alpha["model_attempts"], alpha["refusal_fallbacks"]),
                         ("complete", 2, 2, 1))
        self.assertEqual(alpha["models_used"], ["claude-opus-4-8", "claude-opus-5-5"])
        self.assertFalse(alpha["strict_model_audit_verified"])
        self.assertEqual(summary["jobs"][1]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
