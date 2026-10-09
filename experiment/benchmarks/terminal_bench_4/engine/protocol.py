"""Path, source and setting validation shared by the standalone V8 workflow."""
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
SOURCE_FILES = ('protocol.py', 'run.py', 'runtime.py', 'harbor_runtime.py',
                'health_guard.py',
                'serve_memory.py')
# Imported only on V7 code paths, so only V7 plans hash them.
V7_SOURCE_FILES = ('v7_corpus.py', 'v7_seed.py', 'package_data.py')


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




def sources(base=BASE, arm=None):
    base = Path(base)
    root = base.parents[2]
    paths = [base / 'engine' / name for name in SOURCE_FILES + (V7_SOURCE_FILES if arm == 'v7' else ())]
    paths += [root / 'experiment/shared' / name for name in ('codex_backend.py', 'task_runtime.py')]
    paths += [root / 'experiment/shared/memory' / name for name in ('tools.py', 'prompt.txt', 'review_prompt.txt', 'seed.py')]
    return {p.relative_to(root).as_posix(): sha(p) for p in paths}
