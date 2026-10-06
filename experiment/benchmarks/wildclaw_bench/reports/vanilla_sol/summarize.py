"""Describe saved V1.1 scores without model calls, regrading, or a new runtime audit."""
import csv
import json
import math
import statistics
from pathlib import Path

from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256
from experiment.benchmarks.wildclaw_bench.variants.vanilla_sol import runner

OUT = Path(__file__).resolve().parent


def saved_plan():
    if runner.PROTOCOL.is_file():
        return runner.PROTOCOL, runner.load(runner.PROTOCOL)
    path = runner.ROOT / "experiment/evidence/frozen_protocols/v1_1.json"
    if not path.is_file():
        raise ValueError("A saved V1.1 protocol is required to summarize results")
    return path, runner.load(path)


def score_from(saved):
    evaluation = saved.get("evaluation", {})
    value = evaluation.get("overall_score")
    if (saved.get("status") != "finished"
            or evaluation.get("status") != "graded"
            or type(value) not in (int, float)
            or not math.isfinite(value)):
        return None
    return value


def main():
    protocol_path, plan = saved_plan()
    planned = plan["tasks"]
    if len(planned) != 24 or len({r["task_id"] for r in planned}) != 24:
        raise ValueError("Expected the fixed 24 distinct V1.1 tasks")
    sources = {str(protocol_path.relative_to(runner.ROOT)): sha256(protocol_path)}
    baseline = {}
    for path in sorted((runner.ROOT / "experiment/benchmarks/wildclaw_bench/runs/formal").glob("*-a0/result.json")):
        saved = runner.load(path)
        if saved.get("condition") != "A0":
            continue
        task_id = saved["task_id"]
        if task_id in baseline:
            raise ValueError("Duplicate V1 result for " + task_id)
        baseline[task_id] = score_from(saved)
        sources[str(path.relative_to(runner.ROOT))] = sha256(path)
    rows = []
    for row in planned:
        path = runner.RUNS / "formal" / row["run_id"] / "result.json"
        saved = runner.load(path) if path.is_file() else {}
        if saved and (saved.get("task_id") != row["task_id"]
                      or saved.get("condition") != runner.CONDITION):
            raise ValueError("Saved V1.1 result identity differs from the protocol")
        if path.is_file():
            sources[str(path.relative_to(runner.ROOT))] = sha256(path)
        execution = saved.get("execution", {})
        score = score_from(saved)
        old = baseline.get(row["task_id"])
        rows.append({
            **row, "category": row["task_id"].split("_task_")[0],
            "score": score, "V1": old,
            "V1_minus_V1_1": None if score is None or old is None else old - score,
            "status": "valid" if score is not None else saved.get("status", "pending"),
            "execution_status": execution.get("status"),
            "solve_seconds": execution.get("elapsed_seconds"),
            "total_tokens": execution.get("usage", {}).get("total_tokens"),
        })
    scored = [row for row in rows if row["score"] is not None]
    paired = [row for row in scored if row["V1"] is not None]
    mean = lambda values: statistics.mean(values) if values else None
    comparisons = {"V1": {
        "paired_tasks": len(paired),
        "reference_mean": mean([r["V1"] for r in paired]),
        "reference_minus_v11": mean([r["V1"] - r["score"] for r in paired]),
        "reference_higher": sum(r["V1"] > r["score"] + 1e-9 for r in paired),
        "tied": sum(abs(r["V1"] - r["score"]) <= 1e-9 for r in paired),
        "reference_lower": sum(r["V1"] < r["score"] - 1e-9 for r in paired),
    }}
    categories = []
    for category in sorted({row["category"] for row in rows}):
        available = [row for row in scored if row["category"] == category]
        categories.append({
            "category": category, "graded": len(available),
            "planned": sum(row["category"] == category for row in rows),
            "v11_mean": mean([row["score"] for row in available]),
        })
    batch_path = runner.RUNS / "batch.json"
    state = runner.load(batch_path) if batch_path.is_file() else {}
    historical_audit = OUT / "final_audit.json"
    audit_reference = None
    if historical_audit.is_file():
        proof = runner.load(historical_audit)
        audit_reference = {
            "path": str(historical_audit.relative_to(runner.ROOT)),
            "sha256": sha256(historical_audit),
            "original_summary_sha256": proof.get("summary_sha256"),
            "applies_to_regenerated_summary": False,
        }
    summary = {
        "version": "V1.1", "generated_at_utc": runner.now(),
        "batch_status": state.get("status", "unavailable"),
        "complete": len(scored) == 24, "graded_tasks": len(scored), "planned_tasks": 24,
        "model": plan["model"], "reasoning_effort": plan["reasoning_effort"],
        "official_task_skills": plan["official_task_skills"],
        "active_task": state.get("active"),
        "mean_on_graded_tasks": mean([row["score"] for row in scored]),
        "full_batch_mean": mean([row["score"] for row in scored]) if len(scored) == 24 else None,
        "comparisons": comparisons, "categories": categories, "tasks": rows,
        "derivation": "Saved result.json scores; no model calls, regrading, or new runtime audit.",
        "source_hashes": sources, "historical_audit": audit_reference,
        "limitations": [
            "One rollout per task; public web and environment can change between runs.",
            "V1 uses different solver and judge models; comparison is descriptive only.",
            "Complete means 24 finite saved grades, not a rerun of the historical runtime audit.",
            "Original audit receipts refer to their original summary hashes.",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT / "summary.json", summary)
    with (OUT / "paired.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    fmt = lambda value: "—" if value is None else f"{value:.4f}"
    comparison = comparisons["V1"]
    lines = [
        "# V1.1：GPT-5.6 Sol / medium 无历史资源基线", "",
        f"有效评分：{len(scored)}/24；平均得分：{fmt(summary['mean_on_graded_tasks'])}。", "",
        "原 A0 提示词，Codex 原生工具，每题独立工作区，求解与评分各 1800 秒；不提供历史轨迹、蒸馏经验技能、检索 MCP 或跨题更新。",
        "官方任务自带浏览器／接口说明：" + ("保留" if plan["official_task_skills"] == "preserved" else "移除") + "。", "",
        "本报告从保存的 result.json 重新整理，未调用模型、重新求解或评分，也未重做完整运行审计。历史审计回执仅适用于其中记录的原始 summary 哈希。", "",
        "## V1 历史参考", "",
        "| 对照 | 配对题数 | V1 均分 | V1 减 V1.1 | V1 高/平/低 |",
        "|---|---:|---:|---:|---|",
        f"| V1 / A0 | {comparison['paired_tasks']} | {fmt(comparison['reference_mean'])} | {fmt(comparison['reference_minus_v11'])} | {comparison['reference_higher']}/{comparison['tied']}/{comparison['reference_lower']} |", "",
        "V1 使用不同的 solver 与 judge 模型，比较仅作描述性历史参考。每题每条件一次运行，保留低分和超时首次产物。", "",
        "## 类别", "", "| 类别 | 已评分/计划 | V1.1 |", "|---|---:|---:|",
    ]
    lines.extend(
        f"| {r['category']} | {r['graded']}/{r['planned']} | {fmt(r['v11_mean'])} |"
        for r in categories
    )
    labels = {"completed": "正常结束", "timeout": "达到时限", None: "—"}
    lines += ["", "## 逐题", "", "| 顺序 | 任务 | V1.1 | V1 | 求解状态 |", "|---|---|---:|---:|---|"]
    lines.extend(
        f"| {r['index'] + 1} | {r['task_id']} | {fmt(r['score'])} | {fmt(r['V1'])} | {labels.get(r['execution_status'], r['execution_status'])} |"
        for r in rows
    )
    (OUT / "summary_zh.md").write_text("\n".join(lines) + "\n")
    readme = [
        "# V1.1：GPT-5.6 Sol / medium 基线结果", "",
        f"原始保存结果：**{len(scored)}/24 题，平均得分 {fmt(summary['mean_on_graded_tasks'])}**。沿用 V1 的 A0 提示词，不提供历史轨迹、蒸馏经验技能或检索 MCP；保留官方任务接口说明。", "",
        "- [方法与运行](../../variants/vanilla_sol/README.md)",
        "- [逐题报告](summary_zh.md) · [CSV](paired.csv) · [结构化结果](summary.json)",
        "- [V1 历史基线](../baseline/README.md)", "",
        "本页与汇总由保存的 result.json 重新整理，未调用模型、重新求解或评分。原始结果没有改写。完整历史运行审计未重做，旧审计回执对应其原始 summary 哈希，不代表重新生成的汇总已通过相同审计。", "",
        "V1 使用 Astra / ultra，模型不同，仅作历史参考。每条件每题一次求解；有效低分和超时首次产物保留。",
    ]
    (OUT / "README.md").write_text("\n".join(readme) + "\n")
    print(json.dumps({key: summary[key] for key in ("complete", "graded_tasks", "full_batch_mean")}), flush=True)
    return summary


def finalize():
    state = runner.load(runner.RUNS / "batch.json")
    plan = runner.load(runner.PROTOCOL)
    if state["status"] != "completed" or len(state["completed"]) != 24 or state["active"] is not None:
        raise ValueError("All 24 audited tasks must finish before finalization")
    runner.verify_plan(plan)
    for row, saved in zip(plan["tasks"], state["completed"]):
        if runner.audit_result(row, plan) != saved:
            raise ValueError("Final task audit differs from recorded result")
    report = main()
    proof = {
        "status": "passed", "checked_at_utc": runner.now(), "tasks": 24,
        "model": plan["model"], "reasoning_effort": plan["reasoning_effort"],
        "original_A0_prompt_verified": True, "resources_absent_all_tasks": True,
        "official_task_skills": plan["official_task_skills"],
        "actual_solver_models_verified": True, "all_grades_valid": True,
        "frozen_source_and_prior_results_verified": True,
        "batch_sha256": sha256(runner.RUNS / "batch.json"),
        "summary_sha256": sha256(OUT / "summary.json"),
        "full_batch_mean": report["full_batch_mean"], "model_calls_in_finalization": 0,
    }
    atomic_json(OUT / "completion_verification.json", proof)
    return proof


if __name__ == "__main__":
    main()
