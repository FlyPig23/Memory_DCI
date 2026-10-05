"""Publication boundaries and redaction checks using synthetic fixtures only."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.download_data import _contained, restore
from experiment.benchmarks.terminal_bench_2_1.scripts.package_data import package, sanitize


class DatasetIOTests(unittest.TestCase):
    def test_credential_derivative_preserves_explicit_benchmark_fixture(self):
        fixture = b"hf_" + b"f" * 32
        unknown = b"hf_" + b"a" * 32
        result, counts = sanitize(fixture + b" " + unknown, set(), {fixture})
        self.assertIn(fixture, result)
        self.assertNotIn(unknown, result)
        self.assertEqual(counts, {"huggingface_token": 1})

    def test_complete_pem_redacted_and_placeholder_preserved(self):
        key = b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 64 + b"\n-----END PRIVATE KEY-----"
        placeholder = b"-----BEGIN PRIVATE KEY-----...-----END PRIVATE KEY-----"
        for value in (key, key.replace(b"\n", b"\\n")):
            result, counts = sanitize(value + b" " + placeholder, set())
            self.assertEqual(counts, {"private_key": 1})
            self.assertIn(placeholder, result)
            self.assertNotIn(b"A" * 64, result)

    def test_known_credential_and_json_assignment_redact_validly(self):
        known = b"local-session-" + b"x" * 30
        data = json.dumps({"service-key": "y" * 32, "note": known.decode()}).encode()
        result, counts = sanitize(data, {known})
        self.assertNotIn(known, result)
        self.assertEqual(counts, {"known_local_credential": 1, "credential_assignment": 1})
        self.assertIsInstance(json.loads(result), dict)

    def test_export_cannot_write_inside_source_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "outside the source"):
                package(root, root / "prepared")

    def test_archive_boundaries_checksums_and_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            name, payload = "experiment/benchmarks/terminal_bench_2_1/manifests/test.json", b"good"
            records = [{"path": name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]

            def archive(member, data=payload, kind=tarfile.REGTYPE):
                path = base / "test.tar.gz"
                with tarfile.open(path, "w:gz") as handle:
                    item = tarfile.TarInfo(member)
                    item.type = kind
                    item.size = len(data) if kind == tarfile.REGTYPE else 0
                    item.linkname = "../../outside" if kind == tarfile.SYMTYPE else ""
                    handle.addfile(item, io.BytesIO(data) if item.size else None)
                return path

            for path in ("../outside", "/absolute"):
                with self.assertRaises(ValueError):
                    restore(archive(path), root, records)
            with self.assertRaises(ValueError):
                restore(archive(name, kind=tarfile.SYMTYPE), root, records)
            with self.assertRaises(ValueError):
                restore(archive(name, b"evil"), root, records)
            restore(archive(name), root, records)
            (root / name).write_bytes(b"keep")
            with self.assertRaises(ValueError):
                restore(archive(name), root, records)
            self.assertEqual((root / name).read_bytes(), b"keep")

    def test_destination_ancestor_symlink_is_rejected_even_within_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "Unexpected link"):
                _contained(root / "link/file", root)


if __name__ == "__main__":
    unittest.main()
