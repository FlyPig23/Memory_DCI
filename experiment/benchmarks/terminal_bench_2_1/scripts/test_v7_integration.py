"""Writer side (runtime) and plan side (protocol) agree on the real V7 corpus, up to the first Docker call.

Skipped until the frozen corpus and its exported seeds exist locally. Reads the corpus only; trials go to a
temporary directory and stop before any container starts.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import harbor_runtime, protocol, run, runtime

CORPUS = 'prepared/v7_corpus/final_20261007'
SNAPSHOT = protocol.BASE / 'runs/source_snapshots/v6_before_v7_20261007'


class StopBeforeDocker(Exception):
    pass


async def no_docker(*args, **kwargs):
    raise StopBeforeDocker()


@unittest.skipUnless((protocol.BASE / CORPUS / 'seeds_index.json').is_file(), 'V7 corpus seeds not exported')
class V7WriterPlanAgreement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = json.loads(json.dumps(protocol.build_plan('v7', 'v7-integration-check', task_corpus=CORPUS,
                                                             verifier_policy='unlimited')))

    def simulate(self, task):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        trial = root / task['run_id']
        (root / 'auth.json').write_text('{}\n')
        (root / 'python').mkdir()
        arguments = {'python_root': root / 'python', 'auth_file': root / 'auth.json', 'environment_config': {},
                     'verifier_policy': self.plan['settings']['verifier_policy']}
        if task.get('memory_seed'):
            arguments['memory_seed'] = protocol.local_path(protocol.BASE, task['memory_seed']['path'])
        pool = protocol.local_path(protocol.BASE, task['pool']['path'])
        with patch.object(harbor_runtime, 'command', no_docker):
            with self.assertRaises(StopBeforeDocker):
                asyncio.run(runtime.run_same_task_trial(protocol.local_path(protocol.BASE, task['source_path']), trial,
                                                        pool, task['task_id'], review_image='sha256:x', **arguments))
        (trial / 'agent').mkdir(parents=True, exist_ok=True)
        result = {'task_id': task['task_id'], 'harbor': {'verifier_result': {'rewards': {'reward': 0.0}}},
                  'freeze': {'taken_before_hidden_grading': True, 'solver_stopped': True, 'snapshot_valid': True},
                  'review': {'valid': True, 'received_hidden_grade': False, 'evidence_unchanged': True,
                             'memory_audit': {'final_decision': {'agent_stage': 'review', 'task_id': task['task_id'],
                                                                'decision': 'no_update'}}}}
        (trial / 'v7_result.json').write_text(json.dumps(result))
        (trial / 'agent/model_audit.json').write_text(json.dumps({'model_effort_verified': True,
            'models': [self.plan['settings']['model']], 'efforts': [self.plan['settings']['reasoning_effort']]}))
        (trial / 'agent/usage.json').write_text(json.dumps({'input_tokens': 1, 'output_tokens': 1}))
        health = {'blocked': False, 'phases': {'agent': {'actual_inference': True, 'blocked': False},
                                              'review': {'actual_inference': True, 'blocked': False}}}
        with patch.object(run.health_guard, 'inspect_trial', side_effect=lambda _: copy.deepcopy(health)):
            return run.summary(trial, 'v7', task=task, settings=self.plan['settings'], memory_seed=run.seed_for(self.plan, task))

    def test_seeded_and_empty_tasks_summarize_valid(self):
        tasks = {task['task_id']: task for task in self.plan['tasks']}
        seeded = next(t for t in self.plan['tasks'] if t.get('memory_seed'))
        empty = next(t for t in self.plan['tasks'] if not t['pool']['trajectory_count'])
        for task in (seeded, empty, tasks.get('git-multibranch', seeded)):
            row = self.simulate(task)
            self.assertTrue(row['valid'], (task['task_id'], row))
            self.assertEqual(row['reasons'], [])

    def test_plan_pins_v7_inputs(self):
        self.assertEqual(self.plan['settings']['verifier_policy'], 'unlimited')
        for name in ('/index.json', '/seeds_index.json'):
            self.assertIn(CORPUS + name, self.plan['inputs_sha256'])
        self.assertIn('prepared/v7_corpus/decontamination_decisions.json', self.plan['inputs_sha256'])
        self.assertTrue(all('pool' in task for task in self.plan['tasks']))


@unittest.skipUnless((SNAPSHOT / 'golden_prompt_v5.txt').is_file() and (protocol.BASE / 'prepared/pool').is_dir(),
                     'golden V5/V6 recordings not present')
class V5V6GoldenPrompts(unittest.TestCase):
    """V7 is built on the live V5/V6 text, so the V5/V6 text itself must stay exactly what V6 ran with."""

    def test_v5_v6_solver_prompts_and_mcp_config_unchanged(self):
        pool = protocol.BASE / 'prepared/pool'
        self.assertEqual(harbor_runtime.solver_instructions(pool, '/app'), (SNAPSHOT / 'golden_prompt_v5.txt').read_text())
        self.assertEqual(harbor_runtime.solver_instructions(pool, '/app', initial_memory={'entry_count': 221}),
                         (SNAPSHOT / 'golden_prompt_v6.txt').read_text())
        config = harbor_runtime.make_config('task', '/app', 'solve')
        self.assertNotIn('--pool-scope', config)
        self.assertIn('"/opt/v5/serve_memory.py", "mcp", "--pool", "/pool", "--memory", "/memory", "--task-id", "task", '
                      '"--stage", "solve", "--workspace", "/app"', config.replace("'", '"'))


if __name__ == '__main__':
    unittest.main()
