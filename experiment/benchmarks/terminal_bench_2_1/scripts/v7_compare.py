"""Descriptive V7 comparison against the frozen baseline, V5 and V6 references.

Reads frozen artifacts only; never runs solvers or graders and never writes into run directories.
The same benchmark-source rule is applied to every arm: a trial that fetched the benchmark's own sources counts 0.
Usage (from the repo root, TB2.1 venv):
  python -m experiment.benchmarks.terminal_bench_2_1.scripts.v7_compare <v7_run_name> [--corpus final_20261007]
Writes reports/v7_same_task_replay/comparison.json (local, git-ignored) and prints a summary. Hand reviews of
environment-tampering candidates are read from reports/v7_same_task_replay/tamper_reviews.json.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / 'reports/v7_same_task_replay'
REFERENCE = BASE / 'reports/tb21_report_app/src/data.json'
REFERENCE_SHA256 = '12d5738c69378ce7915fbf9f48984743069492f7991ab6f560fb48bc586caf77'
V6_RUN = 'v6_seeded_sol_medium_20261005'
# Fetching the benchmark's own task sources, tests, solutions or other trajectories (incl. mirrors and write-ups).
BENCHMARK_SOURCE = re.compile(
    r"tbench\.ai|harbor[-_]?framework|laude[-_]institute|marginlab\.ai|spylab\.ai/notes/terminal-bench|original-tasks/|"
    r"supabase\.co|huggingface\.co/datasets/[^\s'\"]*terminal-bench|github\.com/[^\s'\"]*terminal-bench|"
    r"terminal-bench-(?:1|2|science|core)(?!\.1--tb21)", re.I)
TAMPER = re.compile(r"\.pth\b|sitecustomize|usercustomize|/etc/profile|bashrc|/etc/rc\.local|crontab|/tests/|/logs/verifier")


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def commands(events_path):
    out = []
    for line in Path(events_path).read_text().splitlines() if Path(events_path).exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get('item') or {}
        if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
            out.append(item.get('command', ''))
        if event.get('type') == 'item.completed' and item.get('type') == 'file_change':
            out.extend('file_change ' + c.get('path', '') for c in item.get('changes', []))
    return out


def trial_row(trial, completion, corpus_index, seeds_index):
    task_id = completion['task_id']
    events = [json.loads(x) for x in (trial / 'memory/events.jsonl').read_text().splitlines()] \
        if (trial / 'memory/events.jsonl').exists() else []
    tools = Counter(e.get('tool') for e in events if e.get('process_stage') == 'solve')
    cmds = commands(trial / 'agent/codex.events.jsonl')
    native_memory = sum(('/memory' in c or 'memory_summary.md' in c) and 'tool_outputs' not in c for c in cmds)
    native_pool = sum('/pool' in c for c in cmds)
    fetch = sorted({m.group(0) for c in cmds for m in BENCHMARK_SOURCE.finditer(c)})
    tamper = sorted({m.group(0) for c in cmds for m in TAMPER.finditer(c) if c.startswith('file_change') or '>' in c or 'cp ' in c})
    execution = read(trial / 'agent/execution.json') if (trial / 'agent/execution.json').exists() else {}
    usage = read(trial / 'agent/usage.json') if (trial / 'agent/usage.json').exists() else {}
    seed = (seeds_index.get('tasks') or {}).get(task_id) or {}
    review = (read(trial / 'v7_result.json').get('review') or {}) if (trial / 'v7_result.json').exists() else {}
    final = (review.get('memory_audit') or {}).get('final_decision') or {}
    return {'task_id': task_id, 'reward': completion['reward'], 'valid': completion['valid'],
            'workflow_deviations': completion.get('workflow_deviations'),
            'corpus_trajectories': corpus_index['tasks'][task_id]['trajectory_count'],
            'memory_entries': read(trial / 'memory_initial.json').get('entry_count'),
            'memory_seed': bool(seed.get('seed_path')), 'memory_seed_reason': seed.get('reason'),
            'dci_tool_calls': dict(tools), 'native_pool_commands': native_pool, 'native_memory_commands': native_memory,
            'read_memory': tools.get('DCI_search_memory', 0) > 0 or native_memory > 0,
            'searched_trajectories': tools.get('DCI_search_trajectory', 0) > 0 or native_pool > 0,
            'benchmark_source_access': fetch, 'environment_tampering_candidates': tamper,
            'solver_status': execution.get('status'), 'solver_seconds': execution.get('elapsed_seconds'),
            'solver_tokens': usage.get('total_tokens'),
            'review_valid': review.get('valid'), 'review_seconds': review.get('elapsed_seconds'),
            'review_decision': final.get('decision'), 'final_memory_entries': final.get('entry_count'),
            'evidence': {str((trial / p).relative_to(BASE)): sha(trial / p) for p in
                         ('completion.json', 'agent/codex.events.jsonl', 'memory/events.jsonl', 'memory_initial.json',
                          'runtime_contract.json') if (trial / p).exists()}}


def source_access(trial):
    """Benchmark-source strings in a Codex trial's executed commands (the same rule for every arm)."""
    return sorted({m.group(0) for c in commands(trial / 'agent/codex.events.jsonl') for m in BENCHMARK_SOURCE.finditer(c)})


