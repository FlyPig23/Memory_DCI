"""Finalize all 24 V5 records and reports after the controller has exited.

No model calls, solver retries or regrading. Refuses partial data or a live
controller. Reconciles the progress snapshot with audited per-task records,
including the documented resumed-review token correction.
"""
import fcntl
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiment.src.experiment_protocol import atomic_json, sha256
from experiment.scripts.service_control import process_identity, identity_matches
from experiment.variants.dci_memory import runner, report
from experiment.reports.dci_memory.audit import audit
from experiment.reports.dci_memory.build_comparison import build
from experiment.reports.dci_memory.workflow_audit import build as build_workflow


def finalize():
    base = ROOT / runner.RUNS
    with (base / 'batch.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = base / 'batch.json'
        state = json.loads(path.read_text())
        controller = state.get('controller', {})
        if controller.get('pid') and identity_matches(controller, process_identity(controller['pid'])):
            raise ValueError('Wait for the live controller and its reporting phase to finish')
        # Rechecking saved evidence must not re-freeze current implementation sources.
        plan = json.loads((ROOT / runner.VARIANT / 'prepared/formal/protocol.json').read_text())
        if any(not (base / 'formal' / row['run_id'] / 'result.json').is_file() for row in plan['tasks']):
            raise ValueError('All 24 final records are required; missing tasks must continue through the batch')
        results = [runner.audit_result(row) for row in plan['tasks']]
        proof = audit()
        if not proof['complete'] or proof['valid_tasks'] != 24:
            raise ValueError('Full requirement audit is not complete; do not finalize')
        backup = base / 'controllers/batch_before_finalization.json'
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        changes = []
        previous = {row['run_id']: row for row in state.get('completed', [])}
        for row in results:
            old = previous.get(row['run_id'], {})
            delta = {key: {'before': old.get(key), 'after': value}
                     for key, value in row.items() if old.get(key) != value}
            if delta:
                changes.append({'run_id': row['run_id'], 'fields': delta})
        state.update(status='completed', active=None, completed=results, updated_at=time.time())
        state.pop('error', None)
        atomic_json(path, state)
        summary = report.report()
        proof = audit()
        workflow = build_workflow()
        comparison = build()
        if (summary['scored_runs'] != 24 or not proof['complete'] or not comparison['final_audited']
                or workflow['completed_tasks'] != 24):
            raise ValueError('Final report completeness check failed')
        receipt_path = ROOT / 'experiment/reports/dci_memory/finalization.json'
        previous_receipt = None
        reconciliation = changes
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            previous_backup = base / 'controllers/first_finalization.json'
            if not previous_backup.exists():
                previous_backup.write_bytes(receipt_path.read_bytes())
            previous_receipt = {'path': str(previous_backup.relative_to(ROOT)),
                                'sha256': sha256(previous_backup)}
            # A report-only refresh must retain the original token-accounting
            # reconciliation rather than replacing its evidence with [].
            reconciliation = previous.get('snapshot_reconciliation', []) + changes
        receipt = {
            'status': 'complete_audited', 'finished_at': time.time(),
            'formal_solver_runs': 24, 'new_model_calls': 0, 'new_grading_calls': 0,
            'script_sha256': sha256(Path(__file__)), 'snapshot_reconciliation': reconciliation,
            'first_finalization_receipt': previous_receipt,
            'workflow_observation_counts': workflow['workflow_status_counts'],
            'workflow_scope': 'All 24 records inspected; semantic-review flags and deviations are retained, not filtered from scores.',
            'batch_sha256': sha256(path),
            'report_hashes': {name: sha256(ROOT / 'experiment/reports/dci_memory' / name)
                              for name in ['summary.json', 'final_audit.json', 'comparison.json', 'comparison.md', 'comparison.csv',
                                           'workflow_audit.json', 'workflow_audit.md', 'workflow_source_reviews.json']},
        }
        atomic_json(receipt_path, receipt)
        print(json.dumps({'status': receipt['status'], 'scored_runs': summary['scored_runs'],
                          'mean_score': summary['mean_score'], 'snapshot_updates': len(changes)}))
        return receipt


if __name__ == '__main__':
    finalize()
