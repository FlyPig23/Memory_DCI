"""V7 runtime integration: only the corpus description changes; V5/V6 paths stay identical."""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import harbor_runtime, protocol, run, serve_memory

TASK = 'heldout-task'


def same_task_pool(root: Path, *, trajectories=2, task=TASK):
    (root / 'tasks').mkdir(parents=True)
    (root / 'tasks' / f'{task}.md').write_text(f'# {task}\n\nInstruction text.\n')
    (root / 'trajectories' / task).mkdir(parents=True)
    for n in range(1, trajectories + 1):
        path = root / 'trajectories' / task / 'model-a' / f'attempt-{n:03d}' / f'{task}_0.txt'
        path.parent.mkdir(parents=True)
        path.write_text(f'# Historical failed attempt at this task: {task}\nOfficial reward: 0; attempt ID: attempt-{n:03d}\n')
    (root / 'manifest.json').write_text(json.dumps({'kind': 'same_task_failed_official_trajectories', 'task_id': task,
                                                    'cross_task_memory': False, 'successful_trajectories_included': False}))
    return root


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.pool = same_task_pool(Path(self.temporary.name) / 'pool')

    def changed_lines(self, before, after):
        return [line for line in difflib.ndiff(before.splitlines(), after.splitlines()) if line[:2] in ('- ', '+ ')]

    def test_seeded_v7_rewrites_only_corpus_sentences_of_v6(self):
        seeded = {'entry_count': 3}
        v6 = harbor_runtime.solver_instructions(self.pool, '/app', initial_memory=seeded)
        v7 = harbor_runtime.solver_instructions_v7(self.pool, '/app', initial_memory=seeded)
        removed = [line[2:] for line in self.changed_lines(v6, v7) if line.startswith('- ')]
        allowed = ('V6 — seeded', '/pool/tasks:', '/pool/trajectories/<task_id>/<model_alias>/<trial_id>',
                   'original training trajectories', '/memory:', 'query over training-task', 'descriptions. Choose',
                   'similar training tasks', 'B. If no similar training task', 'relevant training trajectories again',
                   'The same initial failure-memory library')
        for line in removed:
            self.assertTrue(any(marker in line for marker in allowed), line)
        self.assertIn('V7 — seeded task-local evidence memory', v7)
        self.assertIn("2 original trajectories of officially graded FAILED attempts at THIS task", v7)
        self.assertIn("3 frozen failure-analysis entries written before testing by another model", v7)
        self.assertIn('successful attempts are held out and not included', v7)
        self.assertNotIn('training', v7.lower().replace('no training', ''))
        # Workflow and integrity wording stay verbatim.
        for sentence in ('A. First reason briefly about the task, then DCI_search_task before substantive',
                         'Never seek held-out historical trajectories,', 'F. Before finishing, check deliverables',
                         'A failed source run does not prove its root cause.'):
            self.assertIn(sentence, v6)
            self.assertIn(sentence, v7)

    def test_unseeded_v7_keeps_v5_memory_line(self):
        v5 = harbor_runtime.solver_instructions(self.pool, '/app')
        v7 = harbor_runtime.solver_instructions_v7(self.pool, '/app')
        self.assertIn('V7 — task-local evidence memory', v7)
        self.assertIn('/memory: initially empty experience memory belonging ONLY to this test task.', v7)
        self.assertNotIn('The same initial failure-memory library', v7)
        self.assertEqual(len(v5.splitlines()), len(v7.splitlines()))

    def test_empty_pool_counts_zero_attempts(self):
        empty = same_task_pool(Path(self.temporary.name) / 'empty', trajectories=0)
        self.assertIn('0 original trajectories of officially graded FAILED attempts',
                      harbor_runtime.solver_instructions_v7(empty, '/app'))

    def test_review_prompt_describes_same_task_pool(self):
        text = harbor_runtime.review_instructions_v7()
        self.assertIn("this task's own officially failed attempts", text)
        self.assertNotIn('training-task/trajectory pool', text)
        self.assertIn('You MUST call the distill tool with stage=final', text)

    def test_mcp_config_adds_scope_only_for_v7(self):
        default = harbor_runtime.make_config(TASK, '/app', 'solve')
        self.assertNotIn('--pool-scope', default)
        self.assertIn('same_task', harbor_runtime.make_config(TASK, '/app', 'solve', pool_scope='same_task'))


class ToolServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool = same_task_pool(self.root / 'pool')

    def library(self):
        return serve_memory.load_tools(harbor_runtime.SHARED / 'tools.py')

    def test_same_task_store_accepts_own_task_and_rewords_descriptors(self):
        store, descriptors = serve_memory.make_store(self.library(), self.pool, self.root / 'memory', TASK, 'solve',
                                                     str(self.root), pool_scope='same_task')
        self.assertEqual(store.build_task_ids, {TASK})
        text = json.dumps(descriptors)
        self.assertNotIn('BUILD tasks', text)
        self.assertIn('officially failed attempt', text)
        result = store.call('DCI_search_trajectory', {'query': 'q', 'command': 'ls', 'selected_task_ids': [TASK]})
        self.assertFalse(result['isError'], result)

    def test_build_scope_still_refuses_same_task_pool(self):
        with self.assertRaisesRegex(ValueError, 'must not belong to build pool'):
            serve_memory.make_store(self.library(), self.pool, self.root / 'memory', TASK, 'solve', str(self.root))

    def test_same_task_store_refuses_other_tasks(self):
        (self.pool / 'trajectories' / 'other-task').mkdir()
        with self.assertRaisesRegex(ValueError, 'exactly the current task'):
            serve_memory.make_store(self.library(), self.pool, self.root / 'memory', TASK, 'solve', str(self.root),
                                    pool_scope='same_task')

    def test_default_descriptors_unchanged(self):
        library = self.library()
        original = copy.deepcopy(library.TOOLS)
        other = self.root / 'build-pool'
        (other / 'tasks').mkdir(parents=True)
        (other / 'trajectories' / 'train-task').mkdir(parents=True)
        _, descriptors = serve_memory.make_store(library, other, self.root / 'memory2', TASK, 'solve', str(self.root))
        for before, after in zip(original, descriptors):
            self.assertEqual(before['description'], after['description'])


class PlanTests(unittest.TestCase):
    def test_v7_requires_corpus_and_others_refuse_it(self):
        with self.assertRaisesRegex(ValueError, 'V7 requires --task-corpus'):
            protocol.build_plan('v7', 'v7-plan-check')
        for arm in ('baseline', 'v5'):
            with self.assertRaisesRegex(ValueError, 'do not accept one'):
                protocol.build_plan(arm, 'v7-plan-check', task_corpus='prepared/v7_corpus/x')

    def test_v7_sources_extend_default_sources_only_for_v7(self):
        default = protocol.sources()
        self.assertEqual(protocol.sources(arm='v6'), default)
        self.assertFalse(any('v7_' in name for name in default))
        if all((protocol.BASE / 'scripts' / name).is_file() for name in protocol.V7_SOURCE_FILES):
            self.assertEqual(set(protocol.sources(arm='v7')) - set(default),
                             {f'experiment/benchmarks/terminal_bench_2_1/scripts/{n}' for n in protocol.V7_SOURCE_FILES})

    def test_seed_for_selects_per_task_seed_only_for_v7(self):
        task = {'task_id': TASK, 'memory_seed': {'manifest_sha256': 'x'}}
        self.assertEqual(run.seed_for({'arm': 'v7'}, task), {'manifest_sha256': 'x'})
        self.assertIsNone(run.seed_for({'arm': 'v5'}, task))
        self.assertEqual(run.seed_for({'arm': 'v6', 'memory_seed': {'manifest_sha256': 'y'}}, task), {'manifest_sha256': 'y'})


