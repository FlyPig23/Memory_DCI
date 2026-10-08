"""Protocol, resume and policy regression checks with synthetic data only."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from experiment.benchmarks.terminal_bench_2_1.scripts import protocol, run


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class PlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / 'repo/experiment/benchmarks/terminal_bench_2_1'
        self.base.mkdir(parents=True)
        write(self.base / 'config.json', protocol.load(protocol.BASE / 'config.json'))
        self.test_ids = [f'test-{i:02d}' for i in reversed(range(36))]
        self.training = [f'train-{i:02d}' for i in range(53)]
        rows, files = [], []
        for task_id in self.test_ids:
            directory = self.base / 'prepared/tasks' / task_id
            directory.mkdir(parents=True)
            (directory / 'instruction.md').write_text('The official task instruction')
            (directory / 'task.toml').write_text('[agent]\ntimeout_sec=123\n[verifier]\ntimeout_sec=456\n[environment]\ngpus=0\n')
            rows.append({'task_id': task_id, 'task_version_id': task_id + '-v1',
                         'source_path': 'prepared/tasks/' + task_id,
                         'instruction_sha256': protocol.sha(directory / 'instruction.md'),
                         'task_config_sha256': protocol.sha(directory / 'task.toml')})
            files.append({'task_version_id': task_id + '-v1', 'files': [
                {'path': name, 'sha256': digest} for name, digest in protocol.inventory(directory).items()]})
        self.split = {'tasks': rows, 'test_task_ids': self.test_ids, 'training_task_ids': self.training}
        write(self.base / 'manifests/split.json', self.split)
        write(self.base / 'manifests/task_files.json', files)
        pin = patch.object(protocol, 'PINNED_SPLIT_SHA256', protocol.sha(self.base / 'manifests/split.json'))
        pin.start(); self.addCleanup(pin.stop)
        source = patch.object(protocol, 'sources', return_value={'source.py': 'synthetic-sha'})
        source.start(); self.addCleanup(source.stop)

    def plan(self, arm='baseline', **kwargs):
        return protocol.build_plan(arm, 'unit-test', base=self.base, **kwargs)

    def make_pool(self):
        pool = self.base / 'prepared/pool'
        for task_id in self.training:
            (pool / 'trajectories' / task_id).mkdir(parents=True)
            path = pool / 'tasks' / (task_id + '.md')
            path.parent.mkdir(exist_ok=True); path.write_text('training card')
        write(pool / 'manifest.json', {'training_task_ids': self.training, 'cross_task_memory': False})
        self.pool_inventory()
        return pool

    def pool_inventory(self):
        write(self.base / 'manifests/pool.json', {'split_sha256': protocol.sha(self.base / 'manifests/split.json'),
                                                'files_sha256': protocol.inventory(self.base / 'prepared/pool')})

    def test_baseline_no_pool_and_exact_order_and_limits_with_model_override(self):
        plan = self.plan(model='another-supported-model', effort='high', verifier_policy='unlimited')
        self.assertEqual([t['task_id'] for t in plan['tasks']], self.test_ids)
        self.assertTrue(plan['tasks'][0]['run_id'].startswith('tb21-'))
        self.assertTrue(plan['tasks'][0]['run_id'].endswith('-01-test-35'))
        self.assertTrue(all(t['official_agent_timeout_seconds'] == 123 and t['official_verifier_timeout_seconds'] == 456 for t in plan['tasks']))
        self.assertEqual(plan['settings']['model'], 'another-supported-model')
        self.assertEqual(plan['settings']['reasoning_effort'], 'high')
        self.assertEqual(plan['settings']['verifier_policy'], 'unlimited')
        self.assertFalse((self.base / 'runs').exists())

    def test_distinct_runs_have_distinct_docker_trial_names(self):
        first = self.plan()
        other = protocol.build_plan('baseline', 'another-run', base=self.base)
        self.assertFalse({t['run_id'] for t in first['tasks']} & {t['run_id'] for t in other['tasks']})
        self.assertEqual([t['task_id'] for t in first['tasks']], [t['task_id'] for t in other['tasks']])

    def test_changed_official_task_or_added_file_rejected(self):
        directory = self.base / 'prepared/tasks' / self.test_ids[0]
        (directory / 'tests').mkdir()
        (directory / 'tests/extra.py').write_text('unofficial test')
        with self.assertRaisesRegex(ValueError, 'package changed'):
            self.plan()
        (directory / 'tests/extra.py').unlink()
        (directory / 'instruction.md').write_text('edited instruction')
        with self.assertRaisesRegex(ValueError, 'package changed'):
            self.plan()

    def test_split_reorder_rejected_even_with_same_ids(self):
        split = dict(self.split, test_task_ids=list(reversed(self.test_ids)))
        write(self.base / 'manifests/split.json', split)
        with self.assertRaisesRegex(ValueError, 'split changed'):
            self.plan()

    def test_v5_valid_pool_and_no_heldout_metadata(self):
        pool = self.make_pool()
        self.assertEqual(self.plan('v5')['review_timeout_seconds'], 180)
        metadata = protocol.load(pool / 'manifest.json')
        metadata['test_task_ids'] = self.test_ids
        write(pool / 'manifest.json', metadata)
        self.pool_inventory()
        with self.assertRaisesRegex(ValueError, 'isolation'):
            self.plan('v5')

    def test_v6_requires_seed_and_other_arms_reject_it(self):
        with self.assertRaisesRegex(ValueError, 'requires --memory-seed'):
            self.plan('v6')
        for arm in ('baseline', 'v5'):
            with self.assertRaisesRegex(ValueError, 'do not accept'):
                self.plan(arm, memory_seed='prepared/memory_seeds/test')

    def test_v6_seed_frozen_in_protocol_and_modified_source_rejected(self):
        from experiment.shared.memory.seed import create_seed
        pool = self.make_pool()
        relative = 'trajectories/train-00/model/trial/train-00_0.txt'
        source = pool / relative
        source.parent.mkdir(parents=True)
        source.write_text('Task: train-00\nScore: 0\nCommand failed: missing package\n')
        self.pool_inventory()
        entry = {'slug': 'check-required-package', 'title': 'Check required package',
                 'keywords': ['package'], 'applicability': 'A required package is missing.',
                 'procedure': ['Check package availability before calling it.'],
                 'pitfalls': ['Missing dependency prevented execution.'],
                 'verification': 'The source failed; proposed preflight is not tested here.',
                 'status': 'source_observed',
                 'sources': [{'path': '/pool/' + relative, 'line_start': 1, 'line_end': 3}],
                 'evidence_summary': 'Observed failure: missing package. Cause hypothesis: dependency unavailable. Repair unverified.'}
        path = self.base / 'prepared/memory_seeds/test'
        create_seed(path, [entry], pool=pool, training_task_ids=self.training,
                    provenance={'builder_model': 'gpt-5.6-luna', 'builder_reasoning_effort': 'xhigh',
                                'model_audit_verified': True, 'source_selection': 'failed_training_trajectories'})
        plan = self.plan('v6', memory_seed='prepared/memory_seeds/test', verifier_policy='unlimited')
        self.assertEqual(plan['memory_seed']['entry_count'], 1)
        self.assertEqual(plan['memory_seed']['manifest_sha256'], protocol.sha(path / 'manifest.json'))
        self.assertEqual(plan['review_timeout_seconds'], 180)
        self.assertEqual([t['task_id'] for t in plan['tasks']], self.test_ids)
        self.assertIn('entries/check-required-package.md', plan['memory_seed']['files_sha256'])
        source.write_text('changed source')
        self.pool_inventory()
        with self.assertRaises(ValueError):
            self.plan('v6', memory_seed='prepared/memory_seeds/test')

    def test_v5_rejects_pool_mutation_or_training_leak(self):
        pool = self.make_pool()
        (pool / 'tasks' / (self.test_ids[0] + '.md')).write_text('leaked heldout task')
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            self.plan('v5')
        self.pool_inventory()
        with self.assertRaisesRegex(ValueError, 'exactly the frozen training'):
            self.plan('v5')

    def test_paths_and_data_symlinks_cannot_escape(self):
        for name in ('../escape', '/absolute', '..', 'a/b', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                protocol.run_directory(self.base, name)
        target = self.base / 'prepared/tasks' / self.test_ids[0] / 'link'
        target.symlink_to('/tmp')
        with self.assertRaisesRegex(ValueError, 'Symbolic links'):
            self.plan()
        (self.base / 'runs').symlink_to(self.base / 'prepared')
        with self.assertRaises(ValueError):
            protocol.run_directory(self.base, 'safe')


class ResumeTests(unittest.TestCase):
    def test_partial_attempt_is_never_automatically_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'incomplete trial'):
                run.saved_completion(Path(temporary), 'baseline')

    def test_changed_result_or_audit_blocks_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write(directory / 'completion.json', {'valid': True, 'reward': 1, 'result_sha256': 'first'})
            with patch.object(run, 'summary', return_value={'valid': True, 'reward': 0, 'result_sha256': 'changed'}):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    run.saved_completion(directory, 'baseline')

    def test_status_cannot_report_completed_after_audit_change_or_missing_marker(self):
        for marker_present in (False, True):
            with self.subTest(marker_present=marker_present), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                directory = base / 'runs/synthetic'
                trial = directory / 'trials/task'
                trial.mkdir(parents=True)
                write(directory / 'protocol.json', {'arm': 'baseline', 'settings': {}, 'tasks': [{'run_id': 'task'}]})
                write(directory / 'state.json', {'status': 'completed'})
                if marker_present:
                    write(trial / 'completion.json', {'valid': True, 'reward': 1})
                with patch.object(run, 'summary', return_value={'valid': True, 'reward': 0}):
                    observed = run.status('baseline', 'synthetic', base=base)
                self.assertEqual(observed['completed'], 0)
                self.assertEqual(observed['status'], 'needs_review')

    def test_status_distinguishes_live_incomplete_trial_from_stale_running_state(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            directory = base / 'runs/synthetic'
            (directory / 'trials/task').mkdir(parents=True)
            write(directory / 'protocol.json', {'arm': 'baseline', 'settings': {},
                                                'tasks': [{'run_id': 'task', 'task_id': 'task'}]})
            write(directory / 'state.json', {'status': 'running', 'active_task': 'task'})
            with (directory / 'controller.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed = run.status('baseline', 'synthetic', base=base)
                self.assertEqual(observed['status'], 'running')
                self.assertTrue(observed['controller_active'])
                self.assertEqual(observed['completed'], 0)
            observed = run.status('baseline', 'synthetic', base=base)
            self.assertEqual(observed['status'], 'needs_review')
            self.assertFalse(observed['controller_active'])

    def test_image_resolution_uses_saved_digest_after_tag_moves(self):
        saved = {'image_id': 'sha256:original', 'repo_digest': 'python@sha256:original'}
        metadata = json.dumps([{'Id': 'sha256:original', 'RepoDigests': ['python@sha256:original']}])
        from experiment.benchmarks.terminal_bench_2_1.scripts import harbor_runtime
        command = AsyncMock(side_effect=[(1, '', ''), (0, '', ''), (0, metadata, '')])
        with patch.object(harbor_runtime, 'command', command):
            result = asyncio.run(run.resolve_image('python:moved', saved))
        self.assertEqual(result, saved)
        self.assertEqual(command.await_args_list[1].args[0], ['docker', 'pull', 'python@sha256:original'])


class RuntimePolicyTests(unittest.TestCase):
    def test_seed_description_preserves_v5_workflow(self):
        from experiment.benchmarks.terminal_bench_2_1.scripts import harbor_runtime
        with tempfile.TemporaryDirectory() as temporary:
            pool = Path(temporary)
            (pool / 'trajectories/train').mkdir(parents=True)
            original = harbor_runtime.solver_instructions(pool, '/app')
            seeded = harbor_runtime.solver_instructions(pool, '/app', initial_memory={'entry_count': 7})
            self.assertIn('initially empty', original)
            self.assertNotIn('initially empty', seeded)
            self.assertIn('7 frozen failure lessons', seeded)
            self.assertEqual(original.split('Required workflow:')[1].split('Initial working directory:')[0],
                             seeded.split('Required workflow:')[1].split('Initial working directory:')[0])

    def test_both_arms_apply_same_explicit_verifier_policy(self):
        from experiment.benchmarks.terminal_bench_2_1.scripts import baseline_runtime, harbor_runtime
        for arm in ('baseline', 'v5'):
            module = baseline_runtime if arm == 'baseline' else harbor_runtime
            for policy, effective in [('official', 456), ('unlimited', None)]:
                with self.subTest(arm=arm, policy=policy), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task, trial_dir, pool = root / 'task', root / 'trial', root / 'pool'
                    task.mkdir()
                    (task / 'task.toml').write_text('[agent]\ntimeout_sec=123\n[verifier]\ntimeout_sec=456\n[environment]\ngpus=0\n')
                    write(root / 'auth.json', {'synthetic': True})
                    write(pool / 'manifest.json', {'synthetic': True})
                    (pool / 'tasks').mkdir()
                    result = SimpleNamespace(model_dump=lambda **_: {'verifier_result': {'rewards': {'reward': 0}}})
                    trial = SimpleNamespace(_agent_timeout_sec=123, _verifier_timeout_sec=456,
                                            _agent_env_mounts=[], add_hook=lambda *_: None,
                                            run=AsyncMock(return_value=result))
                    with patch.object(module.Trial, 'create', AsyncMock(return_value=trial)), \
                         patch.object(module, 'support_mounts', return_value=[]), \
                         patch.object(module, 'clean_private_runtime', AsyncMock()), \
                         patch.object(module, 'command', AsyncMock(return_value=(0, 'sha256:synthetic', ''))):
                        kwargs = {'python_root': root, 'auth_file': root / 'auth.json', 'verifier_policy': policy}
                        if arm == 'baseline':
                            kwargs.update(environment_config={'extra_docker_compose': [str(module.BASE / 'prepared/transport/docker-compose-ca.yaml')]},
                                          cleanup_image='synthetic')
                            asyncio.run(module.run_trial(task, trial_dir, 'task', **kwargs))
                        else:
                            kwargs.update(review_image='synthetic')
                            asyncio.run(module.run_trial(task, trial_dir, pool, 'task', **kwargs))
                    self.assertEqual(trial._agent_timeout_sec, 123)
                    self.assertEqual(trial._verifier_timeout_sec, effective)
                    contract = protocol.load(trial_dir / 'runtime_contract.json')
                    self.assertEqual(contract['official_verifier_timeout_seconds'], 456)
                    self.assertEqual(contract['verifier_timeout_seconds'], effective)
                    self.assertEqual(contract['verifier_timeout_policy'], policy)

    def test_model_override_reaches_both_native_configs_without_extra_baseline_tools(self):
        from experiment.benchmarks.terminal_bench_2_1.scripts import baseline_runtime, harbor_runtime
        config = protocol.settings(model='supported-other', effort='high')
        try:
            run.configure_runtime(config)
            baseline = baseline_runtime.make_config()
            v5 = harbor_runtime.make_config('task', '/app', 'solve')
            self.assertIn('model = "supported-other"', baseline)
            self.assertIn('model_reasoning_effort = "high"', v5)
            self.assertNotIn('mcp_servers', baseline)
            self.assertIn('mcp_servers', v5)
        finally:
            run.configure_runtime(protocol.settings())


if __name__ == '__main__':
    unittest.main()
