"""Check that every configured builder model can be called, in the builder container.

One tiny request per builder; no corpus is mounted. The result records the model
that actually answered (from the Codex session or Claude Code's result event).
Usage: python -m side_experiments.hard_task_failure_replay.scripts.probe_models [builder ...]
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import time

from experiment.benchmarks.terminal_bench_2_1.scripts import build_failure_memory as v6

from . import builders
from .common import MANIFESTS, RUNS, TB, config, write_json

PROMPT = "Reply with the single word OK and nothing else. Do not run any commands."


def probe(builder_id: str, support: dict) -> dict:
    builder = builders.spec(builder_id)
    root = RUNS / "probes" / time.strftime("%Y%m%dT%H%M%S") / builder_id
    attempt, home = root / "work", root / "home"
    attempt.mkdir(parents=True)
    home.mkdir(mode=0o700)
    try:
        outcome = builders.run(builder, support, [], attempt, home, PROMPT, timeout_seconds=240)
    except Exception as exc:  # recorded, not raised: one unavailable model must not hide the others
        return {"builder": builder_id, "model": builder["model"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    answer = ""
    if (attempt / "result.json").exists():
        answer = (attempt / "result.json").read_text().strip()
    else:
        for line in (attempt / "claude.events.jsonl").read_text().splitlines() if (attempt / "claude.events.jsonl").exists() else []:
            event = json.loads(line)
            if event.get("type") == "result":
                answer = str(event.get("result", "")).strip()
    errors = (attempt / "builder.stderr.log").read_text()[-600:]
    return {"builder": builder_id, "engine": builder["engine"], "model": builder["model"], "effort": builder["effort"],
            "ok": outcome["exit_code"] == 0 and outcome["model_audit"]["model_effort_verified"] and "OK" in answer,
            "answer": answer[:80], "exit_code": outcome["exit_code"], "model_audit": outcome["model_audit"],
            "elapsed_seconds": round(outcome["elapsed_seconds"], 1), "stderr_tail": errors if outcome["exit_code"] else ""}


def main() -> int:
    wanted = sys.argv[1:] or list(config()["builders"])
    support = v6.support(TB, v6.DEFAULT_IMAGE)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        rows = list(pool.map(lambda b: probe(b, support), wanted))
    write_json(MANIFESTS / "model_probes.json", {"probed_at_unix": time.time(), "results": rows})
    for row in rows:
        print(json.dumps({k: row.get(k) for k in ("builder", "model", "ok", "answer", "exit_code", "error")}, ensure_ascii=False))
        if not row.get("ok"):
            print("   audit:", json.dumps(row.get("model_audit"), ensure_ascii=False)[:400])
            print("   stderr:", (row.get("stderr_tail") or "")[:400].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
