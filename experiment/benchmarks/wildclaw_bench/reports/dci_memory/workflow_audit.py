"""Read-only workflow evidence supplement; never changes scores or runs models.

The structural requirement audit remains authoritative for data validity.
This supplement distinguishes recorded procedure, observable deviations, and
judgments that need semantic review. Only its own JSON/Markdown are written.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json

REPORT = Path('experiment/benchmarks/wildclaw_bench/reports/dci_memory')
RUNS = Path('experiment/benchmarks/wildclaw_bench/runs/dci_memory/formal')
PROTOCOL = Path('experiment/benchmarks/wildclaw_bench/variants/dci_memory/prepared/formal/protocol.json')
SEARCH_TOOLS = {'DCI_search_task', 'DCI_search_trajectory', 'DCI_search_memory'}
TOLERANCE_SECONDS = 1.0


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def iso_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def excerpt(text, limit=650):
    return text if len(text) <= limit else text[:limit] + ' […]'


def reason_evidence(event):
    """Keep the agent's actual short action decision; never infer reasoning."""
    fields = {key: event.get(key, '') for key in ('goal', 'blocker', 'assessment', 'next_action')}
    text = '\n'.join(f'{key}: {value}' for key, value in fields.items() if value)
    signals = []
    patterns = {
        'no_match': r'no (?:applicable|similar|relevant) (?:training )?task|'
                    r'未(?:发现|找到|检索到).{0,30}(?:相似|相关|语义相近)|'
                    r'(?:无|没有).{0,12}(?:相关命中|相似任务|服饰/穿搭任务)|'
                    r'训练任务库.{0,18}(?:未发现|无)',
        'reject': r'\breject\w*\b|拒绝|不适用|不相似|不具可迁移性|停止强行匹配',
        'adopt': r'\badopt\w*\b|采用|借鉴|沿用',
    }
    for name, pattern in patterns.items():
        if re.search(pattern, text, flags=re.I):
            signals.append(name)
    classification = ('no_match' if 'no_match' in signals else 'reject' if 'reject' in signals
                      else 'adopt_word_present' if 'adopt' in signals else 'uncertain')
    return {'event_id': event.get('event_id'), 'timestamp': event.get('timestamp'),
            'excerpt': excerpt(text, 1100), 'explicit_text_signals': signals,
            'classification': classification,
            'classification_scope': 'Literal language signals only; inspect excerpt to judge applicability/adoption.'}


def search_evidence(event, directory, root):
    saved = event.get('captured_output_path', '')
    path = Path(saved)
    text, output_hash, issue = '', None, None
    if path.is_absolute() and path.is_relative_to('/memory'):
        local = directory / 'memory_after_solver' / path.relative_to('/memory')
        try:
            local.resolve().relative_to((directory / 'memory_after_solver').resolve())
            if local.is_symlink():
                raise ValueError('symlink capture')
            data = local.read_bytes()
            output_hash, text = digest(data), data.decode('utf-8', errors='replace')
            if len(data) != event.get('captured_bytes'):
                issue = 'capture byte count differs from event'
        except (OSError, ValueError) as error:
            issue = f'{type(error).__name__}: {error}'
    else:
        issue = 'capture path is not task-local /memory'
    command = event.get('command', '')
    rc = event.get('returncode')
    # `bash -o pipefail` propagates rg's 1 for no matches. A zero-output
    # rg/head pipeline is compatible with this, never an automatic failure.
    simple_rg = bool(re.match(r'^\s*(?:rg|grep)\s', command)) and ';' not in command and '&&' not in command
    # Regex alternation can contain '|', so parse the head-pipeline shape
    # conservatively by inspecting only shell-style whitespace separators.
    shell_parts = re.split(r'\s+\|\s+', command)
    simple_rg = simple_rg and all(re.match(r'^\s*head(?:\s|$)', part) for part in shell_parts[1:])
    if issue:
        outcome = 'capture_evidence_issue'
    elif event.get('timed_out'):
        outcome = 'command_timeout'
    elif rc == 0:
        outcome = 'completed_with_output' if text else 'completed_empty_output'
    elif rc == 1 and not text and simple_rg:
        outcome = 'no_match_compatible_rc1'
    elif rc == 141 and text and len(shell_parts) > 1:
        outcome = 'output_with_possible_pipeline_sigpipe'
    elif re.search(r'command not found|No such file or directory|unrecognized flag|invalid option|regex parse error', text, re.I):
        outcome = 'recorded_command_error'
    else:
        outcome = 'nonzero_needs_review'
    return {'event_id': event.get('event_id'), 'timestamp': event.get('timestamp'),
            'tool': event.get('tool'), 'query': event.get('query'), 'command': command,
            'pre_query_assessment': event.get('assessment', ''),
            'selected_task_ids': event.get('selected_task_ids', []),
            'returncode': rc, 'timed_out': event.get('timed_out'),
            'captured_bytes': event.get('captured_bytes'), 'output_truncated': event.get('output_truncated'),
            'capture_limit_reached': event.get('capture_limit_reached'),
            'outcome': outcome, 'output_excerpt': excerpt(text), 'capture_sha256': output_hash,
            'evidence_issue': issue,
            'capture_path': str((directory / 'memory_after_solver' / path.relative_to('/memory')).relative_to(root))
                            if path.is_absolute() and path.is_relative_to('/memory') else None}


