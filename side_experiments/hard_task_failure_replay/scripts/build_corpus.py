"""Render each chosen task's failed official attempts into its own corpus.

No model is called. Sources are the raw ATIF records under the main package's
data/historical_trials. Only scored attempts with reward 0, an exact task
version and a transcript body are used. Successful attempts are never copied.

Layout per task (generated, git-ignored):
  corpus/<task>/instruction.md                      read by the distiller only
  corpus/<task>/experience/failed_attempts/<model>/<trial>/<task>_0.txt
  corpus/<task>/experience/memory/                   written by distill.py
The solver later sees corpus/<task>/experience read-only at /experience.
"""
from __future__ import annotations

import collections
import csv
import json
import shutil
import sys

from .common import CORPUS, MANIFESTS, TASKS, TB, TRIALS, load, sha, sha_text, write_json
from .render import render_failed_attempt


def failed_sources(task_id: str, split: str) -> list[dict]:
    rows = []
    if split == "training":
        for row in load(TB / "manifests/pool.json")["trajectories"]:
            if (row["task_id"] == task_id and row.get("is_scored") is True and row.get("score") == 0
                    and row["status"] == "available" and row["source_format"] == "atif_json"):
                source = TB / row["source"]
                if sha(source) != row["source_sha256"]:
                    raise ValueError(f"source changed since the pool audit: {row['trial_id']}")
                rows.append({"trial_id": row["trial_id"], "model": row["model"], "source": source,
                             "version_status": row["task_version_evidence"]["status"]})
    else:
        for row in csv.DictReader(open(TB / "reports/official_test_trials.csv")):
            source = TRIALS / row["trial_id"] / "trajectory.json"
            if (row["task_id"] == task_id and row["is_scored"] == "True" and row["reward"] in ("0", "0.0")
                    and row["version_status"] == "exact" and source.is_file()):
                rows.append({"trial_id": row["trial_id"], "model": None, "source": source,
                             "version_status": row["audit_status"]})
    for item in rows:
        metadata = load(item["source"].with_name("trial_metadata.json"))
        if metadata.get("reward") not in (0, 0.0) or metadata.get("is_scored") is not True:
            raise ValueError(f"trial metadata contradicts a scored failure: {item['trial_id']}")
        item["model"] = item["model"] or metadata["model_name"]
        item["agent"] = f"{metadata['agent_name']} {metadata['agent_version']}"
    return sorted(rows, key=lambda r: (r["model"], r["trial_id"]))


def build(task_id: str, split: str) -> dict:
    root = CORPUS / task_id
    attempts = root / "experience/failed_attempts"
    if attempts.exists():
        shutil.rmtree(attempts)
    attempts.mkdir(parents=True)
    shutil.copyfile(TASKS / task_id / "instruction.md", root / "instruction.md")
    records = []
    for item in failed_sources(task_id, split):
        text = render_failed_attempt(json.loads(item["source"].read_text()), task_id=task_id,
                                     model=item["model"], agent=item["agent"], trial_id=item["trial_id"],
                                     version_status=item["version_status"])
        relative = f"failed_attempts/{item['model']}/{item['trial_id']}/{task_id}_0.txt"
        target = root / "experience" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(0o444)
        records.append({"trial_id": item["trial_id"], "model": item["model"], "agent": item["agent"],
                        "source": str(item["source"].relative_to(TB)), "source_sha256": sha(item["source"]),
                        "path": "/experience/" + relative, "relative": relative, "sha256": sha_text(text),
                        "line_count": len(text.splitlines()), "bytes": len(text.encode())})
    models = collections.Counter(r["model"] for r in records)
    index = [f"# Failed attempts at {task_id}", "",
             f"{len(records)} officially graded attempts by other AI agents. Every one scored 0.",
             "Hidden test results are not included. Paths: failed_attempts/<model>/<trial_id>/<task>_0.txt", "",
             "| model | attempts |", "|---|---:|"] + [f"| {m} | {n} |" for m, n in sorted(models.items())]
    (root / "experience/README.md").write_text("\n".join(index) + "\n")
    manifest = {"task_id": task_id, "split": split, "attempt_count": len(records),
                "bytes": sum(r["bytes"] for r in records), "models": dict(sorted(models.items())),
                "instruction_sha256": sha(root / "instruction.md"),
                "selection": "scored official attempts with reward 0, exact task version, transcript present; successes excluded",
                "rendering": "main pool body format; inline base64 images replaced by a size placeholder",
                "records": records}
    write_json(MANIFESTS / f"corpus_{task_id}.json", manifest)
    return manifest


def main() -> int:
    selection = load(MANIFESTS / "selection.json")
    splits = {r["task_id"]: r["split"] for r in selection["ranking"]}
    for task_id in sys.argv[1:] or selection["chosen_task_ids"]:
        manifest = build(task_id, splits[task_id])
        print(f"{task_id}: {manifest['attempt_count']} failed attempts, {manifest['bytes'] / 1e6:.1f} MB, "
              f"models {manifest['models']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
