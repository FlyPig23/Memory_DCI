#!/usr/bin/env python3
"""Exercise the real stop/restart/gold-injection path using synthetic artifacts."""
from pathlib import Path
import json
import sys
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiment.src.task_runtime import TaskSpec, TaskRuntime
from experiment.src.runtime_support import make_runtime_config
from experiment.src.experiment_protocol import IMAGE, atomic_json


def main():
    run_id = f"grading-smoke-{int(time.time())}"
    inputs = ROOT / "experiment/runtime/synthetic-grading"
    for sub in ("exec", "gt", "skills"):
        (inputs / sub).mkdir(parents=True, exist_ok=True)
    (inputs / "exec/input.txt").write_text("synthetic public input")
    (inputs / "gt/gold.txt").write_text("synthetic grading canary")
    code = """from pathlib import Path
def grade(transcript, workspace_path):
    p=Path(workspace_path)
    assert (p/'gt/gold.txt').read_text()=='synthetic grading canary'
    assert (p/'answer.txt').read_text()=='synthetic answer'
    return {'overall_score':0.75}
"""
    task = TaskSpec("synthetic-grading", "synthetic", "Synthetic grading test", inputs, inputs / "skills", automated_checks=code)
    rt = TaskRuntime(make_runtime_config(ROOT, IMAGE), task, ROOT / "experiment/runs/smoke" / run_id, run_id)
    report = {}
    try:
        rt.prepare("")
        report["gold_hidden_before_stop"] = not (rt.workspace / "gt").exists()
        rt.exec(["python3", "-c", "from pathlib import Path; Path('/tmp_workspace/answer.txt').write_text('synthetic answer')"])
        rt.freeze_agent()
        transcript = rt.run_dir / "synthetic-transcript.jsonl"
        transcript.write_text("")
        report["evaluation"] = rt.grade(transcript_path=transcript, transcript_loader=ROOT / "experiment/vendor/WildClawBench/src/utils/transcript_loader.py")
        report["passed"] = report["gold_hidden_before_stop"] and report["evaluation"]["status"] == "graded" and report["evaluation"]["overall_score"] == 0.75
        if not report["passed"]:
            raise RuntimeError("Synthetic grading lifecycle failed")
    finally:
        atomic_json(rt.run_dir / "smoke_report.json", report)
        rt.cleanup()
        print(json.dumps({"run_dir": str(rt.run_dir), **report}, indent=2))


if __name__ == "__main__":
    main()