def session_observations(directory):
    paths = sorted((directory / 'codex-home/sessions').rglob('*.jsonl'))
    if len(paths) != 1:
        return {'issue': f'expected one raw session; found {len(paths)}'}
    times, native_times, outer_native_times = [], [], []
    model_events = []
    with paths[0].open() as handle:
        for line in handle:
            event = json.loads(line)
            t = iso_time(event['timestamp']) if event.get('timestamp') else None
            if t is not None:
                times.append(t)
            payload = event.get('payload', {})
            if (event.get('type') == 'event_msg' and payload.get('type') in ('item_started', 'item_completed')
                    and payload.get('item', {}).get('type') in ('CommandExecution', 'FileChange')
                    and isinstance(payload.get('started_at_ms'), (int, float))):
                native_times.append(payload['started_at_ms'] / 1000)
            if event.get('type') == 'turn_context':
                model_events.append((payload.get('model'), payload.get('effort', payload.get('reasoning_effort'))))
            if event.get('type') == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call'):
                rendered = json.dumps(payload)
                if any(name in rendered for name in ('tools.exec_command(', 'tools.apply_patch(', 'tools.write_stdin(')):
                    outer_native_times.append(t)
    if not times:
        return {'issue': 'raw session has no timestamps'}
    result = {'path': str(paths[0].relative_to(ROOT)), 'first_timestamp_unix': min(times),
              'last_timestamp_unix': max(times), 'raw_span_seconds': max(times) - min(times),
              'first_native_action_unix': min(native_times) if native_times else None,
              'native_timing_source': 'Internal CommandExecution/FileChange started_at_ms; outer exec may await DCI before a native command.',
              'outer_native_call_observed': bool(outer_native_times),
              'model_effort_pairs': [list(item) for item in sorted(set(model_events))]}
    return result


