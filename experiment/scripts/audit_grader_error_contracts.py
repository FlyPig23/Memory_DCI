#!/usr/bin/env python3
"""Extract only error-contract structure from frozen Automated Checks ASTs.

No task is executed. Prompts, gold, expected answers, and unrelated string
constants are neither inspected for semantics nor emitted.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
FIELDS = {"error", "judge_error", "llm_error", "llm_judge_error", "judge_method", "mode", "parse_error"}


def code_section(markdown: str) -> str:
    match = re.search(r"^##\s+Automated Checks\s*$\n(.*?)(?=^##\s+|\Z)", markdown, re.M | re.S)
    if not match:
        return ""
    code = match.group(1).strip()
    code = re.sub(r"^```[^\n]*\n?", "", code)
    return re.sub(r"\n?```$", "", code).strip()


def expression_contract(node: ast.AST) -> dict:
    """Retain only strings used directly as selected error-field values."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return {"kind": "literal", "value": node.value}
    if isinstance(node, ast.JoinedStr):
        return {"kind": "template", "value": "".join(
            item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else "{dynamic}"
            for item in node.values)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        return {"kind": "format_template", "value": node.left.value}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = [expression_contract(node.left), expression_contract(node.right)]
        return {"kind": "template", "value": "".join(
            str(part["value"]) if part["kind"] in ("literal", "template") else "{dynamic}" for part in parts)}
    if isinstance(node, ast.IfExp):
        return {"kind": "alternatives", "values": [expression_contract(node.body), expression_contract(node.orelse)]}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
        return {"kind": "format_template", "value": node.func.value.value}
    if isinstance(node, ast.Name):
        return {"kind": "dynamic_name", "name": node.id}
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else "call"
        return {"kind": "dynamic_call", "name": name}
    return {"kind": "dynamic_expression", "expression_type": type(node).__name__}


def extract_contracts(code: str) -> list[dict]:
    tree = ast.parse(code)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    contracts = []

    def add(node, field, value, origin, score=None):
        parent, function, handlers = node, None, []
        while parent in parents:
            parent = parents[parent]
            if function is None and isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = parent.name
            if isinstance(parent, ast.ExceptHandler):
                exception = parent.type
                handlers.append(exception.id if isinstance(exception, ast.Name) else type(exception).__name__ if exception else "bare")
        row = {"field": field, "value": expression_contract(value), "origin": origin,
               "checks_line": node.lineno, "function": function, "exception_handlers": handlers}
        if score is not None:
            row["overall_score_contract"] = expression_contract(score)
        contracts.append(row)

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            score = next((value for key, value in zip(node.keys, node.values)
                          if isinstance(key, ast.Constant) and key.value == "overall_score"), None)
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in FIELDS:
                    add(node, key.value, value, "dict_field", score)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            def assignments(target, value):
                if isinstance(target, (ast.Tuple, ast.List)):
                    values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else [value] * len(target.elts)
                    for child, child_value in zip(target.elts, values):
                        assignments(child, child_value)
                elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in FIELDS:
                    add(node, target.slice.value, value, "subscript_assignment")
                elif isinstance(target, ast.Name) and target.id in FIELDS:
                    add(node, target.id, value, "named_assignment")
            if node.value is not None:
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    assignments(target, node.value)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in FIELDS:
                    add(node, keyword.arg, keyword.value, "keyword_argument")
    return sorted(contracts, key=lambda row: (row["checks_line"], row["field"], row["origin"]))


def main() -> None:
    manifest = json.loads((ROOT / "experiment/manifests/task_manifest.json").read_text())
    repo = ROOT / "experiment/vendor/WildClawBench"
    rows = []
    for task in manifest["tasks"]:
        source = repo / task["source_path"]
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != task["source_sha256"]:
            raise RuntimeError("Frozen task source hash mismatch")
        checks = code_section(raw.decode())
        rows.append({"task_id": task["task_id"], "source_sha256": task["source_sha256"],
                     "checks_sha256": hashlib.sha256(checks.encode()).hexdigest(),
                     "error_contracts": extract_contracts(checks) if checks else []})
    result = {"schema_version": 1, "task_count": len(rows), "selected_fields": sorted(FIELDS),
              "policy": "Controller-only static AST extraction; no task execution, Prompt output, gold reads, answer-constant extraction, or rubric changes.",
              "extractor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "classifier_source_sha256": hashlib.sha256((ROOT / "experiment/src/task_runtime.py").read_bytes()).hexdigest(),
              "classification_policy": {
                  "reviewed_artifact_errors_with_official_zero": "retain graded zero",
                  "missing_verifier_inputs_or_explicit_judge_errors": "judge_error",
                  "missing_audit_or_known_rule_fallback": "judge_degraded",
                  "unknown_error_or_unknown_fallback": "needs_review",
                  "transcript_parse_error": "included for completeness; helper transcript records are not automatically score fields",
              }, "tasks": rows}
    target = ROOT / "experiment/manifests/grader_error_contracts.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest": str(target.relative_to(ROOT)), "tasks": len(rows),
                      "contracts": sum(len(row["error_contracts"]) for row in rows)}))


if __name__ == "__main__":
    main()
