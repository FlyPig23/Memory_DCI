"""Extend existing Markdown V5 reports with a source-checked paired comparison.

Only completed V5 summary rows define the comparison cohort. This program
reads saved evidence and writes comparison.md/csv/json; no experiment runs,
audits, models, or grades are invoked or changed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256

FOLDER = Path('experiment/benchmarks/wildclaw_bench/reports/dci_memory')
SOURCES = {
    'v5_summary': FOLDER / 'summary.json', 'v5_audit': FOLDER / 'final_audit.json',
    'v11_summary': Path('experiment/benchmarks/wildclaw_bench/reports/vanilla_sol/summary.json'),
    'v5_protocol': Path('experiment/benchmarks/wildclaw_bench/variants/dci_memory/prepared/formal/protocol.json'),
    'v11_protocol': Path('experiment/benchmarks/wildclaw_bench/variants/vanilla_sol/prepared/formal/protocol.json'),
    'frozen_split': Path('experiment/benchmarks/wildclaw_bench/manifests/split.json'),
    'task_families': Path('experiment/benchmarks/wildclaw_bench/manifests/task_families.json'),
    'v5_pool_manifest': Path('experiment/benchmarks/wildclaw_bench/variants/dci_memory/prepared/pool/manifest.json'),
}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _same(left, right):
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def _number(value, name, *, optional=False, integer=False):
    if value is None and optional:
        return value
    _require(type(value) in ((int,) if integer else (int, float))
             and math.isfinite(value) and value >= 0, f'Invalid/missing {name}')
    return value


def _index(rows):
    result = {r['task_id']: r for r in rows}
    _require(len(rows) == len(result), 'Duplicate task IDs in source summary')
    return result


def _stats(values):
    known = [v for v in values if v is not None]
    return {'count': len(known), 'missing': len(values) - len(known),
            'total': sum(known) if known else None, 'mean': statistics.mean(known) if known else None}


def _delta(value, baseline):
    difference = value - baseline
    return {'difference': difference, 'percent': 100 * difference / baseline if baseline else None}


def _label(task_id):
    match = re.fullmatch(r'(\d\d)_.+_task_(\d+)_(.+)', task_id)
    return f'{match[1]}/{int(match[2]):02d} · {match[3]}' if match else task_id


def _fmt(value, places=2, *, plus=False):
    if value is None:
        return '缺失'
    return f'{value:+,.{places}f}' if plus else f'{value:,.{places}f}'


def build(root=ROOT):
    # Each JSON is read once, so its hash describes the exact snapshot used.
    payloads = {key: (root / path).read_bytes() for key, path in SOURCES.items()}
    documents = {key: json.loads(value) for key, value in payloads.items()}
    hashes = {key: hashlib.sha256(value).hexdigest() for key, value in payloads.items()}
    plan = documents['v5_protocol']
    v5, old11 = (documents[k] for k in ('v5_summary', 'v11_summary'))
    audits = {'v5': documents['v5_audit']}
    _require(v5['source_protocol_sha256'] == hashes['v5_protocol'], 'V5 summary/protocol mismatch')
    _require(plan['model'] == v5['model'] == old11['model'] == 'gpt-5.6-sol',
             'Comparison requires the same GPT-5.6 Sol model')
    _require(plan['reasoning_effort'] == v5['reasoning_effort'] == old11['reasoning_effort'] == 'medium', 'Comparison requires medium reasoning')
    control_keys = ('model', 'reasoning_effort', 'judge_model', 'judge_reasoning_effort',
                    'cli_version', 'solver_image_id', 'timeout_seconds', 'grading_timeout_seconds')
    controls = {key: plan[key] for key in control_keys}
    for baseline_name in ('v11',):
        _require(all(documents[baseline_name + '_protocol'].get(key) == value
                     for key, value in controls.items()),
                 'Frozen model/runtime/budget controls differ: ' + baseline_name)
    _require(old11['complete'], 'Baseline must contain all planned grades')
    baseline = {'v11': _index(old11['tasks'])}
    schedule = _index(plan['tasks'])
    _require(len(schedule) == 24 and all(set(baseline[k]) == set(schedule) for k in baseline),
             'Historical task IDs differ from the frozen V5 schedule')
    _require(_same(statistics.mean(r['score'] for r in old11['tasks']), old11['full_batch_mean']),
             'Historical summary aggregate disagrees with its task rows')
    v5_rows = _index(v5['tasks'])
    _require(set(v5_rows).issubset(schedule) and len(v5_rows) == v5['scored_runs'], 'V5 completed cohort invalid')
    audit_rows = _index(audits['v5']['tasks'])
    rows, result_sources = [], []
    for task_id in sorted(v5_rows, key=lambda t: schedule[t]['index']):
        source = v5_rows[task_id]
        planned = schedule[task_id]
        _require(source['run_id'] == planned['run_id'], 'V5 run-to-task mapping changed')
        result_path = Path('experiment/benchmarks/wildclaw_bench/runs/dci_memory/formal') / source['run_id'] / 'result.json'
        encoded = (root / result_path).read_bytes()
        result_hash = hashlib.sha256(encoded).hexdigest()
        record = json.loads(encoded)
        _require(source['result_sha256'] == result_hash, f'V5 summary is stale for {task_id}; regenerate it first')
        _require(record['status'] == 'finished' and record['evaluation']['status'] == 'graded'
                 and record['task_id'] == task_id, 'V5 source record is not a valid completed grade')
        pairs = {
            'overall_score': record['evaluation']['overall_score'],
            'solver_tokens': record['execution']['usage']['total_tokens'],
            'solver_seconds': record['execution']['elapsed_seconds'],
            'review_tokens': record['memory_review']['execution']['usage']['total_tokens'],
            'review_seconds': record['memory_review']['execution']['elapsed_seconds'],
            'final_decision': record['memory_review']['decision']['decision'],
            'online_entries': record['online_memory']['entry_count'],
            'final_entries': record['final_memory']['entry_count'],
        }
        _require(all(_same(source[key], value) for key, value in pairs.items()),
                 f'V5 summary values differ from completed evidence: {task_id}')
        _require(source['total_tokens'] == source['solver_tokens'] + source['review_tokens']
                 and _same(source['total_seconds'], source['solver_seconds'] + source['review_seconds']),
                 'Combined V5 cost must equal solver plus final review')
        old = {key: baseline[key][task_id] for key in baseline}
        baseline_path = Path('experiment/benchmarks/wildclaw_bench/runs/vanilla_sol/formal') / baseline['v11'][task_id]['run_id'] / 'result.json'
        baseline_record = json.loads((root / baseline_path).read_bytes())
        _require(baseline_record['task_id'] == task_id
                 and baseline_record['evaluation']['status'] == 'graded'
                 and _same(baseline_record['evaluation']['overall_score'], baseline['v11'][task_id]['score']),
                 'Baseline summary differs from saved task grade')
        result_sources.append({'path': str(baseline_path), 'sha256': sha256(root / baseline_path)})
        row = {'index': planned['index'], 'task_id': task_id, 'label': _label(task_id),
               'category': task_id.split('_task_')[0], 'run_id': source['run_id'],
               'v11_score': _number(old['v11']['score'], 'V1.1 score'),
               'v5_score': _number(source['overall_score'], 'V5 score'),
               'v11_solver_tokens': _number(old['v11']['total_tokens'], 'V1.1 tokens', integer=True),
               'v5_solver_tokens': _number(source['solver_tokens'], 'V5 tokens', integer=True),
               'v5_review_tokens': _number(source['review_tokens'], 'V5 review tokens', integer=True),
               'v5_combined_tokens': source['total_tokens'],
               'v11_solver_seconds': _number(old['v11'].get('solve_seconds'), 'V1.1 time', optional=True),
               'v5_solver_seconds': _number(source['solver_seconds'], 'V5 time'),
               'v5_review_seconds': _number(source['review_seconds'], 'V5 review time'),
               'v5_combined_seconds': source['total_seconds'],
               'solver_snapshot_entries': source['online_entries'], 'final_entries': source['final_entries'],
               'online_write_decisions': source['workflow']['online_write_decisions'],
               'solver_final_decision': (record['online_memory'].get('final_decision') or {}).get('decision'),
               'final_decision': source['final_decision'], 'initial_reason_then_task_search': source['workflow']['initial_reason_then_task_search'],
               'trajectory_search_calls': source['workflow']['trajectory_search_calls'],
               'memory_search_calls': source['workflow']['memory_search_calls'],
               'memory_search_after_online_write': source['workflow']['memory_search_after_online_write'],
               'solver_status': source['solver_status'], 'result_sha256': result_hash}
        for prior in ('v11',):
            row['v5_minus_' + prior + '_score'] = row['v5_score'] - row[prior + '_score']
            _require(_same(source[prior + '_score'], row[prior + '_score'])
                     and _same(source[prior + '_tokens'], row[prior + '_solver_tokens'])
                     and _same(source[prior + '_seconds'], row[prior + '_solver_seconds']),
                     'Embedded historical comparison differs from audited historical summary')
        audit = audit_rows.get(task_id, {})
        check_rows = audit.get('checks', [])
        observed_hash = next((c.get('evidence', {}).get('result_sha256') for c in check_rows
                              if c['requirement'] == 'finished_matching_scheduled_run'), None)
        row['v5_requirement_audit_current'] = (audit.get('status') == 'valid' and bool(check_rows)
                                              and all(c['passed'] for c in check_rows) and observed_hash == result_hash)
        rows.append(row)
        result_sources.append({'path': str(result_path), 'sha256': result_hash})

    aggregates = {}
    for variant in ('v11', 'v5'):
        aggregates[variant] = {'scores': _stats([r[variant + '_score'] for r in rows]),
                               'solver_tokens': _stats([r[variant + '_solver_tokens'] for r in rows]),
                               'solver_seconds': _stats([r[variant + '_solver_seconds'] for r in rows]),
                               'missing_time_task_ids': [r['task_id'] for r in rows if r[variant + '_solver_seconds'] is None]}
    for metric in ('review_tokens', 'review_seconds', 'combined_tokens', 'combined_seconds'):
        aggregates['v5'][metric] = _stats([r['v5_' + metric] for r in rows])
    comparisons = {}
    for prior in ('v11',):
        time_pairs = [r for r in rows if r[prior + '_solver_seconds'] is not None]
        wins = sum(r['v5_score'] > r[prior + '_score'] and not _same(r['v5_score'], r[prior + '_score']) for r in rows)
        ties = sum(_same(r['v5_score'], r[prior + '_score']) for r in rows)
        entry = {'tasks': len(rows), 'wins': wins, 'ties': ties, 'losses': len(rows) - wins - ties,
                 'mean_score_difference': statistics.mean(r['v5_score'] - r[prior + '_score'] for r in rows) if rows else None,
                 'paired_time_tasks': len(time_pairs), 'paired_time_task_ids': [r['task_id'] for r in time_pairs],
                 'excluded_missing_time_task_ids': [r['task_id'] for r in rows if r[prior + '_solver_seconds'] is None]}
        if rows:
            entry['solver_tokens'] = _delta(aggregates['v5']['solver_tokens']['total'], aggregates[prior]['solver_tokens']['total'])
            entry['combined_tokens'] = _delta(aggregates['v5']['combined_tokens']['total'], aggregates[prior]['solver_tokens']['total'])
        if time_pairs:
            prior_time = statistics.mean(r[prior + '_solver_seconds'] for r in time_pairs)
            v5_time = statistics.mean(r['v5_solver_seconds'] for r in time_pairs)
            combined = statistics.mean(r['v5_combined_seconds'] for r in time_pairs)
            entry['paired_time'] = {'baseline_mean_seconds': prior_time, 'v5_solver_mean_seconds': v5_time,
                                    'v5_combined_mean_seconds': combined,
                                    'solver_change': _delta(v5_time, prior_time), 'combined_change': _delta(combined, prior_time)}
        comparisons[prior] = entry
    categories = []
    for category in sorted({t.split('_task_')[0] for t in schedule}):
        subset = [r for r in rows if r['category'] == category]
        categories.append({'category': category, 'n': len(subset),
                           **{key + '_mean_score': statistics.mean(r[key + '_score'] for r in subset) if subset else None
                              for key in ('v11', 'v5')}})
    memory = {'tasks_with_solver_snapshot_entries': sum(r['solver_snapshot_entries'] > 0 for r in rows),
              'solver_snapshot_entries_total': sum(r['solver_snapshot_entries'] for r in rows),
              'tasks_with_online_write_decisions': sum(r['online_write_decisions'] > 0 for r in rows),
              'online_write_decisions': sum(r['online_write_decisions'] for r in rows),
              'final_entries_total': sum(r['final_entries'] for r in rows),
              'final_decisions': {value: sum(r['final_decision'] == value for r in rows) for value in ('write', 'no_update')},
              'solver_final_decisions': {value: sum((r['solver_final_decision'] or 'missing') == value for r in rows)
                                         for value in ('write', 'no_update', 'missing')},
              'tasks_with_initial_reason_then_task_search': sum(r['initial_reason_then_task_search'] for r in rows),
              'tasks_with_trajectory_search': sum(r['trajectory_search_calls'] > 0 for r in rows),
              'trajectory_search_calls': sum(r['trajectory_search_calls'] for r in rows),
              'tasks_with_memory_search': sum(r['memory_search_calls'] > 0 for r in rows),
              'memory_search_calls': sum(r['memory_search_calls'] for r in rows),
              'tasks_with_memory_search_after_online_write': sum(r['memory_search_after_online_write'] for r in rows),
              'initial_entries_per_task': 0, 'cross_task_transfer': False,
              'entry_count_scope': 'Sum of task-private entry counts; the same topic in two tasks counts twice.'}
    current_audits = sum(r['v5_requirement_audit_current'] for r in rows)
    final = (len(rows) == 24 and v5['status'] == 'complete' and audits['v5'].get('complete') is True
             and audits['v5'].get('valid_tasks') == 24 and not audits['v5'].get('invalid_tasks')
             and audits['v5']['protocol_sha256'] == hashes['v5_protocol']
             and all(c['passed'] for c in audits['v5']['checks']) and current_audits == 24)
    output = {
        'schema_version': 2,
        'audit_scope': 'Saved run audits and original protocol/result hashes; this report does not re-audit the refactored source against historical source hashes.', 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'complete_audited' if final else ('partial' if len(rows) < 24 else 'awaiting_final_audit'),
        'final_audited': final, 'cohort_tasks': len(rows), 'planned_tasks': 24, 'current_requirement_audits': current_audits,
        'cohort_policy': 'Exactly the completed scored task IDs in the current V5 summary; all versions restricted to that cohort.',
        'model': 'gpt-5.6-sol', 'reasoning_effort': 'medium', 'aggregates': aggregates,
        'shared_frozen_controls': controls,
        'judge_verification_scope': {
            'v11': 'Source result scores verified against the baseline summary. Original judge audit is preserved in the evidence dataset.',
            'v5': 'Current audit joins grading-window gateway requests to manifests, executions and raw turn_context model/effort evidence for completed tasks.',
        },
        'paired_comparisons_v5_minus_baseline': comparisons, 'categories': categories, 'memory': memory, 'tasks': rows,
        'metric_definitions': {
            'score': 'Frozen official task score on [0,1], not binary success rate.',
            'tokens': 'Recorded cumulative main model input+output, including cached input and repeated context; cached input is not added twice. Not monetary billing.',
            'solver_time': 'Recorded solver elapsed seconds, including retrieval and online distillation; excludes setup, hidden grading and separate final review.',
            'combined_cost': 'V5 solver + final review only; grading/setup costs excluded. Final review follows answer freeze.',
            'resumed_review_time': 'Run01 uses conservative original container active interval plus resumed CLI elapsed; inactive interruption gap excluded.',
            'trailing_usage_correction': 'Run06 solver usage includes its recorded final response after the last token_count; independently verified response sums and thread totals give 1,808,532 tokens. Score, duration and frozen evidence are unchanged; receipt is in controller-usage/usage_correction.json.',
            'missing_time': 'Missing elapsed time stays null; recorded-time summaries report their denominator; comparisons use shared known-time pairs.',
            'solver_snapshot_entries': 'Entry count in memory_after_solver; includes any solver stage=final writes. Actual stage=online write operations are counted separately.',
        },
        'interpretation_limit': 'V5 changes scored trajectory exposure, retrieval policy, explicit tools and task-local memory together. Recorded reads/writes and single rollouts do not isolate causal memory benefit; post-answer final review cannot improve that answer or other tasks.',
        'sources': {key: {'path': str(path), 'sha256': hashes[key]} for key, path in SOURCES.items()},
        'v5_result_sources': result_sources, 'generator_sha256': sha256(Path(__file__)),
    }
    folder = root / FOLDER
    atomic_json(folder / 'comparison.json', output)
    fields = ['index', 'task_id', 'category', 'v11_score', 'v5_score',
              'v5_minus_v11_score', 'v11_solver_tokens',
              'v5_solver_tokens', 'v5_review_tokens', 'v5_combined_tokens',
              'v11_solver_seconds', 'v5_solver_seconds', 'v5_review_seconds', 'v5_combined_seconds',
              'solver_snapshot_entries', 'final_entries', 'online_write_decisions', 'solver_final_decision', 'final_decision',
              'initial_reason_then_task_search', 'trajectory_search_calls', 'memory_search_calls', 'memory_search_after_online_write',
              'v5_requirement_audit_current', 'solver_status', 'run_id', 'result_sha256']
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    (folder / 'comparison.csv').write_text(buffer.getvalue())
    (folder / 'comparison.md').write_text(render(output))
    # The frozen generator's legacy label counts all entries in the solver
    # snapshot, including stage=final writes. Correct only the rendered label;
    # experiment code, raw fields and all values remain unchanged.
    readme = folder / 'README.md'
    if readme.exists():
        text = readme.read_text().replace(
            '| 求解中写过 memory 的题数 |',
            '| solver 结束快照有 memory 条目的题数 |')
        note = '实际在线写入以 `stage=online` 的 write 事件为准；快照还可能包含 solver 的 `stage=final` 写入。详见 [同题对照与 memory 行为统计](comparison.md)。'
        if note not in text:
            text = text.rstrip() + '\n\n' + note + '\n'
        readme.write_text(text)
    return output


def render(data):
    rows, agg = data['tasks'], data['aggregates']
    c = data['paired_comparisons_v5_minus_baseline']['v11']
    lines = ['# V5 与 V1.1 同题比较', '',
        f"有效评分：{len(rows)}/24 题。记录审计匹配：{data['current_requirement_audits']}/{len(rows)}。", '',
        '两组采用 GPT-5.6 Sol / medium。V5 额外提供带分数的训练轨迹、检索工具、每题独立 memory 和答案冻结后的复盘。', '',
        '| 条件 | 平均分 | 求解 tokens | 复盘 tokens |', '|---|---:|---:|---:|',
        f"| V1.1 | {agg['v11']['scores']['mean']:.6f} | {agg['v11']['solver_tokens']['total']:,} | — |",
        f"| V5 | {agg['v5']['scores']['mean']:.6f} | {agg['v5']['solver_tokens']['total']:,} | {agg['v5']['review_tokens']['total']:,} |", '',
        f"配对均分差：{c['mean_score_difference']:+.6f}；V5 高 / 平 / 低：{c['wins']} / {c['ties']} / {c['losses']}。", '',
        '| 任务 | V1.1 | V5 | 差值 |', '|---|---:|---:|---:|']
    for r in rows:
        lines.append(f"| {r['label']} | {r['v11_score']:.4f} | {r['v5_score']:.4f} | {r['v5_minus_v11_score']:+.4f} |")
    m = data['memory']
    lines += ['', f"初始 reason → task search：{m['tasks_with_initial_reason_then_task_search']}/{len(rows)}；明确在线写入：{m['tasks_with_online_write_decisions']} 题。", '',
        f"最终独立复盘：write {m['final_decisions']['write']} 题，no_update {m['final_decisions']['no_update']} 题。每题从空 memory 开始，经验不传给下一题。", '',
        '每条件每题仅运行一次，差异不能单独归因于 memory。复盘发生在答案冻结后，不能改善本题答案；tokens 含缓存输入和累计上下文，不代表美元费用。', '',
        '本报告核对保存的 result、协议及原始审计；整理后的源代码与历史冻结代码并不相同，不据此声称重新通过全部运行时审计。', '',
        '完整逐题成本、来源哈希及工作流记录在 Hugging Face 数据集；先按根目录 DATA.md 下载 evidence，再运行：', '',
        '```bash', 'python3 -B experiment/benchmarks/wildclaw_bench/reports/dci_memory/build_comparison.py', '```', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    result = build()
    print(json.dumps({k: result[k] for k in ('status', 'final_audited', 'cohort_tasks', 'planned_tasks', 'current_requirement_audits')}, ensure_ascii=False))