def budget_evidence(execution, directory, limit):
    raw = session_observations(directory)
    elapsed = execution.get('elapsed_seconds')
    valid_number = type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed > 0
    status = execution.get('status')
    within = valid_number and elapsed <= limit + TOLERANCE_SECONDS
    result = {'configured_budget_seconds': limit, 'recorded_elapsed_seconds': elapsed,
              'execution_status': status, 'timing_tolerance_seconds': TOLERANCE_SECONDS,
              'recorded_elapsed_within_budget_tolerance': within, 'raw_session': raw,
              'measurement': 'Monotonic CLI start to communicate completion/timeout; elapsed is recorded before freeze_agent. '
                             'Raw terminal events may include subsequent container-freeze termination overhead.'}
    resume_path = directory / 'resume.json'
    if resume_path.exists():
        resumed = read_json(resume_path)
        active = resumed['preinterrupt_observed_active_seconds'] + resumed['resumed_active_seconds']
        result.update(resumed=True, raw_span_includes_interruption=True,
                      preinterrupt_active_seconds=resumed['preinterrupt_observed_active_seconds'],
                      resumed_active_seconds=resumed['resumed_active_seconds'],
                      active_seconds=active, excluded_gap_seconds=resumed.get('excluded_interruption_gap_seconds'),
                      active_matches_recorded=valid_number and abs(active - elapsed) < .001,
                      measurement=resumed.get('timing_method'))
        result['assessment'] = ('recorded_within_budget' if within and result['active_matches_recorded']
                                else 'timing_needs_review')
    elif raw.get('issue'):
        result['assessment'] = 'timing_evidence_missing'
    elif status == 'timeout':
        result['raw_span_beyond_recorded_seconds'] = max(0, raw['raw_span_seconds'] - elapsed) if valid_number else None
        result['assessment'] = 'timeout_with_freeze_tail_observation' if within else 'recorded_elapsed_needs_review'
        result['raw_span_interpretation'] = 'Do not count raw session tail after timeout as renewed solving; elapsed precedes freeze_agent.'
    else:
        span_covered = valid_number and raw['raw_span_seconds'] <= elapsed + TOLERANCE_SECONDS
        result['recorded_elapsed_covers_raw_span'] = span_covered
        result['assessment'] = 'recorded_within_budget' if within and span_covered else 'timing_needs_review'
    return result


def final_decision_evidence(event, expected_agent_stage):
    if event is None:
        return {'present': False, 'status': 'missing_recorded_decision'}
    valid = (event.get('tool') == 'distill' and event.get('stage') == 'final'
             and event.get('agent_stage') == expected_agent_stage
             and event.get('decision') in ('write', 'no_update'))
    return {'present': True, 'valid_event': valid, 'event_id': event.get('event_id'),
            'timestamp': event.get('timestamp'), 'decision': event.get('decision'),
            'reason_excerpt': excerpt(event.get('reason', ''), 1000), 'entry_paths': event.get('entry_paths', [])}


