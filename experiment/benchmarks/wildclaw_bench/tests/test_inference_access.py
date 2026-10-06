"""Synthetic private-key and run-registration lifecycle checks; no inference."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from experiment.benchmarks.wildclaw_bench.src.inference_access import (
    authorization_sha256, client_token, ensure_service_key, inference_client,
    load_routes, read_service_key, registered_client, service_key_path,
)


class InferenceAccessTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="inference-access-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.scratch.name)
        self.routes = self.root / "experiment/benchmarks/wildclaw_bench/runtime/proxy_routes.json"

    def tearDown(self):
        self.scratch.cleanup()

    def test_stable_service_key_is_private_and_not_saved_in_routes(self):
        path = ensure_service_key(self.root)
        key = read_service_key(path, self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ensure_service_key(self.root), path)
        self.assertEqual(read_service_key(path, self.root), key)
        with inference_client(self.root, "synthetic-run") as client:
            raw = self.routes.read_text()
            self.assertNotIn(key, raw)
            self.assertNotIn(client.api_key, raw)
            self.assertNotIn(client.api_key, repr(client))
            self.assertEqual(client.authorization_sha256, hashlib.sha256(("Bearer " + client.api_key).encode()).hexdigest())
            self.assertEqual(self.routes.stat().st_mode & 0o777, 0o600)

    def test_context_isolates_clients_and_removes_only_its_lease_on_exception(self):
        with inference_client(self.root, "run-first") as first:
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with inference_client(self.root, "run-second") as second:
                    routes = load_routes(self.routes, self.root)
                    self.assertEqual(registered_client(routes, "Bearer " + first.api_key), first.authorization_sha256)
                    self.assertEqual(registered_client(routes, "Bearer " + second.api_key), second.authorization_sha256)
                    self.assertIsNone(registered_client(routes, "Bearer " + "x" * 43))
                    raise RuntimeError("synthetic failure")
            routes = load_routes(self.routes, self.root)
            self.assertEqual(len(routes["inference_clients"]), 1)
            self.assertIn(first.authorization_sha256, routes["inference_clients"])
        self.assertEqual(load_routes(self.routes, self.root)["inference_clients"], {})

    def test_migration_token_can_be_registered_without_leaking_or_duplicate_lease(self):
        token = "skill-dci-run-dev-migration-synthetic"
        with inference_client(self.root, "legacy-synthetic", token=token) as client:
            with self.assertRaises(ValueError):
                with inference_client(self.root, "duplicate", token=token):
                    self.fail("Duplicate registration was accepted")
            self.assertEqual(client.api_key, token)
            self.assertNotIn(token, self.routes.read_text())
        self.assertEqual(load_routes(self.routes, self.root)["inference_clients"], {})

    def test_concurrent_service_key_creation_preserves_one_key(self):
        def create(_):
            path = ensure_service_key(self.root)
            return read_service_key(path, self.root)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(len(set(pool.map(create, range(8)))), 1)

    def test_other_routes_survive_registration_and_removal(self):
        self.routes.parent.mkdir(parents=True)
        original = {"inference": "http://127.0.0.1:18082", "dci": {"synthetic-dci": "http://127.0.0.1:99"}}
        self.routes.write_text(json.dumps(original))
        self.routes.chmod(0o600)
        with inference_client(self.root, "run"):
            pass
        result = json.loads(self.routes.read_text())
        self.assertEqual(result["dci"], original["dci"])
        self.assertEqual(result["inference"], original["inference"])
        self.assertEqual(list(self.routes.parent.glob(".inference-routes-*.tmp")), [])

    def test_loose_mode_symlink_hardlink_and_outside_secret_are_rejected(self):
        path = ensure_service_key(self.root)
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            read_service_key(path, self.root)
        path.chmod(0o600)
        link = path.with_name("link")
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            read_service_key(link, self.root)
        link.unlink()
        os.link(path, link)
        with self.assertRaises(PermissionError):
            read_service_key(path, self.root)
        link.unlink()
        with self.assertRaises(ValueError):
            read_service_key(self.root.parent / "outside", self.root)

    def test_client_headers_and_registry_must_have_expected_shape(self):
        for value in ("", "x" * 43, "Basic " + "x" * 43, "Bearer tiny", "Bearer " + "x" * 43 + "\n"):
            self.assertIsNone(client_token(value))
        authorization = "Bearer " + "x" * 43
        for clients in ([], None, {authorization_sha256(authorization): True}, {authorization_sha256(authorization): {"run_id": "x"}}):
            self.assertIsNone(registered_client({"inference_clients": clients}, authorization))
        with self.assertRaises(ValueError):
            with inference_client(self.root, "../outside"):
                pass


if __name__ == "__main__":
    unittest.main()
