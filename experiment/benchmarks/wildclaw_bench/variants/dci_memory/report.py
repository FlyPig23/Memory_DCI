"""Build descriptive V5 results from saved evidence, without model calls."""
import csv
import json
from pathlib import Path
import statistics

from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256
from . import runner
from experiment.shared.memory.tools import audit_memory


def report(root=runner.ROOT):
    plan = json.loads((root / runner.VARIANT / 'prepared/formal/protocol.json').read_text())
    history = {version: {r['task_id']: r for r in json.loads((root / f'experiment/benchmarks/wildclaw_bench/reports/{folder}/summary.json').read_text())['tasks']}
               for version, folder in [('v11', 'vanilla_sol')]}
    rows, incomplete = [], []
    for row in plan['tasks']:
        directory = root / runner.RUNS / 'formal' / row['run_id']
        if not (directory / 'result.json').exists():
            incomplete.append(row['run_id'])
            continue
        try:
            record = runner.audit_result(row, root)
        except ValueError as error:
            incomplete.append({'run_id': row['run_id'], 'error': str(error)})
            continue
        original = json.loads((directory / 'result.json').read_text())
        online = audit_memory(directory / 'memory_after_solver')
        final = audit_memory(directory / 'memory')
        record.update(online_entries=online['entry_count'], final_entries=final['entry_count'],
                      online_event_counts=online['event_counts'],
                      total_tokens=record['solver_tokens'] + record['review_tokens'],
                      total_seconds=record['solver_seconds'] + record['review_seconds'],
                      solver_status=original['execution']['status'])
        for version in ('v11',):
            past = history[version][row['task_id']]
            record[version + '_score'] = past['score']
            record[version + '_tokens'] = past['total_tokens']
            record[version + '_seconds'] = past.get('solve_seconds')
        rows.append(record)
    mean = lambda key: statistics.mean(r[key] for r in rows) if rows else None
    summary = {'version': 'V5', 'status': 'complete' if len(rows) == 24 and not incomplete else 'partial',
        'model': runner.MODEL, 'reasoning_effort': runner.EFFORT, 'scored_runs': len(rows),
        'incomplete': incomplete, 'mean_score': mean('overall_score'),
        'average_solver_tokens': mean('solver_tokens'), 'average_solver_seconds': mean('solver_seconds'),
        'average_review_tokens': mean('review_tokens'), 'average_review_seconds': mean('review_seconds'),
        'average_total_tokens': mean('total_tokens'), 'average_total_seconds': mean('total_seconds'),
        'total_solver_tokens': sum(r['solver_tokens'] for r in rows),
        'total_review_tokens': sum(r['review_tokens'] for r in rows),
        'tasks_with_online_entries': sum(r['online_entries'] > 0 for r in rows),
        'tasks_with_initial_reason_then_task_search': sum(r['workflow']['initial_reason_then_task_search'] for r in rows),
        'tasks_with_memory_search_after_online_write': sum(r['workflow']['memory_search_after_online_write'] for r in rows),
        'memory_initial_entries': 0, 'memory_cross_task_transfer': False,
        'final_review_decisions': {k: sum(r['final_decision'] == k for r in rows) for k in ('write', 'no_update')},
        'source_protocol_sha256': sha256(root / runner.VARIANT / 'prepared/formal/protocol.json'), 'tasks': rows}
    output = root / 'experiment/benchmarks/wildclaw_bench/reports/dci_memory'
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / 'summary.json', summary)
    fields = ['task_id', 'v11_score', 'overall_score', 'solver_tokens', 'review_tokens', 'total_tokens',
              'solver_seconds', 'review_seconds', 'total_seconds', 'online_entries', 'final_entries', 'final_decision']
    with (output / 'per_task.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    text = ['# V5：DCI + 每题独立 memory', '', f'状态：{summary["status"]}；有效评分 {len(rows)}/24。', '',
        '36 个 training tasks / 432 条带分数命名的原始轨迹只读；24 个 test tasks 每题从空 memory 开始，不跨题传递。',
        'GPT-5.6 Sol / medium，单次 solver rollout；求解中可以反复检索和蒸馏，结束后独立复盘，再按原规则评分。', '',
        '最终复盘发生在答案冻结之后，不计为帮助本题。token 为累计 input+output（包括缓存输入），不代表订阅扣费。', '',
        '| 指标 | V5 |', '|---|---|']
    for label, key in [('平均分', 'mean_score'), ('平均求解 tokens', 'average_solver_tokens'),
                       ('平均求解秒数', 'average_solver_seconds'), ('平均复盘 tokens', 'average_review_tokens'),
                       ('平均复盘秒数', 'average_review_seconds'), ('平均总 tokens', 'average_total_tokens'),
                       ('平均总秒数', 'average_total_seconds'), ('求解中写过 memory 的题数', 'tasks_with_online_entries')]:
        value = summary[key]
        text.append(f'| {label} | {value:.4f} |' if isinstance(value, (float, int)) else f'| {label} | 待完成 |')
    text += ['', '完整逐题对比：`per_task.csv`（见 [数据与恢复](../../../DATA.md)）；原始汇总：`summary.json`（见 [数据与恢复](../../../DATA.md)）。', '',
             'V5 同时改变评分可见性、检索策略、工具接口和每题 memory，不能单独将分差归因于 memory。']
    (output / 'README.md').write_text('\n'.join(text) + '\n')
    return summary


if __name__ == '__main__':
    summary = report()
    print(json.dumps({k:v for k,v in summary.items() if k != 'tasks'}, ensure_ascii=False))
