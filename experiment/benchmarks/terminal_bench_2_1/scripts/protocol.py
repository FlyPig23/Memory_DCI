"""Portable, zero-inference validation of the published fixed 36-task protocol."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tomllib

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parents[2]
PINNED_SPLIT_SHA256 = '4f437d77ebd35e420fe8dbd02fa34395f91c8f9a7d541f1d61798482e52938b6'
ARMS = ('baseline', 'v5', 'v6')
SOURCE_FILES = ('protocol.py', 'run.py', 'runtime.py', 'harbor_runtime.py',
                'baseline_runtime.py', 'baseline_supervisor.py', 'health_guard.py',
                'serve_memory.py')


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def local_path(root, relative):
    root = Path(root).resolve()
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Expected a contained relative path')
    candidate = root / path
    for component in (candidate, *candidate.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError('Symbolic links are not allowed in task/data paths')
    if not candidate.resolve().is_relative_to(root):
        raise ValueError('Path escapes its package')
    return candidate


def inventory(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('Expected a regular data directory')
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symbolic links are not allowed in data packages')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def finite_positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def inspect_task(task_path):
    """Reject unsupported execution semantics without changing official files."""
    path = Path(task_path)
    config = tomllib.loads((path / 'task.toml').read_text())
    for section in ('agent', 'verifier'):
        if not finite_positive(config.get(section, {}).get('timeout_sec')):
            raise ValueError(f'Task needs an explicit finite official {section} timeout: {path.name}')
    if config.get('steps'):
        raise ValueError('Multi-step tasks need a separate runtime adapter')
    environment = config.get('environment', {})
    if config.get('mcp_servers') or config.get('skills') or environment.get('mcp_servers') or environment.get('skills_dir'):
        raise ValueError('Task-supplied MCP/skills need a separate runtime adapter')
    if environment.get('tpu') or environment.get('os', 'linux') != 'linux':
        raise ValueError('Only Linux non-TPU tasks are supported')
    return config


def settings(base=BASE, *, model=None, effort=None, verifier_policy=None):
    value = load(Path(base) / 'config.json')
    if model is not None:
        value['model'] = model
    if effort is not None:
        value['reasoning_effort'] = effort
    if verifier_policy is not None:
        value['verifier_policy'] = verifier_policy
    if set(value) != {'model', 'reasoning_effort', 'cli_version', 'review_image', 'verifier_policy'}:
        raise ValueError('Unexpected or missing config.json settings')
    for name, setting in value.items():
        if not isinstance(setting, str) or not setting.strip() or any(ord(c) < 32 for c in setting):
            raise ValueError(f'Invalid setting: {name}')
    if value['verifier_policy'] not in ('official', 'unlimited'):
        raise ValueError('Unknown verifier policy')
    if value['cli_version'] != '0.153.4':
        raise ValueError('This adapter requires Codex CLI 0.153.4')
    return value


def run_directory(base, name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name):
        raise ValueError('Run name must contain 1-80 letters, digits, underscores, dots or hyphens')
    return local_path(base, 'runs/' + name)


def sources(base=BASE):
    base = Path(base)
    root = base.parents[2]
    paths = [base / 'scripts' / name for name in SOURCE_FILES]
    paths += [root / 'experiment/shared' / name for name in ('codex_backend.py', 'task_runtime.py')]
    paths += [root / 'experiment/shared/memory' / name for name in ('tools.py', 'prompt.txt', 'review_prompt.txt', 'seed.py')]
    return {p.relative_to(root).as_posix(): sha(p) for p in paths}


def verify_pool(base, split):
    base = Path(base)
    pool = local_path(base, 'prepared/pool')
    audit, public = load(base / 'manifests/pool.json'), load(pool / 'manifest.json')
    if audit['split_sha256'] != sha(base / 'manifests/split.json'):
        raise ValueError('Pool belongs to a different split')
    if inventory(pool) != audit['files_sha256']:
        raise ValueError('Prepared pool inventory or contents changed')
    training = set(split['training_task_ids'])
    if ({p.stem for p in (pool / 'tasks').glob('*.md')} != training
            or {p.name for p in (pool / 'trajectories').iterdir() if p.is_dir()} != training
            or set(public['training_task_ids']) != training):
        raise ValueError('Pool must contain exactly the frozen training tasks')
    if public.get('cross_task_memory') is not False or 'test_task_ids' in public or 'test_task_ids_excluded' in public:
        raise ValueError('Pool violates task-local/held-out isolation')


def build_plan(arm, run_name, *, base=BASE, model=None, effort=None, verifier_policy=None, memory_seed=None):
    """Read and hash inputs only. No auth read, run files, subprocesses or imports of Harbor."""
    base = Path(base)
    if arm not in ARMS:
        raise ValueError('Unknown arm')
    if (arm == 'v6') != (memory_seed is not None):
        raise ValueError('V6 requires --memory-seed; baseline and V5 do not accept a seed')
    namespace = hashlib.sha256(str(run_directory(base, run_name)).encode()).hexdigest()[:12]
    config = settings(base, model=model, effort=effort, verifier_policy=verifier_policy)
    split_path = base / 'manifests/split.json'
    if sha(split_path) != PINNED_SPLIT_SHA256:
        raise ValueError('Published fixed task split changed')
    split = load(split_path)
    test, training = split['test_task_ids'], split['training_task_ids']
    if len(test) != 36 or len(set(test)) != 36 or len(training) != 53 or len(set(training)) != 53 or set(test) & set(training):
        raise ValueError('Expected disjoint fixed 36-test/53-training split')
    rows = {row['task_id']: row for row in split['tasks']}
    task_files = load(base / 'manifests/task_files.json')
    packages = {row['task_version_id']: row for row in task_files}
    tasks = []
    for number, task_id in enumerate(test, 1):
        row = rows[task_id]
        relative = 'prepared/tasks/' + task_id
        if row['source_path'] != relative:
            raise ValueError('Task path differs from its fixed package location')
        directory = local_path(base, relative)
        package = packages[row['task_version_id']]
        expected = {f['path']: f['sha256'] for f in package['files']}
        if len(expected) != len(package['files']) or inventory(directory) != expected:
            raise ValueError(f'Official task package changed: {task_id}')
        if sha(directory / 'instruction.md') != row['instruction_sha256'] or sha(directory / 'task.toml') != row['task_config_sha256']:
            raise ValueError(f'Official instruction or config changed: {task_id}')
        spec = inspect_task(directory)
        if arm == 'baseline' and ((directory / 'environment/docker-compose.yaml').exists()
                or spec.get('environment', {}).get('gpus') or spec['verifier'].get('environment')):
            raise ValueError('Baseline requires single-service CPU tasks with a shared verifier')
        tasks.append({'task_id': task_id, 'task_version_id': row['task_version_id'],
                      'run_id': f'tb21-{namespace}-{number:02d}-{task_id}', 'source_path': relative,
                      'official_agent_timeout_seconds': spec['agent']['timeout_sec'],
                      'official_verifier_timeout_seconds': spec['verifier']['timeout_sec'],
                      'files_sha256': expected})
    input_names = ['split.json', 'task_files.json']
    if arm in ('v5', 'v6'):
        verify_pool(base, split)
        input_names += ['pool.json']
    inputs = {f'manifests/{name}': sha(base / 'manifests' / name) for name in input_names}
    seed = None
    if arm == 'v6':
        from experiment.shared.memory.seed import validate_seed
        seed_path = local_path(base, memory_seed)
        manifest = validate_seed(seed_path, pool=base / 'prepared/pool', training_task_ids=training)
        relative = seed_path.relative_to(base.resolve()).as_posix()
        seed = {'path': relative, 'manifest_sha256': sha(seed_path / 'manifest.json'),
                'entry_count': manifest['entry_count'], 'files_sha256': inventory(seed_path)}
        inputs[relative + '/manifest.json'] = seed['manifest_sha256']
    plan = {'schema_version': 1, 'benchmark': 'Terminal-Bench 2.1', 'arm': arm,
            'run_name': run_name, 'settings': config, 'harbor_version': '0.23.0',
            'review_timeout_seconds': 180 if arm in ('v5', 'v6') else None,
            'cross_task_memory': False, 'solver_rollouts_per_task': 1,
            'inputs_sha256': inputs,
            'sources_sha256': sources(base), 'tasks': tasks}
    if seed is not None:
        plan['memory_seed'] = seed
    return plan


def support(base=BASE):
    """Validate the isolated install, including internal interpreter symlinks."""
    base = Path(base).resolve()
    manifest = load(base / 'runtime/support_manifest.json')
    if manifest['versions']['codex'] != '0.153.4' or manifest['versions']['harbor'] != '0.23.0':
        raise ValueError('Installed runtime version differs from this adapter')
    seen = set()
    for record in manifest['files']:
        path = local_path(base, record['path'])
        if record['path'] in seen or not path.is_file() or sha(path) != record['sha256']:
            raise ValueError('Installed support file changed')
        seen.add(record['path'])
    for record in manifest.get('symlinks', []):
        relative = Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid support symlink path')
        path = base / relative
        if not path.is_symlink() or os.readlink(path) != record['target'] or not path.resolve().is_relative_to(base / 'runtime'):
            raise ValueError('Installed support symlink changed or escapes runtime')
    expected_paths = seen | {record['path'] for record in manifest.get('symlinks', [])}
    actual_paths = set()
    for root in (base / 'runtime/bin', local_path(base, manifest['python_root'])):
        for path in root.rglob('*'):
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if path.is_file() or path.is_symlink():
                actual_paths.add(path.relative_to(base).as_posix())
    if expected_paths != actual_paths:
        raise ValueError('Installed support inventory changed')
    for name in ('codex', 'codex-code-mode-host', 'rg'):
        relative = 'runtime/bin/' + name
        if relative not in seen or not os.access(base / relative, os.X_OK):
            raise ValueError('Required runtime binary is missing or not executable')
    python_root = local_path(base, manifest['python_root'])
    if not (python_root / 'bin/python3').is_file():
        raise ValueError('Standalone Python installation is incomplete')
    for relative in ('prepared/transport/ca-certificates.crt', 'prepared/transport/docker-compose-ca.yaml'):
        if not local_path(base, relative).is_file():
            raise ValueError('Run setup_runtime to create the local TLS transport overlay')
    return {'manifest_sha256': sha(base / 'runtime/support_manifest.json'),
            'python_root': manifest['python_root'],
            'transport_sha256': {p: sha(base / p) for p in ('prepared/transport/ca-certificates.crt',
                                                         'prepared/transport/docker-compose-ca.yaml')}}
