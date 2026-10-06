"""Regression coverage for retained workflows without discontinued code or data."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import protocol
from experiment.benchmarks.wildclaw_bench.variants.dci_memory import runner as memory
from experiment.benchmarks.wildclaw_bench.variants.dci_memory import test_pool as pool_fixture

write_json = pool_fixture.write_json
from experiment.benchmarks.wildclaw_bench.variants.vanilla_sol import runner as vanilla

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def clean_project(monkeypatch):
    fixture = pool_fixture.PoolTests()
    fixture.setUp()
    root = fixture.root
    try:
        for directory in ('experiment/shared', 'experiment/shared/memory', 'experiment/benchmarks/wildclaw_bench/src', 'experiment/benchmarks/wildclaw_bench/variants/vanilla_sol',
                          'experiment/benchmarks/wildclaw_bench/variants/dci_memory'):
            for source in (ROOT / directory).glob('*'):
                if source.suffix in ('.py', '.txt') and source.name != 'dci_tools.py':
                    destination = root / directory / source.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
        for name in ('run_wildclaw.py', 'evaluate_wildclaw.py', 'service_control.py',
                     'launch_vanilla_sol_batch.py'):
            destination = root / 'experiment/benchmarks/wildclaw_bench/scripts' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / 'experiment/benchmarks/wildclaw_bench/scripts' / name, destination)
        config = json.loads((ROOT / 'experiment/benchmarks/wildclaw_bench/manifests/workflow_config.json').read_text())
        for name in ('rg', 'codex', 'codex-code-mode-host'):
            destination = root / 'experiment/benchmarks/wildclaw_bench/runtime/bin' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'synthetic runtime binary\n')
        config['rg_mount']['sha256'] = hashlib.sha256(b'synthetic runtime binary\n').hexdigest()
        write_json(root / 'experiment/benchmarks/wildclaw_bench/manifests/workflow_config.json', config)
        write_json(root / 'experiment/benchmarks/wildclaw_bench/manifests/formal_protocol.json', {
            'model': 'gpt-6-astra', 'reasoning_effort': 'ultra', 'cli_version': '0.153.4',
            'image_id': config['solver_image_id'], 'grading_timeout_seconds': 1800,
            'conditions': {'A0': 'Frozen baseline instructions'},
            'schedule': [{'task_id': t, 'condition': arm, 'index': 2*i+j}
                         for i, t in enumerate(fixture.split['test_task_ids'])
                         for j, arm in enumerate(('A0', 'A3'))]})
        for name in ('warmup_dependencies.json', 'runtime_dependency_validation.json',
                     'runtime_skill_patches.json', 'dependency_resources.json'):
            write_json(root / 'experiment/benchmarks/wildclaw_bench/manifests' / name, {})
        for key, value in {
            'ROOT': root, 'VARIANT': root / 'experiment/benchmarks/wildclaw_bench/variants/vanilla_sol',
            'RUNS': root / 'experiment/benchmarks/wildclaw_bench/runs/vanilla_sol',
            'SCHEDULE': root / 'experiment/benchmarks/wildclaw_bench/manifests/vanilla_sol_schedule.json',
            'PROTOCOL': root / 'experiment/benchmarks/wildclaw_bench/variants/vanilla_sol/prepared/formal/protocol.json',
        }.items():
            monkeypatch.setattr(vanilla, key, value)
        yield root
    finally:
        fixture.tearDown()


def test_imports_do_not_load_removed_experiments():
    script = '''
import importlib.abc, sys
class Removed(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("experiment.benchmarks.wildclaw_bench.variants.dci_terminal", "experiment.benchmarks.wildclaw_bench.variants.dci_native",
            "experiment.benchmarks.wildclaw_bench.variants.dci_skills", "experiment.benchmarks.wildclaw_bench.reports.dci_native", "experiment.benchmarks.wildclaw_bench.src.dci_tools")):
            raise ImportError("Removed workflow imported: " + fullname)
sys.meta_path.insert(0, Removed())
from experiment.benchmarks.wildclaw_bench.variants.vanilla_sol import runner, batch
from experiment.benchmarks.wildclaw_bench.variants.dci_memory import runner, batch, report
from experiment.benchmarks.wildclaw_bench.scripts import run_wildclaw, launch_vanilla_sol_batch
'''
    subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT, check=True)


def test_all_three_prepare_without_old_variant_files(clean_project):
    root = clean_project
    first = protocol(root)
    assert first['model'] == 'gpt-6-astra'
    assert first['reasoning_effort'] == 'ultra'
    assert len(first['schedule']) == 24
    assert {r['condition'] for r in first['schedule']} == {'A0'}
    baseline = vanilla.prepare()
    vanilla.verify_plan(baseline)
    latest = memory.prepare(root)
    assert memory.prepare(root, write=False) == latest
    assert [r['task_id'] for r in baseline['tasks']] == [r['task_id'] for r in latest['tasks']]
    assert baseline['condition_instructions'] == 'Frozen baseline instructions'
    assert latest['rg_mount']['source'] == 'experiment/benchmarks/wildclaw_bench/runtime/bin/rg'
    assert latest['memory_policy']['cross_task_transfer'] is False
    assert not latest['historical_results']
    assert not (root / 'experiment/benchmarks/wildclaw_bench/variants/dci_skills_native').exists()


@pytest.mark.parametrize('variant', ('vanilla_sol', 'dci_memory'))
def test_prepare_never_reseals_historical_protocol(clean_project, variant):
    path = clean_project / f'experiment/benchmarks/wildclaw_bench/variants/{variant}/prepared/formal/protocol.json'
    original = b'{"schema_version": 1, "historical_evidence": true}\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    with pytest.raises(ValueError, match='Historical'):
        vanilla.prepare() if variant == 'vanilla_sol' else memory.prepare(clean_project)
    assert path.read_bytes() == original
    assert not (clean_project / 'experiment/benchmarks/wildclaw_bench/variants/dci_memory/prepared/pool').exists()


def test_ripgrep_tampering_blocks_execution(clean_project):
    plan = vanilla.prepare()
    (clean_project / plan['rg_mount']['source']).write_bytes(b'changed binary')
    with pytest.raises(ValueError, match='Frozen input changed'):
        vanilla.verify_plan(plan)


def test_shared_runtime_is_sealed_by_all_workflows(clean_project):
    root = clean_project
    first = protocol(root)
    baseline = vanilla.prepare()
    latest = memory.prepare(root)
    for name in ('codex_backend.py', 'task_runtime.py'):
        key = 'experiment/shared/' + name
        assert key in first['hashes']
        assert key in baseline['hashes']
        assert key in latest['source_hashes']
    (root / 'experiment/shared/codex_backend.py').write_text('# altered runtime\n')
    with pytest.raises(ValueError, match='Frozen input changed'):
        vanilla.verify_plan(baseline)
    with pytest.raises(ValueError):
        memory.prepare(root, write=False)
