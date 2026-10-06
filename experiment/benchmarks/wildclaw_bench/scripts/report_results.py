#!/usr/bin/env python3
"""Recompute retained experiment scores using only 72 saved result.json files."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics

LAYOUT = {
    "V1": ("experiment/benchmarks/wildclaw_bench/runs/formal/*-a0/result.json", "A0"),
    "V1.1": ("experiment/benchmarks/wildclaw_bench/runs/vanilla_sol/formal/*/result.json", "VANILLA_SOL"),
    "V5": ("experiment/benchmarks/wildclaw_bench/runs/dci_memory/formal/*/result.json", "DCI_TASK_LOCAL_MEMORY"),
}


def number(value, label, *, upper=None, integer=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (upper is not None and value > upper)
            or (integer and int(value) != value)):
        raise ValueError(f"Invalid {label}: expected a finite nonnegative number")
    return int(value) if integer else value


def optional(value, label):
    return None if value is None else number(value, label, integer=True)


def stats(values):
    known = [value for value in values if value is not None]
    return {"known_tasks": len(known), "missing_tasks": len(values) - len(known),
            "total": sum(known) if known else None,
            "mean": statistics.mean(known) if known else None}


def read_results(root):
    versions, sources = {}, {}
    for version, (pattern, condition) in LAYOUT.items():
        paths = sorted(root.glob(pattern))
        if len(paths) != 24:
            raise ValueError(f"{version}: expected exactly 24 result.json files, found {len(paths)}")
        rows = {}
        for path in paths:
            raw = path.read_bytes()
            saved = json.loads(raw)
            task_id = saved.get("task_id")
            if not isinstance(task_id, str) or not task_id or task_id in rows:
                raise ValueError(f"{version}: missing or duplicate task identity")
            if saved.get("condition") != condition or saved.get("status") != "finished":
                raise ValueError(f"{version}: unexpected condition or unfinished result: {task_id}")
            grade = saved.get("evaluation", {})
            if grade.get("status") != "graded":
                raise ValueError(f"{version}: result is not graded: {task_id}")
            execution = saved.get("execution", {})
            review = saved.get("memory_review", {})
            review_execution = review.get("execution", {})
            online_memory = saved.get("online_memory", {})
            final_memory = saved.get("final_memory", {})
            decision = review.get("decision", {}).get("decision")
            if version == "V5" and decision is None:
                decision = final_memory.get("final_decision", {}).get("decision")
            if decision not in (None, "write", "no_update"):
                raise ValueError(f"Unrecognized final review decision: {task_id}")
            rows[task_id] = {
                "task_id": task_id, "run_id": saved.get("run_id", path.parent.name),
                "score": number(grade.get("overall_score"), "score", upper=1),
                "solver_tokens": optional(execution.get("usage", {}).get("total_tokens"), "solver tokens"),
                "review_tokens": optional(review_execution.get("usage", {}).get("total_tokens"), "review tokens") if version == "V5" else 0,
                "solver_seconds": execution.get("elapsed_seconds"),
                "review_seconds": review_execution.get("elapsed_seconds") if version == "V5" else 0,
                "solver_status": execution.get("status"),
                "solver_memory_snapshot_entries": optional(online_memory.get("entry_count"), "memory entries") if version == "V5" else None,
                "final_memory_entries": optional(final_memory.get("entry_count"), "final entries") if version == "V5" else None,
                "final_review_decision": decision,
            }
            for key in ("solver_seconds", "review_seconds"):
                value = rows[task_id][key]
                if value is not None:
                    number(value, key)
            sources[str(path.relative_to(root))] = hashlib.sha256(raw).hexdigest()
        versions[version] = rows
    if not (set(versions["V1"]) == set(versions["V1.1"]) == set(versions["V5"])):
        raise ValueError("V1, V1.1 and V5 must contain the same 24 task identities")
    return versions, sources


def build(root):
    versions, sources = read_results(root)
    aggregates = {}
    for version, indexed in versions.items():
        rows = list(indexed.values())
        aggregates[version] = {
            "tasks": len(rows), "mean_score": statistics.mean(row["score"] for row in rows),
            **{field: stats([row[field] for row in rows]) for field in
               ("solver_tokens", "review_tokens", "solver_seconds", "review_seconds")},
            "solver_status_counts": dict(Counter(row["solver_status"] for row in rows)),
        }
    pairs = []
    for task_id in sorted(versions["V5"]):
        old, new = versions["V1.1"][task_id], versions["V5"][task_id]
        pairs.append({"task_id": task_id, "v1_1_score": old["score"],
                      "v5_score": new["score"], "v5_minus_v1_1": new["score"] - old["score"]})
    deltas = [row["v5_minus_v1_1"] for row in pairs]
    memory = list(versions["V5"].values())
    return {
        "schema_version": 1,
        "derivation": "72 saved result.json files only; no model calls, grading, protocols, or private sessions.",
        "source_hashes": sources,
        "versions": aggregates,
        "v5_vs_v1_1": {
            "paired_tasks": len(pairs), "mean_difference": statistics.mean(deltas),
            "higher": sum(value > 1e-9 for value in deltas),
            "tied": sum(abs(value) <= 1e-9 for value in deltas),
            "lower": sum(value < -1e-9 for value in deltas), "tasks": pairs,
        },
        "v5_memory": {
            "solver_snapshot_entries": stats([row["solver_memory_snapshot_entries"] for row in memory]),
            "tasks_with_solver_snapshot_entries": sum((row["solver_memory_snapshot_entries"] or 0) > 0 for row in memory),
            "final_entries": stats([row["final_memory_entries"] for row in memory]),
            "final_review_decisions": dict(Counter(row["final_review_decision"] or "missing" for row in memory)),
            "online_write_tasks": None,
            "online_write_limitation": "Snapshot entry counts may include solver stage=final writes; online write counts require event evidence.",
        },
        "tasks": {version: [indexed[task_id] for task_id in sorted(indexed)]
                  for version, indexed in versions.items()},
        "limitations": [
            "V1 uses a different solver and judge model; V1 comparison is descriptive.",
            "V5 changes scored historical trajectories, retrieval instructions, tools and task-local memory together.",
            "Each condition has one rollout per task; no causal memory benefit is established.",
            "Tokens include cached input and do not represent subscription billing.",
            "Final review follows answer freezing and cannot improve that saved answer.",
            "Shared result files may be sanitized. Source hashes describe these exact files, not unavailable original sessions.",
            "This report validates score arithmetic and matching records, not historical runtime isolation, model settings, or full audit receipts.",
        ],
    }


def write_report(summary, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    fmt = lambda value: "—" if value is None else f"{value:,.2f}"
    lines = [
        "# V1 / V1.1 / V5：保留实验结果", "",
        "仅从 72 个保存的 result.json 重算：每版本恰好 24 题、任务集合相同、无重复、评分状态有效，分数均为 [0, 1] 内有限值。来源文件的 SHA256 见 `summary.json`（见 [数据与恢复](../../../../../DATA.md)）。", "",
        "| 版本 | 题数 | 平均得分 | 平均求解 tokens | 平均复盘 tokens |",
        "|---|---:|---:|---:|---:|",
    ]
    for version, row in summary["versions"].items():
        lines.append(f"| {version} | {row['tasks']} | {row['mean_score']:.7f} | {fmt(row['solver_tokens']['mean'])} | {fmt(row['review_tokens']['mean'])} |")
    paired = summary["v5_vs_v1_1"]
    memory = summary["v5_memory"]
    lines += [
        "",
        f"V5 − V1.1 平均分差 **{paired['mean_difference']:+.6f}**；V5 高 / 平 / 低为 **{paired['higher']} / {paired['tied']} / {paired['lower']}**。每题每条件一次运行。V1 模型不同，仅作历史参考；V5 同时改变轨迹分数可见性、检索指引、工具与 memory，不能单独证明 memory 收益。",
        "",
        f"V5 求解结束时有 memory 条目的任务数：{memory['tasks_with_solver_snapshot_entries']}；独立最终复盘决定：{json.dumps(memory['final_review_decisions'], ensure_ascii=False)}。快照可能包含 solver 的 stage=final 写入，不能据此计算在线写入次数。",
        "",
        "tokens 包含缓存输入，不等于订阅扣费。独立复盘发生在答案冻结后，不能改善该份答案。缺失成本字段保留为空，不当作零计入均值；对应样本数见 JSON。",
        "",
        "这次重算不调用模型，不重新求解或评分。分享文件可能经过脱敏，哈希仅校验当前文件；未重新验证历史运行隔离、实际模型配置或原始审计回执。",
        "",
        "恢复数据后，从项目根目录运行：",
        "",
        "```bash",
        "python3 experiment/benchmarks/wildclaw_bench/scripts/report_results.py",
        "```",
    ]
    (output / "README.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--output", type=Path, help="Default: ROOT/experiment/benchmarks/wildclaw_bench/reports/retained")
    args = parser.parse_args()
    root = args.root.resolve()
    summary = build(root)
    output = args.output or root / "experiment/benchmarks/wildclaw_bench/reports/retained"
    write_report(summary, output)
    print(json.dumps({"source_files": len(summary["source_hashes"]),
                      "means": {name: row["mean_score"] for name, row in summary["versions"].items()},
                      "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