def audit_task(root, scheduled, plan, structural):
    directory = root / RUNS / scheduled['run_id']
    row = {**scheduled, 'status': 'pending', 'workflow_status': 'pending'}
    result_path = directory / 'result.json'
    if not result_path.exists():
        state = read_json(directory / 'run.json') if (directory / 'run.json').exists() else {}
        row['observed_stage'] = state.get('status', 'not_started')
        return row
    data = result_path.read_bytes()
    record = json.loads(data)
    if record.get('status') != 'finished':
        row['observed_stage'] = record.get('status')
        return row
    row.update(status='completed', result_sha256=digest(data))
    score = record.get('evaluation', {}).get('overall_score')
    score_valid = (record.get('task_id') == scheduled['task_id'] and record.get('run_id') == scheduled['run_id']
                   and record.get('evaluation', {}).get('status') == 'graded'
                   and type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1)
    structural_hash = next((item.get('evidence', {}).get('result_sha256') for item in structural.get('checks', [])
                            if item.get('requirement') == 'finished_matching_scheduled_run'), None)
    current_structural = structural_hash == digest(data)
    row['data_validity'] = {'recorded_grade_valid': score_valid, 'score_unchanged': score,
                            'structural_audit_current': current_structural,
                            'structural_audit_status': structural.get('status') if current_structural else 'missing_or_stale',
                            'scope': 'Flow deviations do not invalidate or remove an existing benchmark score.'}
    raw_events = (directory / 'memory_after_solver/events.jsonl').read_bytes()
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    row['solver_events_sha256'] = digest(raw_events)
    searches = [search_evidence(event, directory, root) for event in events if event.get('tool') in SEARCH_TOOLS]
    row['search_events'] = searches
    row['tool_errors'] = [event for event in events if event.get('tool') == 'tool_error']
    lookup = {event['event_id']: event for event in searches}
    links, pending = [], []
    for event in events:
        if event.get('tool') in SEARCH_TOOLS:
            pending.append(event['event_id'])
        elif event.get('tool') == 'reason' and pending:
            links.append({'search_event_ids': pending, 'post_search_reason': reason_evidence(event),
                          'seconds_after_last_search_event': iso_time(event['timestamp']) -
                              iso_time(lookup[pending[-1]]['timestamp']),
                          'association': 'First later reason event; intermediate native actions are not a semantic link proof.'})
            pending = []
    if pending:
        links.append({'search_event_ids': pending, 'post_search_reason': None,
                      'association': 'No later reason event before solver ended.'})
    row['decision_links'] = links
    filtered = [event for event in events if event.get('tool') != 'tool_error']
    names = [event.get('tool') for event in filtered]
    task_indices = [i for i, name in enumerate(names) if name == 'DCI_search_task']
    first_search = filtered[task_indices[0]] if task_indices else None
    initial_ok = bool(names and names[0] == 'reason' and task_indices
                      and not any(name in ('DCI_search_trajectory', 'distill') for name in names[:task_indices[0]]))
    solver_budget = budget_evidence(record['execution'], directory, plan['timeout_seconds'])
    review_budget = budget_evidence(record['memory_review']['execution'], directory / 'review', plan['review_timeout_seconds'])
    first_native = solver_budget['raw_session'].get('first_native_action_unix')
    native_before = (first_native < iso_time(first_search['timestamp'])) if first_search and first_native else None
    row['initial_order'] = {'reason_then_task_search_recorded': initial_ok,
                            'task_search_event_id': first_search.get('event_id') if first_search else None,
                            'native_action_before_task_search': native_before,
                            'scope': 'Native setup versus substantive solving needs transcript inspection; true is a review flag, not an automatic violation.'}
    solver_finals = [event for event in events if event.get('tool') == 'distill' and event.get('stage') == 'final']
    row['solver_final'] = final_decision_evidence(solver_finals[-1] if solver_finals else None, 'solve')
    final_events = [json.loads(line) for line in (directory / 'memory/events.jsonl').read_text().splitlines() if line.strip()]
    decision = record['memory_review'].get('decision', {})
    matches = [event for event in final_events if event.get('event_id') == decision.get('event_id')]
    row['reviewer_final'] = final_decision_evidence(matches[0] if len(matches) == 1 else None, 'review')
    online_writes = [event for event in events if event.get('tool') == 'distill'
                     and event.get('stage') == 'online' and event.get('decision') == 'write']
    row['online_writes'] = [{'event_id': event['event_id'], 'timestamp': event['timestamp'],
                             'reason_excerpt': excerpt(event.get('reason', '')),
                             'later_memory_search_ids': [search['event_id'] for search in searches
                                 if search['tool'] == 'DCI_search_memory' and iso_time(search['timestamp']) > iso_time(event['timestamp'])]}
                            for event in online_writes]
    row['observed_domain_branch'] = ('task_search_then_trajectory_and_online_write' if online_writes and any(
        event['tool'] == 'DCI_search_trajectory' for event in searches) else
        'task_search_then_trajectory_without_online_write' if any(event['tool'] == 'DCI_search_trajectory' for event in searches)
        else 'task_search_without_trajectory_query')
    row['budget_evidence'] = {'solver': solver_budget, 'reviewer': review_budget,
                             'grader': {'recorded_elapsed_seconds': record['evaluation'].get('elapsed_seconds'),
                                        'configured_budget_seconds': plan['grading_timeout_seconds'],
                                        'scope': 'Grading is outside solver/reviewer budgets; actual grader model needs a separate gateway audit.'}}
    deviations, uncertain = [], []
    if not initial_ok:
        deviations.append('Missing recorded initial reason → task search sequence.')
    if not row['solver_final'].get('valid_event'):
        deviations.append('No valid solver-stage final distill decision was recorded.')
    if not row['reviewer_final'].get('valid_event'):
        deviations.append('No valid independent-reviewer final distill decision was recorded.')
    if native_before:
        uncertain.append('Native action preceded task search; inspect whether this was setup or substantive solving.')
    elif solver_budget['raw_session'].get('outer_native_call_observed') and first_native is None:
        uncertain.append('Native commands are present but no internal start timestamp was recorded; outer exec time cannot prove ordering.')
    for link in links:
        linked = [lookup[event_id] for event_id in link['search_event_ids']]
        reason = link['post_search_reason']
        if any(item['tool'] in ('DCI_search_task', 'DCI_search_trajectory') for item in linked):
            if reason is None:
                deviations.append('Search group has no subsequent reason event: ' + ', '.join(link['search_event_ids']))
            elif not reason['explicit_text_signals']:
                uncertain.append('Post-search reason needs applicability/adoption review: ' + reason['event_id'])
        # A failed attempt followed by a successful repair is preserved as an
        # attempt, not counted as an unrepaired workflow deviation.
        bad = [item for item in linked if item['outcome'] in ('command_timeout', 'recorded_command_error')]
        if bad:
            last_bad = max(linked.index(item) for item in bad)
            repaired = any(item['outcome'] in ('completed_with_output', 'completed_empty_output', 'no_match_compatible_rc1')
                           for item in linked[last_bad + 1:])
            if not repaired:
                uncertain.append('Search command error/timeout has no later successful domain query before next reason; native repair may exist: '
                                 + ', '.join(item['event_id'] for item in bad))
    for search in searches:
        if search['outcome'] in ('capture_evidence_issue', 'nonzero_needs_review', 'output_with_possible_pipeline_sigpipe'):
            uncertain.append('Search result needs inspection: ' + search['event_id'] + ' (' + search['outcome'] + ')')
    for stage, evidence in (('solver', solver_budget), ('reviewer', review_budget)):
        if evidence['assessment'] not in ('recorded_within_budget', 'timeout_with_freeze_tail_observation'):
            uncertain.append(stage + ' timing needs inspection: ' + evidence['assessment'])
    row['documented_deviations'] = deviations
    row['semantic_review_flags'] = uncertain
    row['workflow_status'] = ('documented_deviation' if deviations else 'needs_semantic_review' if uncertain else 'recorded_compliant')
    row['semantic_adoption_proven'] = False
    row['status_scope'] = 'recorded_compliant covers the listed observable procedure only; no claim of correct retrieval, adopted method, or memory benefit.'
    return row


