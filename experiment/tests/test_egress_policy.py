from pathlib import Path
import base64
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from mitmproxy import http
from experiment.src import egress_policy


class CredentialEgressTests(unittest.TestCase):
    def test_only_actual_login_material_is_blocked_and_not_audited(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temp:
            root = Path(temp)
            auth = root / "experiment/runtime/codex_auth/auth.json"
            auth.parent.mkdir(parents=True)
            value = "synthetic-login-token-for-transport-test-only"
            auth.write_text(json.dumps({"tokens": {"access_token": value}}))
            with patch.object(egress_policy, "ROOT", root):
                for content in (value.encode(), base64.b64encode(value.encode())):
                    flow = SimpleNamespace(request=http.Request.make("POST", "https://example.com", content))
                    self.assertTrue(egress_policy.contains_real_credential(flow))
                ordinary = SimpleNamespace(request=http.Request.make("POST", "https://example.com", b"benchmark-dummy-password"))
                self.assertFalse(egress_policy.contains_real_credential(ordinary))


class InferenceRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="inference-egress-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.scratch.name)
        self.routes = self.root / "experiment/runtime/proxy_routes.json"
        self.audit = self.root / "experiment/logs/egress.jsonl"
        self.audit.parent.mkdir(parents=True)
        self.patches = [patch.object(egress_policy, "ROOT", self.root),
                        patch.object(egress_policy, "ROUTES", self.routes),
                        patch.object(egress_policy, "AUDIT", self.audit)]
        for active in self.patches:
            active.start()
        self.requests = []
        requests = self.requests
        class StubClient:
            def __init__(self, **kwargs):
                assert kwargs["trust_env"] is False
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def request(self, method, url, **kwargs):
                requests.append({"method": method, "url": url, **kwargs})
                return SimpleNamespace(status_code=200, content=b'{"synthetic":true}',
                                       headers={"content-type": "application/json"})
        self.transport = patch.object(egress_policy.httpx, "AsyncClient", StubClient)
        self.transport.start()

    def tearDown(self):
        self.transport.stop()
        for active in reversed(self.patches):
            active.stop()
        self.scratch.cleanup()

    def write_routes(self, document):
        self.routes.parent.mkdir(parents=True, exist_ok=True)
        self.routes.write_text(json.dumps(document))
        self.routes.chmod(0o600)

    def flow(self, authorization=None, *, url="http://api.hangxiao.internal/v1/chat/completions", headers=None):
        extra = dict(headers or {})
        if authorization is not None:
            extra["Authorization"] = authorization
        return SimpleNamespace(request=http.Request.make("POST", url, b'{"synthetic":true}', extra), response=None)

    async def test_missing_or_unregistered_client_is_denied_without_transport(self):
        self.write_routes({"inference": "http://127.0.0.1:18082"})
        for authorization in (None, "Bearer arbitrary-local-user", "Bearer " + "x" * 43):
            flow = self.flow(authorization)
            await egress_policy.Policy().request(flow)
            self.assertEqual(flow.response.status_code, 403)
        self.assertEqual(self.requests, [])

    async def test_registered_client_gets_service_key_and_trusted_original_hash(self):
        from experiment.src.inference_access import (
            CLIENT_AUTHORIZATION_HEADER, inference_client, read_service_key, service_key_path)
        self.write_routes({"inference": "http://127.0.0.1:18082"})
        with inference_client(self.root, "synthetic-run") as client:
            key = read_service_key(service_key_path(self.root), self.root)
            for url in ("http://api.hangxiao.internal/v1/chat/completions",
                        "https://openrouter.ai/api/v1/chat/completions"):
                flow = self.flow("Bearer " + client.api_key, url=url,
                                 headers={CLIENT_AUTHORIZATION_HEADER: "f" * 64})
                await egress_policy.Policy().request(flow)
                self.assertEqual(flow.response.status_code, 200)
                forwarded = self.requests[-1]
                self.assertEqual(forwarded["url"], "http://127.0.0.1:18082/v1/chat/completions")
                self.assertEqual(forwarded["headers"]["Authorization"], "Bearer " + key)
                self.assertEqual(forwarded["headers"][CLIENT_AUTHORIZATION_HEADER], client.authorization_sha256)
                self.assertNotIn(client.api_key, repr(forwarded))
            raw = self.audit.read_text()
            self.assertNotIn(client.api_key, raw)
            self.assertNotIn(key, raw)
        revoked = self.flow("Bearer " + client.api_key)
        await egress_policy.Policy().request(revoked)
        self.assertEqual(revoked.response.status_code, 403)
        self.assertEqual(len(self.requests), 2)

    async def test_missing_service_key_or_invalid_private_registry_fails_closed(self):
        from experiment.src.inference_access import inference_client, service_key_path
        self.write_routes({"inference": "http://127.0.0.1:18082"})
        with inference_client(self.root, "synthetic-run") as client:
            service_key_path(self.root).unlink()
            flow = self.flow("Bearer " + client.api_key)
            await egress_policy.Policy().request(flow)
            self.assertEqual(flow.response.status_code, 403)
            self.assertEqual(self.requests, [])
        self.routes.chmod(0o644)
        flow = self.flow("Bearer " + "x" * 43)
        await egress_policy.Policy().request(flow)
        self.assertEqual(flow.response.status_code, 403)
        self.assertEqual(self.requests, [])

    async def test_dci_registration_and_endpoint_restriction_are_preserved(self):
        self.write_routes({"dci": {"synthetic-dci-token": "http://127.0.0.1:12345"}})
        flow = self.flow("Bearer synthetic-dci-token", url="http://dci.hangxiao.internal/rpc")
        await egress_policy.Policy().request(flow)
        self.assertEqual(flow.response.status_code, 200)
        self.assertEqual(self.requests[-1]["headers"]["Authorization"], "Bearer synthetic-dci-token")
        invalid = self.flow("Bearer synthetic-dci-token", url="http://dci.hangxiao.internal/v1/anything")
        await egress_policy.Policy().request(invalid)
        self.assertEqual(invalid.response.status_code, 403)
        self.assertEqual(len(self.requests), 1)

    async def test_actual_oauth_export_still_blocks_without_url_or_body_leak(self):
        from urllib.parse import quote
        auth = self.root / "experiment/runtime/codex_auth/auth.json"
        auth.parent.mkdir(parents=True)
        value = "synthetic-actual-oauth-must-never-reach-the-audit"
        auth.write_text(json.dumps({"tokens": {"access_token": value}}))
        for body in (value.encode(), base64.b64encode(value.encode())):
            flow = self.flow(url="https://example.com/export/" + quote(value, safe=""))
            flow.request.content = body
            await egress_policy.Policy().request(flow)
            self.assertEqual(flow.response.status_code, 403)
        self.assertEqual(self.requests, [])
        raw = self.audit.read_text()
        self.assertNotIn(value, raw)
        self.assertNotIn(base64.b64encode(value.encode()).decode(), raw)
        self.assertIn("credential-export-blocked.invalid", raw)


if __name__ == "__main__":
    unittest.main()
