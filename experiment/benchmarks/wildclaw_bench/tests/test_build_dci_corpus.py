"""Synthetic import fixtures exercise sealed test data, source linkage and images."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_dci_corpus.py"
SPEC = importlib.util.spec_from_file_location("build_dci_corpus", SCRIPT)
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH1cAAAAASUVORK5CYII=")


def trace(task="01_A_task_1_build", run="run1", model="Model A"):
    return b"\n".join(json.dumps(row).encode() for row in [
        {"type": "session", "task_id": task, "run_id": run, "model": model, "trace_status": "completed"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "public task"}]}},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "exec", "arguments": {"cmd": "read input"}}]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "c1", "content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}]}},
        {"type": "message", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "failed task solution, but execution ended cleanly"}]}},
    ])


def test_normalization_preserves_actions_and_decodes_deduplicated_images(tmp_path):
    result = corpus.normalize_trace(trace(), tmp_path)
    assert "ACTION exec" in result["body"]
    assert "role=toolResult tool_call_id=c1" in result["body"]
    assert "base64" not in result["body"]
    assert len(result["images"]) == 1
    assert (tmp_path / result["images"][0]["path"]).read_bytes() == PNG
    assert result["images"][0]["sha256"] == corpus.digest(PNG)
    assert result["trace_status"] == "completed"


def test_sanitizer_removes_secrets_and_linked_evaluator_output(tmp_path):
    secret = "sk-proj-" + "a" * 35
    rows = [
        {"role": "assistant", "content": [{"type": "toolCall", "id": "eval1", "name": "exec", "arguments": {"cmd": "cat /tmp_workspace/gt/answer.json"}}]},
        {"role": "toolResult", "toolCallId": "eval1", "content": [{"type": "text", "text": "hidden answer without identifying path"}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"key={secret} OPENROUTER_API_KEY=realCredentialValue123 PASSWORD='sensitive123'"}]},
    ]
    data = b"\n".join(json.dumps(row).encode() for row in rows)
    result = corpus.normalize_trace(data, tmp_path)
    assert "hidden answer" not in result["body"]
    assert "realCredentialValue123" not in result["body"]
    assert "sensitive123" not in result["body"]
    assert secret not in result["body"]
    assert result["sanitization"]["evaluator_actions"] == 1
    assert result["sanitization"]["evaluator_tool_results"] == 1


def test_missing_tool_result_is_interrupted_without_claiming_failure(tmp_path):
    data = json.dumps({"role": "assistant", "content": [{"type": "toolCall", "id": "c", "name": "read", "arguments": {}}]}).encode()
    result = corpus.normalize_trace(data, tmp_path)
    assert result["trace_status"] == "interrupted"
    assert "success" not in result


@pytest.mark.parametrize("name", ["../../escape", "/root/escape", "safe/../escape"])
def test_archive_paths_cannot_escape(name):
    with pytest.raises(ValueError, match="Unsafe"):
        corpus.safe_member_identity(name, {"task"})


def test_evaluator_free_numeric_outcomes_are_not_binarized():
    score = corpus.numeric_score(json.dumps({"overall_score": 0.25, "metric": 0, "explanation": "private grading narrative"}).encode())
    assert score == {"overall_score": 0.25, "metric": 0}


def fixture_snapshot(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    pa = pytest.importorskip("pyarrow")
    root = tmp_path / "snapshot"
    root.mkdir()
    build, test = "01_A_task_1_build", "01_A_task_2_sealed"
    session = root / f"sessions/model_a/{build}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_bytes(trace(build))
    (session.parent / f"{test}.jsonl").write_bytes(b"INVALID_JSON_SEALED_TEST_SESSION\xff")
    for alias, kind, model in (("model_a", "tar", "Model A"), ("model_b", "zip", "Model B")):
        members = {
            f"output/{build}/run1/chat.jsonl": trace(build, model=model),
            f"output/{build}/run1/score.json": b'{"overall_score": 0.0, "notes": "BUILD_GRADER_NARRATIVE_NOT_FOR_SOLVER"}',
            f"output/{test}/run1/chat.jsonl": b"INVALID_JSON_SEALED_TEST_CHAT\xff",
            f"output/{test}/run1/score.json": b"INVALID_JSON_SEALED_TEST_SCORE\xff",
            f"output/{test}/run1/task_output/secret.txt": b"SEALED_TEST_ARTIFACT",
        }
        if kind == "tar":
            with tarfile.open(root / f"output_{alias}.tar.gz", "w:gz") as archive:
                for name, content in members.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        else:
            with zipfile.ZipFile(root / f"output_{alias}.zip", "w") as archive:
                for name, content in members.items():
                    archive.writestr(name, content)
    rows = [{"task_id": task, "model_name": model, "trajectory": "SEALED_PARQUET_TRAJECTORY_MUST_NOT_BE_PARSED"}
            for task in (build, test) for model in ("Model A", "Model B")]
    pq.write_table(pa.Table.from_pylist(rows), root / "train.parquet")
    siblings = [{"rfilename": path.relative_to(root).as_posix(), "size": path.stat().st_size,
                 "lfs": {"sha256": corpus.hash_file(path)}} for path in sorted(root.rglob("*")) if path.is_file()]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"sha": corpus.REVISION, "siblings": siblings}))
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"build_task_ids": [build], "test_task_ids": [test], "dev_corpus_task_ids": [build], "dev_excluded_family_task_ids": []}))
    return root, inventory, split


def test_union_import_links_formats_and_never_decodes_sealed_content(tmp_path):
    snapshot, inventory, split = fixture_snapshot(tmp_path)
    output = tmp_path / "corpus"
    summary = corpus.build(snapshot, inventory, split, output)
    assert summary["build_episode_count"] == 2
    assert summary["build_episodes_with_official_metrics"] == 2
    assert summary["build_images"] == 1
    manifest = json.loads((output / "frozen_build/manifest.json").read_text())
    assert len(manifest["episodes"]) == 2
    for view in ("frozen_build", "dev"):
        assert all(path.stat().st_nlink == 1 for path in (output / view).rglob("*") if path.is_file())
    episode = next(e for e in manifest["episodes"] if e["model_alias"] == "model_a")
    assert len(episode["sources"]) == 2
    assert episode["outcome"]["metrics"]["overall_score"] == 0.0
    assert episode["outcome"]["success_label"] is None
    assert all(e["task_id"] == "01_A_task_1_build" for e in manifest["episodes"])
    for view in ("frozen_build", "dev"):
        for path in (output / view).rglob("*"):
            if path.is_file() and path.suffix != ".png":
                content = path.read_text()
                assert "INVALID_JSON_SEALED" not in content
                assert "SEALED_PARQUET_TRAJECTORY" not in content
                assert "BUILD_GRADER_NARRATIVE" not in content
    coverage = json.loads((output / "coverage.json").read_text())
    assert len(coverage["cells"]) == 4
    sealed = [row for row in coverage["cells"] if row["partition"] == "sealed_test"]
    assert all(row["canonical_episode_count"] is None for row in sealed)


def test_incomplete_download_is_rejected_before_any_corpus_is_written(tmp_path):
    snapshot, inventory, split = fixture_snapshot(tmp_path)
    (snapshot / "train.parquet").write_bytes(b"partial download")
    output = tmp_path / "corpus"
    with pytest.raises(ValueError, match="missing/incomplete"):
        corpus.build(snapshot, inventory, split, output)
    assert not output.exists()