def markdown(data):
    rows = data['tasks']
    text = ['# V5 工作流证据补充审计', '',
            f'已完成 {data["completed_tasks"]}/24；其余任务标记 pending。生成时间：{data["generated_at_utc"]}。', '',
            '本报告区分数据有效性、可观察的流程依从性与需要语义复核的判断。分数全部保留；不调用模型、不重跑、不改分。', '',
            '`recorded_compliant` 仅表示下列可观察流程已有记录。搜索结果是否真正适用、是否采用、是否产生收益，需要另行分析。', '',
            '| Run | Task | 数据审计 | 流程 | 查询次数 task / trajectory / memory | Solver final / reviewer final |',
            '| --- | --- | --- | --- | --- | --- |']
    for row in rows:
        if row['status'] != 'completed':
            state = 'evidence_error' if row.get('analysis_error') else 'pending'
            text.append(f'| {row["run_id"]} | {row["task_id"]} | — | {state} ({row.get("observed_stage", "")}) | — | — |')
            continue
        counts = Counter(event['tool'] for event in row.get('search_events', []))
        values = ' / '.join(str(counts[name]) for name in ('DCI_search_task', 'DCI_search_trajectory', 'DCI_search_memory'))
        finals = ' / '.join(row.get(name, {}).get('decision', 'missing') for name in ('solver_final', 'reviewer_final'))
        text.append(f'| {row["run_id"]} | {row["task_id"]} | {row.get("data_validity", {}).get("structural_audit_status", "unknown")} | {row["workflow_status"]} | {values} | {finals} |')
    text += ['', '## 判定边界', '',
             '- rc=1 且无输出的 rg/grep 查询保留为“与无匹配一致”，不会直接算命令失败。一般 shell 管道不能仅凭返回码判断语义。',
             '- 搜索参数中的 assessment 是查询前的意图；检索后决定另关联后续 reason 事件并保留原文。文字匹配仅标提示线索，不是语义裁判。',
             '- 在线写入后再次查询只证明调用顺序，不自动证明读取 memory 正文或采用经验。',
             '- budget 使用已记录单调时钟 elapsed，容差 1 秒用于调度／浮点差异，不增加运行预算。常规结束对比 raw session 跨度；timeout 的尾部冻结事件单独观察。恢复 reviewer 按两个活动区间计算，排除中断空档。',
             '- 结构审计是现有 final_audit.json 的当前 result 哈希对应状态。此补充报告不替代该审计，也不把流程偏差等同坏分或作废成绩。', '']
    for row in rows:
        if row['status'] != 'completed' and not row.get('analysis_error'):
            continue
        text += [f'## {row["run_id"]} · {row["task_id"]}', '',
                 f'分数 {row.get("data_validity", {}).get("score_unchanged", "—")}；流程状态 `{row["workflow_status"]}`。', '']
        if row.get('analysis_error'):
            text += [str(row['analysis_error']), '']
            continue
        text += [f'记录的 domain 分支：`{row["observed_domain_branch"]}`。', '']
        for event in row['search_events']:
            text += [f'- `{event["tool"]}` · `{event["event_id"]}`：rc={event["returncode"]}，{event["captured_bytes"]} bytes，`{event["outcome"]}`；query：{event.get("query", "")}',
                     f'  命令：`{event["command"].replace("`", "ˋ")}`']
            if event['selected_task_ids']:
                text += ['  选中的 training tasks：' + ', '.join('`' + task + '`' for task in event['selected_task_ids'])]
        for link in row['decision_links']:
            reason = link['post_search_reason']
            text += ['', '关联查询：' + ', '.join('`' + event_id + '`' for event_id in link['search_event_ids'])]
            if reason:
                text += [f'后续 reason `{reason["event_id"]}`；明确文字线索：`{reason["classification"]}`。', '',
                         '> ' + reason['excerpt'].replace('\n', '\n> ')]
                text += ['', f'该 reason 记录于本组末次查询事件之后 {link["seconds_after_last_search_event"]:.1f} 秒；先后关系不等于立即作出采用决定。']
            else:
                text += ['未记录后续 reason。']
        text += ['', f'实际 stage=online 写入决定：{len(row["online_writes"])} 次。']
        for write in row['online_writes']:
            text += [f'- `{write["event_id"]}`：{write["reason_excerpt"]}',
                     '  后续 memory 查询事件：' + (', '.join('`' + item + '`' for item in write['later_memory_search_ids']) or '无') + '；只证明调用顺序。']
        text += ['', '最终更新决定：', '']
        for name in ('solver_final', 'reviewer_final'):
            final = row[name]
            text += [f'- {name}: `{final.get("decision", "missing")}`。{final.get("reason_excerpt", "")}']
        text += ['', '时间证据：', '', '| 阶段 | 预算 s | elapsed s | raw span s | 判定 |', '| --- | --- | --- | --- | --- |']
        for name in ('solver', 'reviewer'):
            budget = row['budget_evidence'][name]
            raw_span = budget['raw_session'].get('raw_span_seconds')
            text += [f'| {name} | {budget["configured_budget_seconds"]} | {budget["recorded_elapsed_seconds"]:.3f} | {raw_span:.3f}'
                     f'{"（含中断空档，不作预算比较）" if budget.get("resumed") else ""} | {budget["assessment"]} |'] if raw_span is not None else [f'| {name} | {budget["configured_budget_seconds"]} | {budget["recorded_elapsed_seconds"]} | 缺失 | {budget["assessment"]} |']
        for item in row['documented_deviations']:
            text += ['', '流程偏差：' + item]
        for item in row['semantic_review_flags']:
            text += ['', '待复核：' + item]
        if row.get('source_review'):
            text += ['', '原始轨迹复核（来源哈希已核对）：']
            text += ['- ' + item for item in row['source_review'].get('observations', [])]
        text += ['', f'来源：`{RUNS / row["run_id"] / "result.json"}`，SHA256 `{row["result_sha256"]}`。', '']
    text += ['## 来源与重建', '', f'- 协议：`{PROTOCOL}`，SHA256 `{data["protocol_sha256"]}`。',
             f'- 结构审计：`{REPORT / "final_audit.json"}`，SHA256 `{data["structural_audit_sha256"]}`。',
             '- 各题结果、memory_after_solver/events.jsonl、保存的查询输出、原始 Codex sessions、恢复计时元数据。',
             '- 逐条轨迹复核：`workflow_source_reviews.json`（见 [数据与恢复](../../../DATA.md)）。仅在所列全部来源哈希仍一致时采用复核结论。',
             '- `python3 -B experiment/benchmarks/wildclaw_bench/reports/dci_memory/workflow_audit.py`；只输出同目录 workflow_audit.json 和 workflow_audit.md。', '']
    return '\n'.join(text)


