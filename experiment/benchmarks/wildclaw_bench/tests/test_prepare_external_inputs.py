"""Public staging must preserve official transient inputs without opening gold."""
from pathlib import Path

import pytest

from experiment.benchmarks.wildclaw_bench.scripts import prepare_external_inputs as staging


def setup(monkeypatch, root):
    monkeypatch.setattr(staging, 'PROJECT', root)
    monkeypatch.setattr(staging, 'SNAPSHOT', root / 'snapshot')
    monkeypatch.setattr(staging, 'STAGING', root / 'staging')
    source = root / 'snapshot/workspace/category/task'
    (source / 'exec').mkdir(parents=True)
    (source / 'exec/input.txt').write_text('original public input')
    (source / 'tmp').mkdir()
    (source / 'tmp/messages.json').write_text('{"messages": ["scenario-specific"]}')
    (source / 'generate_puzzle.py').write_text('raise RuntimeError("never execute me")')
    (source / 'gt').mkdir()
    (source / 'gt/answer.txt').write_text('must not be opened during staging')
    return source, [{'task_id': 'task', 'public_workspace_relpath': 'workspace/category/task'}]


def test_stage_preserves_tmp_and_root_helpers_with_independent_hash_verified_copies(tmp_path, monkeypatch):
    source, tasks = setup(monkeypatch, tmp_path)
    original_hash = staging.sha256

    def no_gold_hash(path):
        assert 'gt' not in Path(path).parts
        return original_hash(path)

    monkeypatch.setattr(staging, 'sha256', no_gold_hash)
    rows = staging.stage(tasks)
    target = tmp_path / 'staging/category/task'
    assert (target / 'tmp/messages.json').read_bytes() == (source / 'tmp/messages.json').read_bytes()
    assert (target / 'generate_puzzle.py').is_file()
    assert not (target / 'exec/generate_puzzle.py').exists()
    assert (target / 'gt').is_symlink() and (target / 'gt').resolve() == source / 'gt'
    assert rows[0]['verified_public_file_count'] == 3
    assert rows[0]['verified_auxiliary_file_count'] == 2
    assert all(row['sha256'] == row['staged_sha256'] for row in rows[0]['public_inputs'])
    assert (target / 'tmp/messages.json').stat().st_nlink == 1
    (target / 'tmp/messages.json').write_text('modified staging')
    assert 'scenario-specific' in (source / 'tmp/messages.json').read_text()
    with pytest.raises(ValueError, match='differs'):
        staging.stage(tasks)


def test_stage_is_idempotent_and_reports_every_public_path(tmp_path, monkeypatch):
    _, tasks = setup(monkeypatch, tmp_path)
    first, second = staging.stage(tasks), staging.stage(tasks)
    assert first[0]['newly_copied_public_files'] == 3
    assert second[0]['newly_copied_public_files'] == 0
    assert first[0]['public_inputs'] == second[0]['public_inputs']


def test_stage_rejects_unexpected_public_symlink_without_following_it(tmp_path, monkeypatch):
    source, tasks = setup(monkeypatch, tmp_path)
    (source / 'tmp/link').symlink_to(source / 'gt/answer.txt')
    with pytest.raises(ValueError, match='symlink'):
        staging.stage(tasks)


def test_absent_original_workspace_is_explicit_and_does_not_invent_inputs(tmp_path, monkeypatch):
    _, _ = setup(monkeypatch, tmp_path)
    rows = staging.stage([{'task_id': 'no-assets', 'public_workspace_relpath': 'workspace/empty/task'}])
    assert rows[0]['snapshot_workspace_exists'] is False
    assert rows[0]['public_inputs'] == []
    assert (tmp_path / 'staging/empty/task/exec').is_dir()
