"""Archive restoration must not overwrite files through pre-existing links."""
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from scripts import download_data as downloader


def archive(path, name, payload):
    with tarfile.open(path, 'w:gz') as handle:
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))
    return [{'path': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}]


def test_restore_ignores_fixed_temporary_symlink(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'preserve')
    (root / 'sample.txt.restore-part').symlink_to(outside)
    packed = tmp_path / 'inputs.tar.gz'
    records = archive(packed, 'sample.txt', b'dataset')
    downloader.restore(packed, root, records)
    assert outside.read_bytes() == b'preserve'
    assert (root / 'sample.txt').read_bytes() == b'dataset'
    assert not (root / 'sample.txt').is_symlink()


def test_download_ignores_fixed_temporary_symlink(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.write_bytes(b'preserve')
    destination = tmp_path / 'cache' / 'inputs.tar.gz'
    destination.parent.mkdir()
    destination.with_name(destination.name + '.part').symlink_to(outside)
    monkeypatch.setattr(downloader.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'dataset'))
    downloader.download('https://example.invalid/file', destination, hashlib.sha256(b'dataset').hexdigest())
    assert outside.read_bytes() == b'preserve'
    assert destination.read_bytes() == b'dataset'


def task_fixture(root, relative='workspace/task'):
    manifests = root / 'experiment/manifests'
    manifests.mkdir(parents=True)
    (manifests / 'split.json').write_text(json.dumps({'test_task_ids': ['task']}))
    (manifests / 'task_manifest.json').write_text(json.dumps({'tasks': [
        {'task_id': 'task', 'public_workspace_relpath': relative}]}))
    source = root / 'experiment/trajectory library/WildClawBench/task/hf_snapshot/workspace/task'
    (source / 'exec').mkdir(parents=True)
    (source / 'exec/input.txt').write_text('public')
    (source / 'gt').mkdir()
    (source / 'gt/answer.txt').write_text('grader only')
    return source


def test_stage_rejects_parent_directory_escape(tmp_path):
    root = tmp_path / 'project'
    task_fixture(root)
    outside = tmp_path / 'outside'
    outside.mkdir()
    runtime = root / 'experiment/runtime'
    runtime.mkdir()
    (runtime / 'task_inputs').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='escapes destination'):
        downloader.stage_test_inputs(root)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('relative', ('workspace/../../../../outside', '/tmp/outside'))
def test_stage_rejects_traversal_in_metadata(tmp_path, relative):
    root = tmp_path / 'project'
    task_fixture(root, relative)
    with pytest.raises(ValueError, match='Unsafe public workspace'):
        downloader.stage_test_inputs(root)
    assert not (root / 'experiment/runtime').exists()


def test_staged_public_inputs_and_grader_link_are_idempotent(tmp_path):
    source = task_fixture(tmp_path)
    downloader.stage_test_inputs(tmp_path)
    downloader.stage_test_inputs(tmp_path)
    target = tmp_path / 'experiment/runtime/task_inputs/task'
    assert (target / 'exec/input.txt').read_text() == 'public'
    assert (target / 'gt').is_symlink()
    assert (target / 'gt').resolve() == (source / 'gt').resolve()


def test_restore_rejects_archive_traversal_and_wrong_hash(tmp_path):
    packed = tmp_path / 'inputs.tar.gz'
    records = archive(packed, '../outside.txt', b'unsafe')
    with pytest.raises(ValueError, match='Unexpected archive member'):
        downloader.restore(packed, tmp_path / 'project', records)
    records = archive(packed, 'safe.txt', b'dataset')
    records[0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='checksum mismatch'):
        downloader.restore(packed, tmp_path / 'project', records)
    assert not (tmp_path / 'project/safe.txt').exists()
    assert not list((tmp_path / 'project').glob('*.restore-part'))


def test_stage_rejects_exec_link_to_grader_material_even_inside_root(tmp_path):
    source = task_fixture(tmp_path)
    target = tmp_path / 'experiment/runtime/task_inputs/task'
    target.mkdir(parents=True)
    (target / 'exec').symlink_to(source / 'gt', target_is_directory=True)
    with pytest.raises(ValueError, match='Unexpected link'):
        downloader.stage_test_inputs(tmp_path)
    assert (source / 'gt/answer.txt').read_text() == 'grader only'
    assert not (source / 'gt/input.txt').exists()
