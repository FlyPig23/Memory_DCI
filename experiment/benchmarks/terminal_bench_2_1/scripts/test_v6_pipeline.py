"""No inference: construction failures cannot start a partial-seed experiment."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from experiment.benchmarks.terminal_bench_2_1.scripts import protocol, v6_pipeline


class PipelineTests(unittest.TestCase):
    def test_only_complete_nonempty_seed_starts_fixed_full_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            seed = base / 'prepared/seed'
            execute = AsyncMock()
            with patch.object(protocol, 'BASE', base), \
                 patch.object(v6_pipeline.builder, 'build', return_value={'status': 'frozen', 'entry_count': 7}) as build, \
                 patch.object(protocol, 'build_plan', return_value={'tasks': list(range(36))}) as plan, \
                 patch.object(v6_pipeline.run, 'execute', execute), \
                 patch.object(v6_pipeline.run, 'status', return_value={'status': 'completed', 'completed': 36, 'total': 36}):
                result = v6_pipeline.execute_pipeline(work_dir=base / 'build', seed_dir=seed,
                    run_name='v6-test', auth_file=base / 'unused-auth.json')
            self.assertEqual(result['phase'], 'completed')
            self.assertEqual(result['completed'], 36)
            plan.assert_called_once_with('v6', 'v6-test', model='gpt-5.6-sol', effort='medium',
                                         verifier_policy='unlimited', memory_seed='prepared/seed')
            execute.assert_awaited_once()
            self.assertFalse(build.call_args.kwargs['accept_code_update'])
            self.assertFalse(build.call_args.kwargs['accept_budget_update'])
            self.assertFalse(build.call_args.kwargs['retry_budget_exhausted'])
            self.assertIsNone(build.call_args.kwargs['timeout_seconds'])

    def test_cooperative_pause_never_launches_solver(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with patch.object(protocol, 'BASE', base), \
                 patch.object(v6_pipeline.builder, 'build', return_value={
                     'status': 'paused', 'entry_count': 8, 'complete_jobs': 2}) as build, \
                 patch.object(v6_pipeline.run, 'execute', AsyncMock()) as execute:
                result = v6_pipeline.execute_pipeline(work_dir=base / 'build', seed_dir=base / 'seed',
                    run_name='v6-test', auth_file=base / 'unused-auth.json',
                    accept_builder_code_update=True, accept_builder_budget_update=True,
                    retry_builder_budget_exhausted=True)
            self.assertEqual(result['phase'], 'paused')
            self.assertEqual(result['completed_builder_jobs'], 2)
            execute.assert_not_awaited()
            self.assertTrue(build.call_args.kwargs['accept_budget_update'])
            self.assertTrue(build.call_args.kwargs['retry_budget_exhausted'])
            self.assertEqual(protocol.load(base / 'build/pipeline_state.json')['phase'], 'paused')

    def test_partial_or_empty_seed_never_launches_solver(self):
        for status in ({'status': 'building', 'entry_count': 7}, {'status': 'frozen', 'entry_count': 0}):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                with patch.object(protocol, 'BASE', base), \
                     patch.object(v6_pipeline.builder, 'build', return_value=status), \
                     patch.object(v6_pipeline.run, 'execute', AsyncMock()) as execute:
                    with self.assertRaises(ValueError):
                        v6_pipeline.execute_pipeline(work_dir=base / 'build', seed_dir=base / 'seed',
                                                     run_name='v6-test', auth_file=base / 'unused-auth.json')
                execute.assert_not_awaited()
                state = protocol.load(base / 'build/pipeline_state.json')
                self.assertEqual(state['phase'], 'needs_review')
                self.assertEqual(state['failed_phase'], 'building_failure_memory')


if __name__ == '__main__':
    unittest.main()
