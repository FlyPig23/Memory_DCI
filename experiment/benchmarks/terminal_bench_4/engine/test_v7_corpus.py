"""V7 same-task corpus checks with synthetic data only: no network, Docker or model."""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from experiment.benchmarks.terminal_bench_4.engine import v7_corpus as corpus

ARCHIVED = corpus.BASE / ('runs/invalid_infrastructure_20261003/source_snapshot/experiment/benchmarks/'
                          'terminal_bench_2_1/scripts/prepare_pool.py')
HUB = 'https://hub.harborframework.com'
TESTS = ('alpha-task', 'beta-task', 'zero-task')
TRAINING = ('train-task',)


def words(prefix, count):
    return ' '.join(f'{prefix}{i}' for i in range(count))


VISIBLE = 'visible passage ' + words('vis', 40)
ENVIRONMENT = 'environment passage ' + words('env', 40)
SOLUTION = 'solution only ' + words('sol', 40)
TESTS_TEXT = 'tests only ' + words('tst', 90)
README = 'readme only ' + words('rdm', 30)
SECRET = 'sk-' + 'A1b2C3d4E5f6G7h8I9j0K1l2'
LOCAL_SECRET = 'LOCALSECRET-0123456789abcdef'
SUCCESS_MARKER = 'SUCCESSFUL-RUN-MARKER-7f3a'
PNG_ONE = base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic image one').decode()
PNG_TWO = base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic image two').decode()
NATIVE = 'native log line\n\x00nul inside\nunicode é\n'.encode()
# n, task, model, CSV reward, CSV is_scored, CSV version_status, body kind
TRIALS = [
    (1, 'alpha-task', 'model-b', '0', 'True', 'exact', 'image'),
    (2, 'alpha-task', 'model-a', '0', 'True', 'exact', 'secret'),
    (3, 'alpha-task', 'model-a', '0', 'True', 'exact', 'fetch'),
    (4, 'alpha-task', 'model-a', '0', 'True', 'exact', 'solution'),
    (5, 'alpha-task', 'model-c', '0', 'True', 'exact', 'tests'),
    (6, 'alpha-task', 'model-c', '0', 'True', 'exact', 'visible'),
    (7, 'alpha-task', 'model-a', '0', 'True', 'exact', 'unavailable'),
    (8, 'alpha-task', 'model-b', '0', 'True', 'exact', 'native'),
    (9, 'alpha-task', 'model-a', '1', 'True', 'exact', 'success'),
    (10, 'alpha-task', 'model-a', '', 'True', 'exact', 'null'),
    (11, 'alpha-task', 'model-a', '0', 'False', 'exact', 'unscored'),
    (12, 'beta-task', 'model-a', '0', 'True', 'exact', 'testref'),
    (13, 'beta-task', 'model x/y', '0', 'True', 'exact', 'local'),
    (14, 'zero-task', 'model-a', '1', 'True', 'exact', 'success'),
    (15, 'train-task', 'model-a', '0', 'True', 'exact', 'training'),
    (16, 'alpha-task', 'model-a', '0', 'True', 'other', 'version'),
    (17, 'beta-task', 'model-b', '0', 'True', 'exact', 'plain'),
]
SELECTED = (1, 2, 3, 4, 5, 6, 7, 8, 12, 13, 17)


def tid(n):
    return f'{n:08x}-1111-4111-8111-{n:012x}'


def jid(n):
    return f'{n:08x}-2222-4222-8222-{n:012x}'


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def archived_render(value, output, images, trial_id, depth=0):
    """Verbatim copy of the archived prepare_pool.render() that produced the training pool."""
    indent = '  ' * depth
    if isinstance(value, dict):
        if not value:
            output.append(indent+'{}')
        for key, item in value.items():
            output.append(indent+str(key)+':')
            archived_render(item, output, images, trial_id, depth+1)
    elif isinstance(value, list):
        if not value:
            output.append(indent+'[]')
        for i, item in enumerate(value):
            output.append(indent+f'[{i}]')
            archived_render(item, output, images, trial_id, depth+1)
    elif isinstance(value, str):
        match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)', value)
        if match:
            blob = base64.b64decode(match[2], validate=False)
            name = hashlib.sha256(blob).hexdigest()+'.'+match[1]
            path = images/trial_id/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            value = f'/pool/images/{trial_id}/{name}'
        output.extend(indent+line for line in (value.splitlines() or ['""']))
    else:
        output.append(indent+json.dumps(value, ensure_ascii=False))


SAMPLE = json.loads(json.dumps({
    'schema_version': 'ATIF-v1.2', 'session_id': 'session', 'agent': {'name': 'terminus-2', 'extra': {}},
    'steps': [
        {'step_id': 1, 'source': 'user', 'message': 'Line one\nLine two\r\nLine three\n\nafter blank\n', 'tags': []},
        {'step_id': 2, 'tool_calls': [{'function_name': 'bash', 'arguments': {'command': 'printf "a\\tb"\necho done'}}],
         'observation': {'results': [{'content': ''}, {'content': 'nul\x00byte, unicode é 漢   separator'}]},
         'metrics': {'prompt_tokens': 12, 'cost': 0.5, 'cached': None, 'ok': True, 'tiny': 1e-07, 'big': 10 ** 20}},
        {'step_id': 3, 'message': 'data:image/png;base64,' + PNG_ONE},
        {'step_id': 4, 'message': 'data:image/webp;base64,' + PNG_TWO[:8] + '\n' + PNG_TWO[8:]},
        {'step_id': 5, 'message': '<img src="data:image/png;base64,' + PNG_ONE + '"> stays verbatim inside text'},
        {'step_id': 6, 'message': 'data:image/gif;base64,R0lGODlh'},
        {'step_id': 7, 'nested': [[], {}, [{'deep': ['x', 1.5, False]}]]},
    ]}))


