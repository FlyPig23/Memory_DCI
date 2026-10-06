#!/usr/bin/env python3
"""Synthetic transport/authentication smoke; does not read a benchmark task."""
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from experiment.shared.codex_backend import CodexBackend, CodexSettings
from experiment.shared.task_runtime import TaskSpec, TaskRuntime
from experiment.benchmarks.wildclaw_bench.src.runtime_support import prepare_ca_bundle, make_runtime_config, copy_runtime_auth


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-call", action="store_true")
    args = parser.parse_args()
    prepare_ca_bundle(ROOT)
    smoke_id = f"transport-{int(time.time())}"
    assets = ROOT / "experiment/benchmarks/wildclaw_bench/runtime/synthetic-inputs"
    (assets / "exec").mkdir(parents=True, exist_ok=True)
    skills = assets / "skills"
    skills.mkdir(exist_ok=True)
    task = TaskSpec("synthetic-transport", "synthetic", "Reply with exactly AUTHENTICATED_OK. Do not use any tools.", assets, skills, timeout_seconds=180)
    rt = TaskRuntime(make_runtime_config(ROOT, "hangxiao-skill-dci/wildclaw-codex:runtime-v1"), task,
                     ROOT / "experiment/benchmarks/wildclaw_bench/runs/smoke" / smoke_id, smoke_id)
    backend = CodexBackend(CodexSettings(reasoning_effort="low", no_tools=True, inference_only=True))
    report = {}
    try:
        backend.prepare(rt)
        copy_runtime_auth(rt)
        report["login"] = backend.preflight(rt)
        code = '''import json, requests, socket
checks = {}
for key, url in [('public_web','https://example.com'),('benchmark_block','https://github.com/InternLM/WildClawBench'),('private_block','http://169.254.169.254/latest/meta-data/')]:
 try:
  r=requests.get(url,timeout=30); checks[key]={'status':r.status_code}
 except Exception as e: checks[key]={'error':type(e).__name__}
s=socket.socket(); s.settimeout(3)
checks['direct_network_block']=s.connect_ex(('1.1.1.1',443))!=0
print(json.dumps(checks))'''
        report["transport"] = json.loads(rt.exec(["python3", "-c", code], timeout=110).stdout)
        t = report["transport"]
        report["transport_pass"] = t["public_web"].get("status") == 200 and t["benchmark_block"].get("status") == 403 and t["private_block"].get("status") == 403 and t["direct_network_block"]
        if not report["transport_pass"]:
            raise RuntimeError("Synthetic network boundary smoke failed")
        if args.model_call:
            result = backend.run(rt)
            output = (rt.workspace / ".codex-last-message.txt").read_text() if (rt.workspace / ".codex-last-message.txt").exists() else ""
            report["model"] = {"status": result.status, "sentinel_matched": output.strip() == "AUTHENTICATED_OK", "usage": result.usage}
            if result.status != "completed" or not report["model"]["sentinel_matched"]:
                raise RuntimeError("Authenticated model smoke failed; inspect private run logs")
        else:
            rt.freeze_agent()
    finally:
        (rt.run_dir / "smoke_report.json").write_text(json.dumps(report, indent=2))
        rt.cleanup()
        print(json.dumps({"run_dir": str(rt.run_dir), **report}, indent=2))


if __name__ == "__main__":
    main()