def build(root=ROOT):
    protocol_bytes = (root / PROTOCOL).read_bytes()
    plan = json.loads(protocol_bytes)
    audit_bytes = (root / REPORT / 'final_audit.json').read_bytes()
    structural = json.loads(audit_bytes)
    structural_rows = {row['run_id']: row for row in structural['tasks']}
    review_path = root / REPORT / 'workflow_source_reviews.json'
    source_reviews = read_json(review_path).get('reviews', {}) if review_path.exists() else {}
    rows = []
    for scheduled in plan['tasks']:
        try:
            row = audit_task(root, scheduled, plan, structural_rows.get(scheduled['run_id'], {}))
            source_review = source_reviews.get(scheduled['run_id'])
            if row['status'] == 'completed' and source_review:
                sources = source_review.get('sources', {})
                row['workflow_status_before_source_review'] = row['workflow_status']
                if sources and all((root / path).is_file() and digest((root / path).read_bytes()) == expected
                                   for path, expected in sources.items()):
                    row['source_review'] = source_review
                    resolved = source_review.get('resolved_semantic_flags', [])
                    row['semantic_review_flags'] = [flag for flag in row['semantic_review_flags'] if flag not in resolved]
                    row['documented_deviations'].extend(source_review.get('documented_deviations', []))
                else:
                    row['semantic_review_flags'].append('Source-review hashes are missing or stale; review conclusions were not applied.')
                row['workflow_status'] = ('documented_deviation' if row['documented_deviations'] else
                                          'needs_semantic_review' if row['semantic_review_flags'] else 'recorded_compliant')
        except (OSError, ValueError, KeyError, TypeError) as error:
            row = {**scheduled, 'status': 'evidence_error',
                   'workflow_status': 'needs_semantic_review', 'analysis_error': {'type': type(error).__name__, 'message': str(error)}}
        rows.append(row)
    completed = sum(row['status'] == 'completed' for row in rows)
    result = {'schema_version': 1, 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'partial' if completed < 24 else 'all_records_inspected',
              'completed_tasks': completed, 'scheduled_tasks': 24,
              'workflow_status_counts': dict(Counter(row['workflow_status'] for row in rows)),
              'protocol_sha256': digest(protocol_bytes), 'structural_audit_sha256': digest(audit_bytes),
              'source_reviews_sha256': digest(review_path.read_bytes()) if review_path.exists() else None,
              'audit_script_sha256': digest(Path(__file__).read_bytes()),
              'scope': 'All scheduled tasks; observational coverage only. Does not establish semantic adoption or causal effects. Scores are never filtered by compliance.',
              'tasks': rows}
    output = root / REPORT
    atomic_json(output / 'workflow_audit.json', result)
    (output / 'workflow_audit.md').write_text(markdown(result))
    return result


if __name__ == '__main__':
    value = build()
    print(json.dumps({key: value[key] for key in ('status', 'completed_tasks', 'scheduled_tasks', 'workflow_status_counts')}))