def paired(rows, key, label, mine='counted_reward'):
    items = [r for r in rows if r.get(key) is not None and r.get(mine) is not None]
    return {'n': len(items), 'v7_passed': sum(r[mine] for r in items),
            f'{label}_passed': sum(r[key] for r in items),
            'wins': [r['task_id'] for r in items if r[mine] > r[key]],
            'losses': [r['task_id'] for r in items if r[mine] < r[key]],
            'ties': sum(r[mine] == r[key] for r in items)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('run_name')
    parser.add_argument('--corpus', default='final_20261007')
    args = parser.parse_args(argv)
    from . import run
    if sha(REFERENCE) != REFERENCE_SHA256:
        raise SystemExit('Frozen reference rows changed')
    reference = {r['task_id']: r for r in read(REFERENCE)['queries']['baseline_recovered_comparison']['rows']}
    v6 = {row['task_id']: row for row in run.status('v6', V6_RUN)['trials']}
    status = run.status('v7', args.run_name)
    corpus = BASE / 'prepared/v7_corpus' / args.corpus
    index, seeds = read(corpus / 'index.json'), read(corpus / 'seeds_index.json')
    plan = read(BASE / 'runs' / args.run_name / 'protocol.json')
    rows = []
    for task in plan['tasks']:
        trial = BASE / 'runs' / args.run_name / 'trials' / task['run_id']
        if not (trial / 'completion.json').exists():
            continue
        row = trial_row(trial, read(trial / 'completion.json'), index, seeds)
        ref = reference[row['task_id']]
        # Same counting rule as the side experiment: fetching benchmark sources counts as 0.
        row['counted_reward'] = 0.0 if row['benchmark_source_access'] else row['reward']
        v6_row = v6.get(row['task_id']) or {}
        baseline_access = source_access(BASE / Path(ref['selected_result_path']).parent)
        v6_access = source_access(BASE / 'runs' / V6_RUN / 'trials' / v6_row['run_id']) if v6_row else []
        row.update(baseline_reward=ref['selected_baseline_reward'], baseline_source=ref['selected_source'],
                   v5_reward=ref['v5_original_reward'] if ref['comparison_eligible'] else None,
                   v6_reward=v6_row.get('reward'), baseline_source_access=baseline_access, v6_source_access=v6_access,
                   baseline_counted=0.0 if baseline_access else ref['selected_baseline_reward'],
                   v6_counted=(0.0 if v6_access else v6_row.get('reward')) if v6_row else None)
        rows.append(row)
    with_corpus = [r for r in rows if r['corpus_trajectories']]
    reviews = read(OUT / 'tamper_reviews.json') if (OUT / 'tamper_reviews.json').exists() else {}
    out = {'run_name': args.run_name, 'status': status['status'], 'completed': status['completed'],
           'reference_sha256': REFERENCE_SHA256, 'corpus': args.corpus,
           'v7_passed_counted': sum(r['counted_reward'] or 0 for r in rows),
           'v7_passed_raw': sum(r['reward'] or 0 for r in rows),
           # Counted: a trial that fetched benchmark sources scores 0, in every arm. Raw: official rewards only.
           'vs_baseline_all': paired(rows, 'baseline_counted', 'baseline'),
           'vs_v6_all': paired(rows, 'v6_counted', 'v6'),
           'vs_v5_eligible': paired(rows, 'v5_reward', 'v5'),
           'vs_baseline_with_corpus': paired(with_corpus, 'baseline_counted', 'baseline'),
           'raw': {'vs_baseline_all': paired(rows, 'baseline_reward', 'baseline', 'reward'),
                   'vs_v6_all': paired(rows, 'v6_reward', 'v6', 'reward')},
           'baseline_failures': {r['task_id']: r['counted_reward'] for r in rows if r['baseline_reward'] == 0},
           'usage': {'tasks_read_memory': sum(r['read_memory'] for r in rows),
                     'tasks_searched_trajectories': sum(r['searched_trajectories'] for r in rows),
                     'tasks_with_memory_seed': sum(r['memory_seed'] for r in rows),
                     'solver_tokens': sum(r['solver_tokens'] or 0 for r in rows),
                     'solver_hours': round(sum(r['solver_seconds'] or 0 for r in rows) / 3600, 2),
                     'review_valid': sum(r['review_valid'] is True for r in rows),
                     'review_decisions': dict(Counter(r['review_decision'] for r in rows)),
                     'tasks_whose_memory_grew': sum((r['final_memory_entries'] or 0) > (r['memory_entries'] or 0) for r in rows)},
           'integrity': {'benchmark_source_access': {r['task_id']: r['benchmark_source_access'] for r in rows
                                                     if r['benchmark_source_access']},
                         'baseline_source_access': {r['task_id']: r['baseline_source_access'] for r in rows
                                                    if r['baseline_source_access']},
                         'v6_source_access': {r['task_id']: r['v6_source_access'] for r in rows if r['v6_source_access']},
                         'tampering_candidates': {r['task_id']: r['environment_tampering_candidates'] for r in rows
                                                  if r['environment_tampering_candidates']},
                         # Pattern hits are candidates only; every hit is read by hand and recorded here.
                         'tampering_reviews': {task: reviews.get(task) for task in
                                               (r['task_id'] for r in rows if r['environment_tampering_candidates'])}},
           'rows': rows}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'comparison.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in out.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
