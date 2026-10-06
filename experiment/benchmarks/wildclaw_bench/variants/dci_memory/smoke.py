"""One synthetic V5 integration probe, never a held-out benchmark rollout."""
import json
from dataclasses import asdict
from pathlib import Path
import time

from experiment.shared.codex_backend import CodexBackend
from experiment.shared.task_runtime import Mount, TaskSpec
from experiment.benchmarks.wildclaw_bench.src.runtime_support import make_runtime_config, copy_runtime_auth
from experiment.benchmarks.wildclaw_bench.src.inference_access import inference_client
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256, workflow_config
from experiment.benchmarks.wildclaw_bench.scripts.evaluate_wildclaw import freeze_solver_evidence
from . import runner
from experiment.shared.memory.tools import initialize_memory, audit_memory
from .memory_runtime import MemoryTaskRuntime


def main():
    root = runner.ROOT
    directory = root / 'experiment/benchmarks/wildclaw_bench/runs/dci_memory/smoke' / ('integration-' + str(time.time_ns()))
    directory.mkdir(parents=True)
    inputs = directory / 'inputs'
    (inputs / 'exec').mkdir(parents=True)
    (inputs / 'exec/request.txt').write_text('Synthetic probe: create result.txt containing exactly READY.\n')
    task_file = directory / 'task.md'
    task_file.write_text('# Synthetic V5 integration probe\nNo benchmark question or gold materials.\n')
    old = workflow_config(root)
    plan = {**old, 'pool_path': str(runner.VARIANT / 'prepared/pool'), 'review_timeout_seconds': 180,
            'review_instructions': (root / 'experiment/shared/memory/review_prompt.txt').read_text()}
    task = TaskSpec('v5-integration-probe', 'synthetic',
        'This is a synthetic integration probe. Use reason, then DCI_search_task with a PDF-related query. '
        'Select one relevant build task and use DCI_search_trajectory to read a small real action/result window. '
        'Call distill(stage=online,decision=write) for one concise source_observed lesson supported by exact '
        'trajectory lines; then DCI_search_memory to read your entry. Finally write /tmp_workspace/result.txt '
        'containing exactly READY. Call distill(stage=final,decision=no_update) before finishing. '
        'Keep all work short; do not execute any task from the historical pool.', inputs, inputs, timeout_seconds=180)
    run_dir = directory / 'run'
    initialize_memory(run_dir / 'memory', task.task_id)
    initial = runner.file_hashes(run_dir / 'memory')
    mounts = (Mount(root / plan['pool_path'], '/pool'), Mount(run_dir / 'memory', '/memory', False),
              Mount(root / runner.VARIANT, '/opt/v5'), Mount(root / plan['rg_mount']['source'], '/usr/local/bin/rg'))
    runtime = None
    try:
        with inference_client(root, 'v5-smoke-' + directory.name[-12:]) as gateway:
            config = make_runtime_config(root, plan['solver_image_id'], support_mounts=mounts, warmup_override='')
            runtime = MemoryTaskRuntime(config, task, run_dir, 'v5-smoke-' + directory.name[-12:],
                pool_manifest_sha256=sha256(root / plan['pool_path'] / 'manifest.json'), memory_initial_sha256=runner.digest_json(initial))
            backend = CodexBackend(runner.settings(task.task_id, 'solve', plan))
            backend.prepare(runtime)
            copy_runtime_auth(runtime)
            backend.preflight(runtime)
            execution = backend.run(runtime, condition_instructions=(root / 'experiment/shared/memory/prompt.txt').read_text(), verify_login=False)
            runner.sync_auth(runtime, root)
            runner.audit_model(run_dir, plan)
            assert execution.status == 'completed', execution
            assert (runtime.workspace / 'result.txt').read_text().strip() == 'READY'
            online = audit_memory(run_dir / 'memory')
            assert online['valid'] and online['entry_count'] > 0, online
            assert all(online['event_counts'].get(tool, 0) > 0 for tool in plan.get('tools',
                ['reason', 'DCI_search_task', 'DCI_search_trajectory', 'DCI_search_memory', 'distill'])), online
            freeze_solver_evidence(runtime, transcript_path=execution.transcript_path,
                transcript_loader=root / 'experiment/benchmarks/wildclaw_bench/vendor/WildClawBench/src/utils/transcript_loader.py',
                task_source=task_file, source_sha256=sha256(task_file))
            review = runner.review_memory(root, run_dir, task, plan, gateway)
            assert review['decision']['agent_stage'] == 'review'
            assert (runtime.workspace / 'result.txt').read_text().strip() == 'READY'
            runtime.detach_resources_for_grading()
            record = {'status': 'passed', 'solver': {**asdict(execution), 'transcript_path': str(execution.transcript_path)},
                      'online_memory': online, 'review': review, 'real_test_tasks_run': 0}
            atomic_json(directory / 'validation.json', record)
            print(json.dumps({'status': 'passed', 'directory': str(directory.relative_to(root)),
                              'online_entries': online['entry_count'], 'final_decision': review['decision']['decision']}))
    finally:
        if runtime:
            runtime.cleanup()


if __name__ == '__main__':
    main()
