"""Synthetic gateway checks; local HTTP uses a mock runner, no Docker or LLM."""
from __future__ import annotations

import base64
import json
import hashlib
import http.client
import threading
from contextlib import contextmanager
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from experiment.src.codex_backend import CodexSettings, build_exec_argv, render_config
from experiment.src.codex_inference_gateway import (
    GatewayConfig, GatewayError, GatewayServer, InferenceEngine, InferenceOutput, download_image,
    openai_usage, prepare_request, sse_response, validate_remote_image_url,
)


from experiment.src.inference_access import CLIENT_AUTHORIZATION_HEADER, ensure_service_key, read_service_key


# Minimal image payload used only to test transport and byte preservation.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV5kAAAAASUVORK5CYII=")


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="gateway-test-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.scratch.name)
        self.config = GatewayConfig(self.root, "image-id", self.root / "auth.json", self.root / "codex",
                                    self.root / "gateway", self.root / "ca.pem", self.root / "bridge.py",
                                    queue_timeout_seconds=0)
        self.payload = {"model": "openai/requested-judge", "messages": [
            {"role": "system", "content": "Synthetic rubric: answer with its requested structure."},
            {"role": "user", "content": "Synthetic candidate"},
        ]}

    def tearDown(self):
        self.scratch.cleanup()

    def engine(self, content="synthetic answer"):
        runner = Mock(return_value=InferenceOutput(content, {"input_tokens": 15, "output_tokens": 7}, 0.01))
        return InferenceEngine(self.config, runner=runner), runner

    def prepare(self, payload=None, **kwargs):
        directory = self.root / "request"
        directory.mkdir()
        return prepare_request(payload or self.payload, directory, "request1", self.config, **kwargs)

    def test_role_contents_are_preserved_and_api_model_substitution_is_explicit(self):
        self.config = replace(self.config, actual_model="gpt-6-astra")
        engine, runner = self.engine()
        response, request = engine.complete({**self.payload, "temperature": 0, "max_tokens": 123})
        encoded = json.loads(request.prompt.split("Conversation (JSON):\n", 1)[1])
        self.assertEqual(encoded, self.payload["messages"])
        self.assertEqual(response["model"], "gpt-6-astra")
        self.assertEqual(response["x_skill_dci"]["requested_model"], self.payload["model"])
        self.assertFalse(response["x_skill_dci"]["sampling_parameters_enforced"])
        manifest = json.loads((request.directory / "manifest.json").read_text())
        self.assertEqual(manifest["unapplied_parameters"], {"temperature": 0, "max_tokens": 123})
        self.assertEqual(len(manifest["request_sha256"]), 64)
        runner.assert_called_once()

    def test_safety_judge_thinking_disabled_is_explicitly_recorded_not_applied(self):
        engine, runner = self.engine('{"synthetic_score":0.5}')
        payload = self.payload | {"temperature": 0, "max_tokens": 1000,
                                  "response_format": {"type": "json_object"},
                                  "thinking": {"type": "disabled"}}
        response, request = engine.complete(payload)
        manifest = json.loads((request.directory / "manifest.json").read_text())
        self.assertEqual(manifest["unapplied_parameters"], {
            "temperature": 0, "max_tokens": 1000, "thinking": {"type": "disabled"}})
        self.assertIn("configured Codex reasoning effort remains in effect", " ".join(request.differences))
        self.assertEqual(engine.config.reasoning_effort, "medium")
        self.assertFalse(response["x_skill_dci"]["sampling_parameters_enforced"])
        runner.assert_called_once()

    def test_other_thinking_contracts_are_rejected_before_model_call(self):
        for value in (None, False, "disabled", {}, {"type": "enabled"},
                      {"type": "disabled", "budget_tokens": 0}):
            with self.subTest(thinking=value):
                engine, runner = self.engine()
                with self.assertRaises(GatewayError) as error:
                    engine.complete(self.payload | {"thinking": value})
                self.assertEqual(error.exception.code, "thinking_unsupported")
                runner.assert_not_called()

    def test_requests_have_independent_directories_and_no_memory(self):
        engine, _ = self.engine()
        _, first = engine.complete(self.payload)
        _, second = engine.complete(self.payload)
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(list((first.directory / "input/exec/request_images").iterdir()), [])
        self.assertFalse((first.directory / "input/exec/request.json").exists())
        self.assertTrue((first.directory / "request.json").is_file())

    def test_image_data_uri_preserves_bytes_and_original_position(self):
        uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
        payload = {**self.payload, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Inspect this image"},
            {"type": "image_url", "image_url": {"url": uri, "detail": "high"}},
        ]}]}
        request = self.prepare(payload)
        self.assertEqual(len(request.input_images), 1)
        manifest = request.image_manifest[0]
        self.assertEqual(manifest["message_index"], 0)
        self.assertEqual(manifest["block_index"], 1)
        self.assertEqual((request.directory / "input/exec/request_images" / manifest["path"]).read_bytes(), PNG)
        self.assertIn("[Attached image 1:", request.prompt)
        self.assertNotIn("base64,", request.prompt)

    def test_remote_images_use_the_injected_controlled_downloader(self):
        downloader = Mock(return_value=PNG)
        url = "https://example.org/figure.png"
        payload = {**self.payload, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
        ]}]}
        request = self.prepare(payload, downloader=downloader)
        downloader.assert_called_once_with(url, self.config)
        self.assertEqual(len(request.input_images), 1)

    def test_private_image_locations_and_direct_download_are_rejected(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/a", "http://169.254.169.254/a",
                    "https://localhost/a", "https://user:pass@example.org/a", "https://example.org:8443/a"):
            with self.subTest(url=url), self.assertRaises(GatewayError):
                validate_remote_image_url(url)
        with self.assertRaises(GatewayError) as error:
            download_image("https://example.org/image.png", self.config)
        self.assertEqual(error.exception.code, "remote_images_disabled")

    def test_unsupported_endpoint_inputs_do_not_call_a_model(self):
        changes = [{"n": 2}, {"tools": [{"type": "function"}]}, {"logprobs": True},
                   {"stop": ["END"]}, {"response_format": {"type": "json_schema"}},
                   {"modalities": ["image"]}, {"not_a_supported_parameter": 1}]
        for change in changes:
            with self.subTest(change=change):
                engine, runner = self.engine()
                with self.assertRaises(GatewayError):
                    engine.complete(self.payload | change)
                runner.assert_not_called()

    def test_json_object_is_validated_without_repair_or_fabrication(self):
        payload = self.payload | {"response_format": {"type": "json_object"}}
        engine, runner = self.engine('{"synthetic_score":0.5}')
        response, _ = engine.complete(payload)
        self.assertEqual(response["choices"][0]["message"]["content"], '{"synthetic_score":0.5}')
        runner.assert_called_once()
        for content in ("not JSON", "[1, 2]", "```json\n{}\n```"):
            engine, runner = self.engine(content)
            with self.assertRaises(GatewayError) as error:
                engine.complete(payload)
            self.assertEqual(error.exception.code, "invalid_json_content")
            runner.assert_called_once()

    def test_buffered_sse_has_content_finish_and_done_frames(self):
        engine, _ = self.engine()
        response, request = engine.complete(self.payload | {"stream": True, "stream_options": {"include_usage": True}})
        body = sse_response(response, include_usage=request.include_stream_usage).decode()
        frames = body.strip().split("\n\n")
        self.assertEqual(frames[-1], "data: [DONE]")
        parsed = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
        self.assertEqual(parsed[1]["choices"][0]["delta"]["content"], "synthetic answer")
        self.assertEqual(parsed[2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(parsed[3]["usage"]["total_tokens"], 22)

    def test_unknown_token_usage_is_omitted_not_zero(self):
        self.assertIsNone(openai_usage({}))
        self.assertIsNone(openai_usage({"input_tokens": None, "output_tokens": None}))
        self.assertEqual(openai_usage({"input_tokens": 10, "output_tokens": 5}),
                         {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    def test_busy_requests_do_not_start_model_calls(self):
        engine, runner = self.engine()
        engine.slots.acquire()
        try:
            with self.assertRaises(GatewayError) as error:
                engine.complete(self.payload)
            self.assertEqual(error.exception.status, 429)
            runner.assert_not_called()
        finally:
            engine.slots.release()

    def test_model_failures_propagate_without_response(self):
        runner = Mock(side_effect=GatewayError(504, "inference_timeout", "synthetic timeout"))
        engine = InferenceEngine(self.config, runner=runner)
        with self.assertRaises(GatewayError) as error:
            engine.complete(self.payload)
        self.assertEqual(error.exception.status, 504)
        request_dir = next(path for path in self.config.inference_root.iterdir() if path.is_dir())
        self.assertFalse((request_dir / "response.json").exists())
        self.assertEqual(json.loads((request_dir / "status.json").read_text())["code"], "inference_timeout")

    def test_audit_index_links_failure_to_authorization_hash_without_rubric(self):
        runner = Mock(side_effect=GatewayError(504, "inference_timeout", "synthetic timeout"))
        engine = InferenceEngine(self.config, runner=runner)
        auth_hash = hashlib.sha256(b"Bearer skill-dci-run-toy").hexdigest()
        with self.assertRaises(GatewayError):
            engine.complete(self.payload, request_context={"authorization_sha256": auth_hash})
        raw = (self.config.inference_root / "events.jsonl").read_text()
        events = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([event["status"] for event in events], ["started", "error"])
        self.assertEqual(events[-1]["http_status"], 504)
        self.assertEqual(events[-1]["authorization_sha256"], auth_hash)
        self.assertNotIn("Synthetic rubric", raw)
        self.assertNotIn("Bearer skill-dci-run-toy", raw)


    @contextmanager
    def http_gateway(self):
        path = ensure_service_key(self.root)
        config = replace(self.config, port=0, api_key_file=path)
        engine, runner = self.engine()
        server = GatewayServer(config, engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, runner, read_service_key(path, self.root)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def http_request(self, server, headers, *, method="POST", path="/v1/chat/completions", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            data = json.dumps(self.payload) if body is None else body
            connection.request(method, path, body=data, headers={"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_gateway_refuses_to_listen_without_a_private_service_key(self):
        with self.assertRaises(ValueError):
            GatewayServer(replace(self.config, port=0))
        path = ensure_service_key(self.root)
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            GatewayServer(replace(self.config, port=0, api_key_file=path))

    def test_http_rejects_direct_clients_and_spoofed_identity_before_inference(self):
        with self.http_gateway() as (server, runner, key):
            forged_identity = "b" * 64
            for authorization in (None, "Bearer " + "x" * 43, "Basic " + key):
                headers = {CLIENT_AUTHORIZATION_HEADER: forged_identity}
                if authorization is not None:
                    headers["Authorization"] = authorization
                status, response = self.http_request(server, headers)
                self.assertEqual(status, 401)
                self.assertEqual(response["error"]["code"], "unauthorized")
            runner.assert_not_called()
        raw = (self.config.inference_root / "events.jsonl").read_text()
        self.assertNotIn(forged_identity, raw)
        self.assertNotIn(key, raw)

    def test_authenticated_proxy_preserves_client_hash_in_manifest_and_events(self):
        original = "Bearer " + "y" * 43
        identity = hashlib.sha256(original.encode()).hexdigest()
        with self.http_gateway() as (server, runner, key):
            status, response = self.http_request(server, {
                "Authorization": "Bearer " + key, CLIENT_AUTHORIZATION_HEADER: identity})
            self.assertEqual(status, 200)
            runner.assert_called_once()
        raw = (self.config.inference_root / "events.jsonl").read_text()
        self.assertEqual({row["authorization_sha256"] for row in map(json.loads, raw.splitlines())}, {identity})
        directory = next(path for path in self.config.inference_root.iterdir() if path.is_dir())
        manifest = (directory / "manifest.json").read_text()
        self.assertIn(identity, manifest)
        self.assertNotIn(key, raw + manifest)
        self.assertNotIn(hashlib.sha256(("Bearer " + key).encode()).hexdigest(), raw + manifest)
        self.assertNotIn(original, raw + manifest)

    def test_service_key_without_valid_client_identity_cannot_invoke_inference(self):
        with self.http_gateway() as (server, runner, key):
            for identity in (None, "not-a-hash", "b" * 63, "B" * 64):
                headers = {"Authorization": "Bearer " + key}
                if identity is not None:
                    headers[CLIENT_AUTHORIZATION_HEADER] = identity
                status, response = self.http_request(server, headers)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_client_identity")
            runner.assert_not_called()
            status, _ = self.http_request(server, {"Authorization": "Bearer " + key}, method="GET", path="/v1/models")
            self.assertEqual(status, 200)
        raw = (self.config.inference_root / "events.jsonl").read_text()
        self.assertNotIn(key, raw)
        self.assertNotIn(hashlib.sha256(("Bearer " + key).encode()).hexdigest(), raw)

    def test_inference_config_disables_tools_and_attaches_images(self):
        settings = CodexSettings(no_tools=True, inference_only=True,
                                 input_images=("/tmp_workspace/request_images/image_1.png",))
        config = tomllib.loads(render_config(settings))
        self.assertFalse(config["features"]["shell_tool"])
        self.assertFalse(config["features"]["image_generation"])
        self.assertFalse(config["features"]["apps"])
        self.assertFalse(config["features"]["plugins"])
        self.assertFalse(config["features"]["memories"])
        self.assertEqual(config["sandbox_mode"], "read-only")
        argv = build_exec_argv(settings)
        self.assertIn("--image", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        solver = tomllib.loads(render_config(CodexSettings()))
        self.assertTrue(solver["features"]["image_generation"])


if __name__ == "__main__":
    unittest.main()