class FakeHub:
    """Serves synthetic Hub responses by exact URL; unknown URLs are HTTP 404."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise corpus.HTTPStatusError(404, url)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


class World:
    """A synthetic benchmark directory: tasks, split, official CSV, registry, public trials, Hub."""

    def __init__(self, root: Path, *, failing=()):
        self.root = root
        self.base = root / 'repo/experiment/benchmarks/terminal_bench_2_1'
        self.data = self.base / 'data/historical_trials'
        self.creds = root / 'credentials'
        self.hub = FakeHub()
        self.public = {}
        auth = self.creds / 'experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json'
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({'tokens': {'access_token': LOCAL_SECRET}}))
        self.write_tasks()
        self.write_trials(set(failing))
        corpus.write_sources(self.base)
        self.put_local_body(13)
        self.audit = self.download()

    def instruction(self, task):
        return f'Build the {task} widget.\n{VISIBLE}\n'

    def write_tasks(self):
        rows = []
        for task in TESTS + TRAINING:
            directory = self.base / 'prepared/tasks' / task
            files = {'instruction.md': self.instruction(task), 'task.toml': '[agent]\ntimeout_sec = 60\n',
                     'README.md': f'# Package readme\n{README}\n',
                     'environment/Dockerfile': f'FROM python:3.12-slim\n# {ENVIRONMENT}\n',
                     'tests/test_outputs.py': f'# {TESTS_TEXT}\n# {VISIBLE}\n',
                     'tests/test.sh': '#!/bin/bash\npytest /tests/test_outputs.py\n',
                     'solution/solve.sh': f'#!/bin/bash\n# {SOLUTION}\n# {ENVIRONMENT}\n'}
            for name, text in files.items():
                (directory / name).parent.mkdir(parents=True, exist_ok=True)
                (directory / name).write_text(text)
            (directory / 'tests/fixture.bin').write_bytes(b'\x00\x01binary fixture')
            row = {'task_id': task, 'task_version_id': f'{task}-version', 'task_revision': 1,
                   'source_path': f'prepared/tasks/{task}', 'split': 'training' if task in TRAINING else 'test',
                   'instruction_sha256': corpus.sha(directory / 'instruction.md')}
            if task != 'beta-task':
                row.update(class_id='C01', class_label='Synthetic class', fine_family='synthetic_family',
                           transfer_tags=['python', 'widgets'])
            rows.append(row)
        self.split = {'tasks': rows, 'test_task_ids': list(TESTS), 'training_task_ids': list(TRAINING)}
        self.write_json('manifests/split.json', self.split)

    def write_json(self, relative, value):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2))

    def body(self, n, task, kind):
        messages = {'image': ['looked at the screen', 'data:image/png;base64,' + PNG_ONE],
                    'secret': [f'export OPENAI_KEY_VALUE={SECRET}', f'the local token {LOCAL_SECRET} leaked'],
                    'fetch': ['curl -s "https://github.com/search?q=alpha+widget"', 'data:image/png;base64,' + PNG_TWO],
                    'solution': ['my notes: ' + SOLUTION], 'tests': ['I recall: ' + TESTS_TEXT],
                    'visible': ['Re-reading the task: ' + VISIBLE + ' and the image: ' + ENVIRONMENT],
                    'testref': ['running python -m pytest tests/test_outputs.py'],
                    'local': ['local body reused'], 'plain': [f'plain failure {n}'],
                    'success': [SUCCESS_MARKER]}.get(kind, ['unused'])
        return {'schema_version': 'ATIF-v1.2', 'session_id': 'session', 'agent': {'name': 'terminus-2'},
                'steps': [{'step_id': 1, 'source': 'user', 'message': self.instruction(task)}]
                + [{'step_id': i + 2, 'source': 'agent', 'message': m} for i, m in enumerate(messages)]}

    def write_trials(self, failing):
        csv_rows, registry = [], []
        for n, task, model, reward, scored, version, kind in TRIALS:
            public_reward = {'1': 1, '': None}.get(reward, 0)
            self.public[n] = {'id': tid(n), 'name': f'{task}__{n}', 'task_name': f'terminal-bench/{task}',
                              'source': 'terminal-bench/terminal-bench-2-1', 'agent_name': 'terminus-2',
                              'agent_version': '2.0.0', 'model_provider': 'synthetic', 'model_name': model,
                              'reward': public_reward, 'is_scored': scored == 'True', 'error_type': None,
                              'attempt': 1, 'n_attempts': 1, 'job_id': jid(n), 'job_name': f'job-{n}',
                              'leaderboard_row_ids': ['row-1']}
            csv_rows.append({'row_id': 'row-1', 'trial_id': tid(n), 'task_id': task, 'reward': reward,
                             'is_scored': scored, 'reward_status': 'binary', 'error_type': '', 'version_status': version})
            registry.append({'id': tid(n), 'job_id': jid(n), 'task_name': f'terminal-bench/{task}',
                             'task_content_hash': 'f' * 64, 'lock': {'env': {'HIDDEN_ENV': 'do-not-copy-env'}},
                             'config': {'env': {'HIDDEN_ENV': 'do-not-copy-env'}},
                             'archive_path': f'trials/{tid(n)}/trial.tar.gz',
                             'trajectory_path': None if kind == 'unavailable' else f'trials/{tid(n)}/trajectory.json'})
            self.route(n, task, kind, n in failing)
        self.csv_rows = csv_rows
        self.write_csv(csv_rows)
        self.write_json('data/discovery/registry_trials.json', registry)
        self.write_json('manifests/historical/public_trials.json', {'trials': list(self.public.values())})

    def write_csv(self, rows):
        path = self.base / 'reports/official_test_trials.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def route(self, n, task, kind, failing):
        viewer = corpus.viewer_url(tid(n), jid(n))
        base, query = f'{HUB}/api/trials/{tid(n)}', f'jobId={jid(n)}'
        if failing:
            self.hub.routes[viewer] = OSError('connection reset by synthetic peer')
        elif kind in ('unavailable', 'native'):
            self.hub.routes[viewer] = b'null'
            files = ([{'path': 'config.json', 'size': 2, 'is_dir': False}] if kind == 'unavailable' else
                     [{'path': 'agent', 'size': 0, 'is_dir': True},
                      {'path': 'agent/claude-code.txt', 'size': len(NATIVE), 'is_dir': False}])
            self.hub.routes[f'{base}/files?{query}'] = json.dumps({'files': files}).encode()
            self.hub.routes[f'{base}/summary?{query}'] = (
                b'{"exception_info": {"exception_type": "AgentTimeoutError"}}' if kind == 'unavailable' else b'{}')
            self.hub.routes[f'{base}/files/agent/claude-code.txt?{query}'] = NATIVE
        else:
            self.hub.routes[viewer] = json.dumps(self.body(n, task, kind)).encode()

    def put_local_body(self, n):
        """An existing local body with the archived sidecar format (reused, never re-downloaded)."""
        directory = self.data / tid(n)
        directory.mkdir(parents=True)
        data = self.hub.routes[corpus.viewer_url(tid(n), jid(n))]
        (directory / 'trajectory.json').write_bytes(data)
        relative = f'data/historical_trials/{tid(n)}/trajectory.json'
        url = f'{HUB}/api/trials/{tid(n)}/trajectory?jobId={jid(n)}&trajectory_path=trials%2F{tid(n)}%2Ftrajectory.json'
        (directory / 'trajectory.json.source.json').write_text(json.dumps(
            {'url': url, 'retrieved_at': '2026-10-01T00:00:00Z', 'bytes': len(data),
             'sha256': sha_bytes(data), 'path': relative}, indent=2) + '\n')
        (directory / 'trial_metadata.json').write_text(json.dumps(self.public[n], indent=2) + '\n')
        # A body of a successful attempt on disk must never be selected.
        success = self.data / tid(9)
        success.mkdir(parents=True)
        (success / 'trajectory.json').write_text(json.dumps(self.body(9, 'alpha-task', 'success')))

    def download(self, **kwargs):
        with patch.object(corpus, 'http_get', self.hub), patch.object(corpus, '_sleep', lambda seconds: None):
            return corpus.download_sources(self.base, **kwargs)

    def build(self, name='b1', **kwargs):
        kwargs.setdefault('credentials_root', self.creds)
        return corpus.build_corpus(self.base / 'prepared/v7_corpus' / name, base=self.base, **kwargs)

    def pool(self, name, task):
        return self.base / 'prepared/v7_corpus' / name / 'pools' / task

    def audit_of(self, name, task):
        return json.loads((self.base / 'prepared/v7_corpus' / name / 'audit' / f'{task}.json').read_text())


class WorldTest(unittest.TestCase):
    failing = ()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.world = World(Path(temporary.name), failing=self.failing)
        self.base = self.world.base

    def mutable_copy(self, pool):
        target = self.world.root / ('copy-' + os.urandom(4).hex()) / pool.name
        shutil.copytree(pool, target)
        for path in target.rglob('*'):
            if path.is_file():
                path.chmod(0o644)
        return target


class SourceTests(WorldTest):
    def test_selection_excludes_reward_one_null_unscored_other_versions_and_other_tasks(self):
        selected = corpus.select_failures(self.world.split, self.world.csv_rows)
        self.assertEqual(sorted(r['trial_id'] for r in selected), sorted(tid(n) for n in SELECTED))
        manifest = json.loads((self.base / 'manifests/v7_failure_sources.json').read_text())
        self.assertEqual(manifest['kind'], 'v7_failure_sources')
        self.assertEqual(manifest['counts_by_task'], {'alpha-task': 8, 'beta-task': 3, 'zero-task': 0})
        self.assertEqual(set(manifest['inputs_sha256']), set(corpus.SOURCE_INPUTS))
        self.assertTrue(all(tuple(row) == corpus.ROW_FIELDS for row in manifest['rows']))
        text = json.dumps(manifest)
        for n in (9, 10, 11, 14, 15, 16):
            self.assertNotIn(tid(n), text)
        self.assertNotIn('do-not-copy-env', text)
        self.assertEqual(set(manifest), {'schema_version', 'kind', 'created_at', 'inputs_sha256', 'selection',
                                         'row_count', 'counts_by_task', 'rows'})
        null_row = next(r for r in manifest['rows'] if r['trial_id'] == tid(7))
        self.assertIsNone(null_row['registry_trajectory_path'])
        with self.assertRaisesRegex(ValueError, 'Duplicate trial'):
            corpus.select_failures(self.world.split, self.world.csv_rows + self.world.csv_rows[:1])

    def test_join_refuses_missing_rows_and_public_rewards_that_are_not_zero(self):
        path = self.base / 'manifests/historical/public_trials.json'
        original = path.read_text()
        public = json.loads(original)
        public['trials'][0]['reward'] = 1
        path.write_text(json.dumps(public))
        with self.assertRaisesRegex(ValueError, 'not an official scored failure'):
            corpus.failure_sources(self.base)
        public['trials'] = public['trials'][1:]
        path.write_text(json.dumps(public))
        with self.assertRaisesRegex(ValueError, 'lacks its registry/public join'):
            corpus.failure_sources(self.base)
        path.write_text(original)
        registry = self.base / 'data/discovery/registry_trials.json'
        rows = json.loads(registry.read_text())
        rows[1]['job_id'] = 'another-job'
        registry.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            corpus.failure_sources(self.base)

    def test_sources_are_idempotent_and_refuse_a_changed_selection_without_replace(self):
        manifest, written = corpus.write_sources(self.base)
        self.assertFalse(written)
        rows = [r for r in self.world.csv_rows if r['trial_id'] != tid(17)]
        self.world.write_csv(rows)
        with self.assertRaisesRegex(ValueError, '--replace'):
            corpus.write_sources(self.base)
        manifest, written = corpus.write_sources(self.base, replace=True)
        self.assertTrue(written)
        self.assertEqual(manifest['counts_by_task']['beta-task'], 2)

    def test_changed_inputs_block_the_build(self):
        registry = self.base / 'data/discovery/registry_trials.json'
        registry.write_text(registry.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'input changed'):
            self.world.build()


class DownloadTests(WorldTest):
    failing = (17,)

    def test_download_writes_sidecars_metadata_markers_and_audit(self):
        audit = self.world.audit
        statuses = {row['trial_id']: (row['status'], row['action']) for row in audit['trials']}
        self.assertEqual(statuses[tid(13)], ('available', 'reused'))
        self.assertEqual(statuses[tid(1)], ('available', 'downloaded'))
        self.assertEqual(statuses[tid(7)], ('unavailable', 'recovered'))
        self.assertEqual(statuses[tid(8)], ('raw_fallback', 'recovered'))
        self.assertEqual(statuses[tid(17)], ('unresolved', 'failed'))
        self.assertEqual(audit['counts'], {'available': 8, 'raw_fallback': 1, 'unavailable': 1, 'unresolved': 1})
        self.assertFalse(audit['complete'])
        self.assertNotIn(corpus.viewer_url(tid(13), jid(13)), self.world.hub.calls)
        directory = self.world.data / tid(1)
        sidecar = json.loads((directory / 'trajectory.json.source.json').read_text())
        self.assertEqual(set(sidecar), {'url', 'retrieved_at', 'bytes', 'sha256', 'path'})
        self.assertEqual(sidecar['path'], f'data/historical_trials/{tid(1)}/trajectory.json')
        self.assertEqual(sidecar['url'], f'{HUB}/api/trials/{tid(1)}/trajectory?jobId={jid(1)}'
                                         f'&trajectory_path=trials%2F{tid(1)}%2Ftrajectory.json')
        self.assertEqual(sidecar['sha256'], corpus.sha(directory / 'trajectory.json'))
        atif = json.loads((directory / 'atif_download.json').read_text())
        self.assertEqual(set(atif), {'trial_id', 'task_name', 'mode', 'files', 'errors', 'status', 'finished_at'})
        self.assertEqual((atif['mode'], atif['status'], atif['files']), ('atif', 'complete', [sidecar]))
        self.assertEqual((directory / 'trial_metadata.json').read_bytes(),
                         (json.dumps(self.world.public[1], ensure_ascii=False, indent=2) + '\n').encode())
        missing = self.world.data / tid(7)
        self.assertFalse((missing / 'atif_download.json').exists())
        marker = json.loads((missing / 'trajectory_unavailable.json').read_text())
        self.assertEqual(marker['status'], 'official_unavailable')
        self.assertIn('AgentTimeoutError', marker['reason'])
        raw = json.loads((self.world.data / tid(8) / 'trajectory_raw_fallback.json').read_text())
        self.assertEqual((raw['status'], raw['source_format']), ('official_raw_unparsed', 'native_agent_log'))
        self.assertEqual(json.loads((self.world.data / tid(8) / 'atif_download.json').read_text())['status'], 'partial')
        self.assertFalse((self.world.data / tid(17) / 'trajectory.json').exists())
        self.assertFalse((self.world.data / tid(17) / 'trajectory_unavailable.json').exists())

    def test_second_download_reuses_everything_resolved_and_retries_unresolved(self):
        calls = len(self.world.hub.calls)
        del self.world.hub.routes[corpus.viewer_url(tid(17), jid(17))]
        self.world.route(17, 'beta-task', 'plain', False)
        audit = self.world.download()
        self.assertTrue(audit['complete'])
        retried = self.world.hub.calls[calls:]
        self.assertEqual(retried, [corpus.viewer_url(tid(17), jid(17))])

    def test_unresolved_sources_block_a_build_unless_diagnostic(self):
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.world.build()
        index = self.world.build('diagnostic', allow_unresolved=True)
        self.assertFalse(index['complete'])
        statuses = {r['trial_id']: r['status'] for r in index['tasks']['beta-task']['records']}
        self.assertEqual(statuses[tid(17)], 'unresolved')
        card = (self.world.pool('diagnostic', 'beta-task') / 'tasks/beta-task.md').read_text()
        self.assertNotIn('attempt=attempt-002', card)
        with self.assertRaisesRegex(ValueError, 'complete'):
            corpus.verify_corpus(self.base / 'prepared/v7_corpus/diagnostic', self.world.split)

    def test_request_retry_policy(self):
        sleeps, calls = [], []

        def run(responses):
            sleeps.clear(), calls.clear()

            def get(url):
                calls.append(url)
                value = responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value
            with patch.object(corpus, 'http_get', get), patch.object(corpus, '_sleep', sleeps.append):
                return corpus.request('https://hub.harborframework.com/x')

        with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
            run([corpus.HTTPStatusError(404, 'u')])
        self.assertEqual((len(calls), sleeps), (1, []))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            run([corpus.HTTPStatusError(500, 'u')] * 4)
        self.assertEqual((len(calls), sleeps), (4, [1, 2, 4]))
        self.assertEqual(run([OSError('reset'), b'body']), b'body')
        self.assertEqual(sleeps, [1])

    def test_invalid_local_sidecar_is_refetched_and_tampering_blocks_a_build(self):
        directory = self.world.data / tid(13)
        (directory / 'trajectory.json').write_bytes(b'{"steps": []}')
        self.assertEqual(corpus.trial_state(json.loads(json.dumps(self.row(13))), self.world.data)['status'], 'invalid')
        with self.assertRaisesRegex(ValueError, 'fails validation'):
            self.world.build('tampered', allow_unresolved=True)
        audit = self.world.download()
        self.assertEqual({r['trial_id']: r['action'] for r in audit['trials']}[tid(13)], 'downloaded')

    def row(self, n):
        rows = json.loads((self.base / 'manifests/v7_failure_sources.json').read_text())['rows']
        return next(r for r in rows if r['trial_id'] == tid(n))


class BuildTests(WorldTest):
    def setUp(self):
        super().setUp()
        self.index = self.world.build()
        self.alpha = self.world.pool('b1', 'alpha-task')

    def test_header_card_and_manifest_formats(self):
        lines = (self.alpha / 'trajectories/alpha-task/model-a/attempt-001/alpha-task_0.txt').read_text().split('\n')
        self.assertEqual(lines[:8], [
            '# Historical failed attempt at this task: alpha-task',
            'Model: model-a; agent: terminus-2 2.0.0',
            'Official reward: 0; attempt ID: attempt-001',
            'Official scored execution: True; attempt: 1; parent scored trial: self',
            'Source task compatibility: exact_task_version; target task version: alpha-task-version',
            'The reward is a source-run outcome, not a guarantee for every historical action.',
            'All ATIF fields below are rendered without summarizing or distilling; embedded images become local files.',
            ''])
        self.assertEqual(lines[8:11], ['schema_version:', '  ATIF-v1.2', 'session_id:'])
        native = (self.alpha / 'trajectories/alpha-task/model-b/attempt-006/alpha-task_0.txt').read_bytes()
        header = ('# Historical failed attempt at this task: alpha-task\nModel: model-b; agent: terminus-2 2.0.0\n'
                  'Official reward: 0; attempt ID: attempt-006\n'
                  'Official scored execution: True; attempt: 1; parent scored trial: self\n'
                  'Source task compatibility: exact_task_version; target task version: alpha-task-version\n'
                  'The reward is a source-run outcome, not a guarantee for every historical action.\n'
                  'Official source format: original native agent log, retained byte-for-byte without conversion.\n'
                  'Official source availability reason: Official viewer has no ATIF; complete original native agent '
                  'log retained byte-for-byte, without summarization or conversion.\n\n')
        self.assertEqual(native, header.encode() + NATIVE)
        card = (self.alpha / 'tasks/alpha-task.md').read_text()
        rows = [f'- /pool/trajectories/alpha-task/{m}/attempt-{a}/alpha-task_0.txt | score=0 | model={m} '
                f'| scored=True | attempt=attempt-{a}' for m, a in (('model-a', '001'), ('model-b', '005'),
                                                                     ('model-b', '006'), ('model-c', '008'))]
        expected = ('# alpha-task\n\n## Preclassification (researcher-defined; not an official benchmark category)\n'
                    'Class: C01 · Synthetic class\nFine family: synthetic_family\nTransfer tags: python, widgets\n'
                    'Use the current task constraints to judge applicability; shared category is not proof of transfer.\n\n'
                    + self.world.instruction('alpha-task') + '\n\n## Historical trajectories\n'
                    '5 officially graded failed attempts at this task (official reward 0). Successful attempts are not included.\n'
                    'Keep failures and partial runs. Check actual actions and observations before adopting a method.\n\n'
                    '- NO TRAJECTORY BODY AVAILABLE (official source confirmed) | score=0 | model=model-a | scored=True '
                    '| attempt=attempt-004\n' + '\n'.join(rows) + '\n')
        self.assertEqual(card, expected)
        beta = (self.world.pool('b1', 'beta-task') / 'tasks/beta-task.md').read_text()
        self.assertTrue(beta.startswith('# beta-task\n\n' + self.world.instruction('beta-task') + '\n\n## Historical'))
        manifest = json.loads((self.alpha / 'manifest.json').read_text())
        self.assertEqual(manifest, {
            'schema_version': 1, 'benchmark': 'Terminal-Bench 2.1', 'kind': 'same_task_failed_official_trajectories',
            'task_id': 'alpha-task', 'trajectory_count': 4, 'source_record_count': 5,
            'confirmed_unavailable_count': 1, 'raw_unparsed_trajectory_count': 1,
            'successful_trajectories_included': False,
            'trajectory_format': 'all-field ATIF text rendering; original raw/native fallbacks preserved byte-for-byte',
            'trajectory_path': 'trajectories/<task_id>/<model_alias>/<attempt_id>/<task_id>_0.txt',
            'mutable_memory': '/memory', 'cross_task_memory': False})
        readme = (self.alpha / 'README.md').read_text()
        self.assertIn('only officially graded failed attempts', readme)
        self.assertIn('No successful attempt', readme)
        self.assertIn('/memory for this task only', readme)
        self.assertEqual(sorted(p.name for p in self.alpha.iterdir()), ['README.md', 'images', 'manifest.json', 'tasks', 'trajectories'])
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o444 for p in self.alpha.rglob('*') if p.is_file()))

    def test_index_records_and_attempt_order(self):
        record = self.index['tasks']['alpha-task']
        self.assertEqual(list(self.index['tasks']), list(TESTS))
        self.assertTrue(self.index['complete'])
        self.assertEqual(record['pool_path'], 'prepared/v7_corpus/b1/pools/alpha-task')
        order = [(r['attempt_id'], r['trial_id'], r['status']) for r in record['records']]
        self.assertEqual(order, [
            ('attempt-001', tid(2), 'available'), ('attempt-002', tid(3), 'excluded'),
            ('attempt-003', tid(4), 'excluded'), ('attempt-004', tid(7), 'unavailable'),
            ('attempt-005', tid(1), 'available'), ('attempt-006', tid(8), 'raw_fallback'),
            ('attempt-007', tid(5), 'excluded'), ('attempt-008', tid(6), 'available')])
        self.assertEqual((record['trajectory_count'], record['source_record_count'], record['excluded_count']), (4, 5, 3))
        self.assertEqual(record['pool_files_sha256'], corpus._protocol().inventory(self.alpha))
        self.assertEqual(record['pool_manifest_sha256'], corpus.sha(self.alpha / 'manifest.json'))
        for item in record['records']:
            if item['pool_file']:
                self.assertEqual(item['pool_sha256'], corpus.sha(self.alpha / item['pool_file']))
            else:
                self.assertIsNone(item['pool_sha256'])
        beta = [(r['attempt_id'], r['trial_id'], r['status']) for r in self.index['tasks']['beta-task']['records']]
        self.assertEqual(beta, [('attempt-001', tid(12), 'excluded'), ('attempt-002', tid(17), 'available'),
                                ('attempt-003', tid(13), 'available')])
        self.assertTrue((self.world.pool('b1', 'beta-task') / 'trajectories/beta-task/model-x-y/attempt-003').is_dir())
        self.assertEqual(corpus.verify_corpus(self.base / 'prepared/v7_corpus/b1', self.world.split), self.index)

    def test_attempt_ids_anonymize_hub_ids_and_no_success_reaches_a_pool(self):
        pools = self.base / 'prepared/v7_corpus/b1/pools'
        for path in pools.rglob('*'):
            for n, *_ in TRIALS:
                self.assertNotIn(tid(n), path.as_posix())
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(tid(n).encode(), data)
                    self.assertNotIn(jid(n).encode(), data)
                    self.assertNotIn(SUCCESS_MARKER.encode(), data)
        host = (self.base / 'prepared/v7_corpus/b1/index.json').read_text()
        host += ''.join(p.read_text() for p in (self.base / 'prepared/v7_corpus/b1/audit').glob('*.json'))
        for n in (9, 10, 11, 14, 15, 16):
            self.assertNotIn(tid(n), host)

    def test_sanitize_applied_before_hashing_and_recorded(self):
        path = self.alpha / 'trajectories/alpha-task/model-a/attempt-001/alpha-task_0.txt'
        text = path.read_text()
        self.assertNotIn(SECRET, text)
        self.assertNotIn(LOCAL_SECRET, text)
        self.assertIn('[REDACTED_OPENAI_KEY]', text)
        self.assertIn('[REDACTED_KNOWN_LOCAL_CREDENTIAL]', text)
        record = next(r for r in self.world.audit_of('b1', 'alpha-task')['records'] if r['attempt_id'] == 'attempt-001')
        self.assertEqual(record['redactions'], {'known_local_credential': 1, 'openai_key': 1})
        self.assertEqual(record['pool_sha256'], corpus.sha(path))
        self.assertNotEqual(record['rendered_sha256'], record['pool_sha256'])
        self.assertNotIn(LOCAL_SECRET, json.dumps(self.world.audit_of('b1', 'alpha-task')))

    def test_decontamination_flags_leaks_but_not_instruction_or_environment_overlap(self):
        audit = {r['attempt_id']: r for r in self.world.audit_of('b1', 'alpha-task')['records']}
        self.assertEqual(audit['attempt-002']['decontamination']['reasons'], ['fetch_hits'])
        self.assertEqual(audit['attempt-002']['decontamination']['offsets']['fetch_hits'][0],
                         {'line': 31, 'match': 'github.com/search', 'field_hint': 'steps/message'})
        solution = audit['attempt-003']['decontamination']
        self.assertEqual(solution['solution_overlap'], 23)
        self.assertEqual(solution['review']['solution_overlap_max_run'], 42)
        self.assertEqual(solution['offsets']['solution_overlap'][0]['field_hint'], 'steps/message')
        self.assertEqual(audit['attempt-003']['decontamination']['reasons'], ['solution_overlap'])
        self.assertEqual(audit['attempt-007']['decontamination']['reasons'], ['tests_overlap_max_run'])
        self.assertGreaterEqual(audit['attempt-007']['decontamination']['tests_overlap_max_run'], 90)
        visible = audit['attempt-008']['decontamination']
        self.assertFalse(visible['flagged'])
        self.assertEqual((visible['solution_overlap'], visible['tests_overlap_max_run']), (0, 0))
        self.assertEqual(audit['attempt-002']['exclusion_reason'], 'decontamination_flag')
        beta = self.world.audit_of('b1', 'beta-task')['records'][0]['decontamination']
        self.assertEqual(beta['reasons'], ['test_file_refs'])
        # Without the visible-text exclusion the same quotation would count as overlap.
        tests_dir = self.base / 'prepared/tasks/alpha-task'
        unfiltered = {'solution': corpus.shingles([(tests_dir / 'solution/solve.sh').read_text()], 20),
                      'tests': corpus.shingles([(tests_dir / 'tests/test_outputs.py').read_text()], 12)}
        quoted = (self.alpha / 'trajectories/alpha-task/model-c/attempt-008/alpha-task_0.txt').read_text()
        self.assertTrue(corpus.contamination(quoted, unfiltered)['flagged'])
        report = corpus.decontam_report(self.base / 'prepared/v7_corpus/b1')
        self.assertEqual(sorted((r['task_id'], r['attempt_id']) for r in report), [
            ('alpha-task', 'attempt-002'), ('alpha-task', 'attempt-003'), ('alpha-task', 'attempt-007'),
            ('beta-task', 'attempt-001')])
        self.assertFalse(any(r['published'] for r in report))
        host = json.dumps(report) + ''.join(p.read_text() for p in (self.base / 'prepared/v7_corpus/b1').rglob('*.json'))
        for marker in ('tst5 ', 'sol5 ', 'rdm5 '):
            self.assertNotIn(marker, host)
        card = (self.alpha / 'tasks/alpha-task.md').read_text()
        for attempt in ('attempt-002', 'attempt-003', 'attempt-007'):
            self.assertNotIn(attempt, card)
            self.assertFalse(any(attempt in p.as_posix() for p in self.alpha.rglob('*')))

    def test_reviewed_decisions_keep_or_exclude(self):
        decisions = self.base / 'prepared/v7_corpus/decontamination_decisions.json'
        decisions.write_text(json.dumps({tid(3): {'decision': 'keep', 'reason': 'search result was unrelated'},
                                         tid(1): {'decision': 'exclude', 'reason': 'reviewer exclusion'}}))
        index = self.world.build('b2')
        self.assertEqual(index['decisions_sha256'], corpus.sha(decisions))
        statuses = {r['trial_id']: r['status'] for r in index['tasks']['alpha-task']['records']}
        self.assertEqual((statuses[tid(3)], statuses[tid(1)]), ('available', 'excluded'))
        pool = self.world.pool('b2', 'alpha-task')
        self.assertEqual(sorted(p.name for p in (pool / 'images').iterdir()), ['attempt-002'])
        decisions.write_text(json.dumps({'not-a-source': {'decision': 'keep', 'reason': 'x'}}))
        with self.assertRaisesRegex(ValueError, 'unknown failure source'):
            self.world.build('b3')

    def test_images_published_only_for_published_attempts(self):
        image_dirs = sorted(p.name for p in (self.alpha / 'images').iterdir())
        self.assertEqual(image_dirs, ['attempt-005'])
        blob = b'\x89PNG\r\n\x1a\nsynthetic image one'
        name = sha_bytes(blob) + '.png'
        self.assertEqual((self.alpha / 'images/attempt-005' / name).read_bytes(), blob)
        text = (self.alpha / 'trajectories/alpha-task/model-b/attempt-005/alpha-task_0.txt').read_text()
        self.assertIn(f'/pool/images/attempt-005/{name}', text)

    def test_zero_failure_task_gets_card_only_pool(self):
        pool = self.world.pool('b1', 'zero-task')
        self.assertEqual(list((pool / 'trajectories/zero-task').iterdir()), [])
        card = (pool / 'tasks/zero-task.md').read_text()
        self.assertIn('\n## Historical trajectories\n0 officially graded failed attempts at this task', card)
        self.assertFalse(re.search(r'^- ', card, re.MULTILINE))
        self.assertEqual(self.index['tasks']['zero-task']['records'], [])
        self.assertEqual(corpus.verify_task_pool(pool, 'zero-task')['trajectory_count'], 0)

    def test_build_refuses_overwrite_and_foreign_locations(self):
        with self.assertRaises(FileExistsError):
            self.world.build()
        with self.assertRaisesRegex(ValueError, 'prepared/v7_corpus'):
            corpus.build_corpus(self.base / 'prepared/elsewhere/b9', base=self.base, credentials_root=self.world.creds)

    def test_verify_task_pool_rejections(self):
        record = self.index['tasks']['alpha-task']
        self.assertEqual(corpus.verify_task_pool(self.alpha, 'alpha-task', record)['trajectory_count'], 4)
        attempt = 'trajectories/alpha-task/model-a/attempt-001'

        def rejects(change, pattern, expected=None):
            pool = self.mutable_copy(self.alpha)
            change(pool)
            with self.assertRaisesRegex(ValueError, pattern):
                corpus.verify_task_pool(pool, 'alpha-task', expected)

        def success(pool):
            path = pool / 'trajectories/alpha-task/model-a/attempt-009/alpha-task_1.txt'
            path.parent.mkdir()
            path.write_text('# Historical failed attempt at this task: alpha-task\n')
        rejects(success, 'exactly alpha-task_0.txt')
        rejects(lambda pool: (pool / attempt / 'alpha-task_0.txt').rename(pool / attempt / 'alpha-task_unscored.txt'),
                'exactly alpha-task_0.txt')
        rejects(lambda pool: (pool / 'trajectories/other-task').mkdir(), 'exactly the current task directory')
        rejects(lambda pool: (pool / 'tasks/other-task.md').write_text('# other-task\n'), 'exactly the current task card')
        rejects(lambda pool: (pool / attempt / 'link').symlink_to(pool / 'README.md'), 'Symbolic links')

        def header(pool, old, new):
            path = pool / attempt / 'alpha-task_0.txt'
            path.write_text(path.read_text().replace(old, new, 1))
        rejects(lambda pool: header(pool, 'Official reward: 0', 'Official reward: 1'), 'header')
        rejects(lambda pool: header(pool, 'Official scored execution: True', 'Official scored execution: False'), 'header')
        rejects(lambda pool: header(pool, 'this task: alpha-task', 'this task: beta-task'), 'header')

        def manifest(pool, **changes):
            path = pool / 'manifest.json'
            path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))
        rejects(lambda pool: manifest(pool, trajectory_count=5, source_record_count=6), 'count differs')
        rejects(lambda pool: manifest(pool, successful_trajectories_included=True), 'failure-only')
        rejects(lambda pool: manifest(pool, training_task_ids=['train-task']), 'unexpected or missing')
        rejects(lambda pool: (pool / 'README.md').write_text('changed\n'), 'frozen corpus record', record)
        rejects(lambda pool: manifest(pool), 'frozen corpus record', record)
        rejects(lambda pool: shutil.rmtree(pool / 'images'), 'not referenced|frozen corpus record', record)

    def test_verify_corpus_rejects_successes_and_tampering(self):
        root = self.base / 'prepared/v7_corpus/b1'
        rows = [dict(r, reward='1') if r['trial_id'] == tid(2) else r for r in self.world.csv_rows]
        self.world.write_csv(rows)
        with self.assertRaisesRegex(ValueError, 'not an official scored failure'):
            corpus.verify_corpus(root, self.world.split)
        self.world.write_csv(self.world.csv_rows)
        corpus.verify_corpus(root, self.world.split)
        target = root / 'pools/alpha-task/trajectories/alpha-task/model-a/attempt-001/alpha-task_0.txt'
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b'appended\n')
        with self.assertRaisesRegex(ValueError, 'frozen corpus record'):
            corpus.verify_corpus(root, self.world.split)
        with self.assertRaisesRegex(ValueError, 'cover exactly'):
            corpus.verify_corpus(root, dict(self.world.split, test_task_ids=['alpha-task', 'beta-task']))

    def test_cli_uses_explicit_roots(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(corpus.main(['verify', '--base', str(self.base), '--build', 'b1']), 0)
            self.assertEqual(corpus.main(['decontam-report', '--base', str(self.base), '--build', 'b1']), 0)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0]['verified_tasks'], 3)
        self.assertEqual(lines[-1]['flagged'], 4)
        manifests = self.world.root / 'other-manifests'
        with redirect_stdout(io.StringIO()):
            corpus.main(['sources', '--base', str(self.base), '--manifests-root', str(manifests)])
            with patch.object(corpus, 'http_get', self.world.hub):
                corpus.main(['download', '--base', str(self.base), '--manifests-root', str(manifests),
                             '--data-root', str(self.world.data), '--workers', '2'])
            corpus.main(['build', '--base', str(self.base), '--out', 'prepared/v7_corpus/cli',
                         '--manifests-root', str(manifests), '--data-root', str(self.world.data)])
        self.assertTrue((manifests / 'v7_download_audit.json').is_file())
        self.assertTrue((self.base / 'prepared/v7_corpus/cli/index.json').is_file())


class RenderTests(unittest.TestCase):
    def render_both(self, value):
        with tempfile.TemporaryDirectory() as temporary:
            images = Path(temporary)
            expected, actual, blobs = [], [], {}
            archived_render(value, expected, images, 'attempt-001')
            corpus.render(value, actual, blobs, 'attempt-001')
            stored = {p.name: p.read_bytes() for p in (images / 'attempt-001').iterdir()} if (images / 'attempt-001').exists() else {}
        return expected, actual, stored, blobs

    def test_renderer_byte_equals_archived_renderer(self):
        expected, actual, stored, blobs = self.render_both(SAMPLE)
        self.assertEqual(actual, expected)
        self.assertEqual(blobs, stored)
        self.assertEqual(len(blobs), 2)
        text = '\n'.join(actual)
        self.assertIn('<img src="data:image/png;base64,' + PNG_ONE, text)
        self.assertIn('data:image/gif;base64,R0lGODlh', text)
        self.assertIn('nul\x00byte', text)
        self.assertIn('      ""', text)

    @unittest.skipUnless(ARCHIVED.is_file(), 'archived prepare_pool.py snapshot is not in this checkout')
    def test_renderer_matches_the_archived_file(self):
        spec = importlib.util.spec_from_file_location('archived_prepare_pool', ARCHIVED)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            expected, actual = [], []
            module.render(SAMPLE, expected, Path(temporary), 'attempt-001')
            corpus.render(SAMPLE, actual, {}, 'attempt-001')
        self.assertEqual(actual, expected)

    def test_unencodable_text_is_escaped_not_dropped(self):
        data, images, escaped = corpus.render_atif({'steps': ['lone \ud800 surrogate']}, ['# h', ''], 'attempt-001')
        self.assertTrue(escaped)
        self.assertIn(b'lone \\ud800 surrogate', data)
        data, images, escaped = corpus.render_atif({'steps': ['plain']}, ['# h', ''], 'attempt-001')
        self.assertFalse(escaped)
        self.assertEqual(data, b'# h\n\nsteps:\n  [0]\n    plain\n')


if __name__ == '__main__':
    unittest.main()