class V7CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'trial'
        (self.directory / 'agent').mkdir(parents=True)
        body_hash = hashlib.sha256(b'# Lesson\n').hexdigest()
        self.seed = {'entry_count': 2, 'manifest_sha256': 'a' * 64,
                     'files_sha256': {f'entries/{TASK}-0-overview.md': body_hash,
                                      f'entries/{TASK}-1-lesson.md': body_hash, 'manifest.json': 'f' * 64}}
        self.pool_files = {f'trajectories/{TASK}/model-a/attempt-001/{TASK}_0.txt': 'b' * 64}
        self.task = {'task_id': TASK, 'official_agent_timeout_seconds': 900, 'official_verifier_timeout_seconds': 900,
                     'pool': {'path': 'prepared/v7_corpus/b/pools/' + TASK, 'manifest_sha256': 'c' * 64,
                              'files_sha256': self.pool_files, 'trajectory_count': 1},
                     'memory_seed': self.seed}
        self.settings = {'model': 'gpt-5.6-sol', 'reasoning_effort': 'medium', 'verifier_policy': 'unlimited'}
        self.result = {'task_id': TASK, 'harbor': {'verifier_result': {'rewards': {'reward': 1.0}}},
                       'freeze': {'taken_before_hidden_grading': True, 'solver_stopped': True, 'snapshot_valid': True},
                       'review': {'valid': True, 'received_hidden_grade': False, 'evidence_unchanged': True,
                                  'memory_audit': {'final_decision': {'agent_stage': 'review', 'task_id': TASK,
                                                                     'decision': 'no_update'}}}}
        self.initial = {'valid': True, 'entry_count': 2, 'inherited_entries': 2, 'event_count': 0,
                        'scope': 'single_test_task', 'task_id': TASK, 'seed_manifest_sha256': self.seed['manifest_sha256'],
                        'files_sha256': {k: v for k, v in self.seed['files_sha256'].items() if k.startswith('entries/')}}
        self.contract = {'condition': 'v7', 'pool_scope': 'same_task', 'task_id': TASK,
                         'model': 'gpt-5.6-sol', 'reasoning_effort': 'medium', 'solver_timeout_seconds': 900,
                         'official_verifier_timeout_seconds': 900, 'verifier_timeout_seconds': None,
                         'verifier_timeout_policy': 'unlimited', 'cross_task_memory': False, 'initial_skills': 0,
                         'seed_manifest_sha256': self.seed['manifest_sha256'], 'initial_memory_entries': 2,
                         'pool_manifest_sha256': 'c' * 64, 'pool_trajectory_count': 1,
                         'pool_files_sha256_digest': run.pool_digest(self.pool_files)}
        self.model = {'model_effort_verified': True, 'models': ['gpt-5.6-sol'], 'efforts': ['medium']}
        self.health = {'blocked': False, 'phases': {'agent': {'actual_inference': True, 'blocked': False},
                                                   'review': {'actual_inference': True, 'blocked': False}}}
        patcher = patch.object(run.health_guard, 'inspect_trial', side_effect=lambda _: copy.deepcopy(self.health))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write('agent/usage.json', {'input_tokens': 1, 'output_tokens': 1})

    def write(self, name, content):
        (self.directory / name).write_text(json.dumps(content))

    def summarize(self, seed='task'):
        for name, value in (('v7_result.json', self.result), ('memory_initial.json', self.initial),
                            ('runtime_contract.json', self.contract), ('agent/model_audit.json', self.model)):
            self.write(name, value)
        return run.summary(self.directory, 'v7', task=self.task, settings=self.settings,
                           memory_seed=self.seed if seed == 'task' else seed)

    def test_seeded_trial_is_valid_with_deviation_list(self):
        row = self.summarize()
        self.assertTrue(row['valid'], row)
        self.assertEqual(row['workflow_deviations'], [])
        self.assertEqual(row['reward'], 1.0)

    def test_swapped_seed_is_refused(self):
        self.initial['seed_manifest_sha256'] = 'z' * 64
        self.assertIn('task_seed_mismatch', self.summarize()['reasons'])

    def test_unseeded_trial_requires_empty_initial_memory(self):
        self.task['memory_seed'] = None
        self.initial.update(entry_count=0, inherited_entries=0, seed_manifest_sha256=None, files_sha256={})
        self.contract.update(seed_manifest_sha256=None, initial_memory_entries=0)
        row = self.summarize(seed=None)
        self.assertTrue(row['valid'], row)
        self.initial['seed_manifest_sha256'] = 'a' * 64
        self.assertIn('task_seed_mismatch', self.summarize(seed=None)['reasons'])

    def test_pool_contract_mismatch_is_refused(self):
        self.contract['pool_files_sha256_digest'] = 'y' * 64
        self.assertIn('task_corpus_contract_mismatch', self.summarize()['reasons'])
        self.contract['pool_files_sha256_digest'] = run.pool_digest(self.pool_files)
        self.contract['condition'] = 'v6'
        self.assertIn('task_corpus_contract_mismatch', self.summarize()['reasons'])

    def test_review_failure_is_deviation_not_invalidity(self):
        self.result['review'] = {'valid': False, 'received_hidden_grade': False, 'evidence_unchanged': True}
        row = self.summarize()
        self.assertTrue(row['valid'], row)
        self.assertEqual(row['workflow_deviations'], ['post_submission_review_incomplete'])


class V6RegressionTests(unittest.TestCase):
    """The completed V6 run must keep recomputing exactly as recorded (skipped where runs/ is absent)."""

    def test_completed_v6_run_status_unchanged(self):
        name = 'v6_seeded_sol_medium_20261005'
        if not (protocol.BASE / 'runs' / name / 'protocol.json').is_file():
            self.skipTest('local V6 run not present')
        status = run.status('v6', name)
        self.assertEqual((status['status'], status['completed'], status['total']), ('completed', 36, 36))
        self.assertTrue(all(row['valid'] and row['status'] == 'completed' for row in status['trials']))


if __name__ == '__main__':
    unittest.main()
