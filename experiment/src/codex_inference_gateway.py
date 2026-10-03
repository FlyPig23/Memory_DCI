"""Private OpenAI chat-completions facade backed by isolated Codex CLI runs.

This is an explicitly recorded provider substitution, not an implementation of
the OpenAI/OpenRouter sampling API. OAuth is consumed only by the supported CLI.
Unsupported operations fail; no scores or model responses are manufactured.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from .codex_backend import CodexBackend, CodexSettings
    from .inference_access import CLIENT_AUTHORIZATION_HEADER, authorization_sha256, read_service_key, valid_authorization_hash
    from .task_runtime import Mount, RuntimeConfig, TaskRuntime, TaskSpec, contained
except ImportError:
    from codex_backend import CodexBackend, CodexSettings
    from inference_access import CLIENT_AUTHORIZATION_HEADER, authorization_sha256, read_service_key, valid_authorization_hash
    from task_runtime import Mount, RuntimeConfig, TaskRuntime, TaskSpec, contained


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        self.recorded = False
        super().__init__(message)

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": "codex_gateway_error", "code": self.code}}


@dataclass(frozen=True)
class GatewayConfig:
    project_root: Path
    image: str
    auth_file: Path = field(repr=False)
    binary: Path
    gateway_dir: Path
    ca_bundle: Path
    bridge_script: Path
    port: int = 18082
    docker_host: str | None = None
    actual_model: str = "gpt-5.6-sol"
    reasoning_effort: str = "medium"
    cli_version: str = "0.153.4"
    timeout_seconds: int = 600
    max_concurrent: int = 1
    queue_timeout_seconds: int = 30
    max_body_bytes: int = 64 * 1024 * 1024
    max_image_bytes: int = 20 * 1024 * 1024
    max_images: int = 16
    remote_image_proxy: str | None = None
    api_key_file: Path | None = field(default=None, repr=False)
    inference_root: Path | None = None

    def __post_init__(self) -> None:
        root = Path(self.project_root).resolve()
        object.__setattr__(self, "project_root", root)
        for name in ("auth_file", "binary", "gateway_dir", "ca_bundle", "bridge_script", "api_key_file"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, contained(value, root))
        inference_root = self.inference_root or root / "experiment/runtime/inference"
        object.__setattr__(self, "inference_root", contained(inference_root, root / "experiment/runtime/inference"))
        if not (0 <= self.port <= 65535) or self.timeout_seconds <= 0 or self.max_concurrent <= 0:
            raise ValueError("Invalid port, timeout, or concurrency")
        if self.remote_image_proxy:
            proxy = urllib.parse.urlsplit(self.remote_image_proxy)
            if proxy.scheme != "http" or proxy.hostname != "127.0.0.1" or not proxy.port:
                raise ValueError("Remote images require a controller-owned loopback HTTP proxy")


@dataclass(frozen=True)
class PreparedRequest:
    request_id: str
    directory: Path
    requested_model: str
    prompt: str
    input_images: tuple[str, ...]
    image_manifest: tuple[Mapping[str, Any], ...]
    response_format: str
    stream: bool
    include_stream_usage: bool
    differences: tuple[str, ...]
    unapplied_parameters: Mapping[str, Any]


@dataclass(frozen=True)
class InferenceOutput:
    content: str
    usage: Mapping[str, Any]
    elapsed_seconds: float


def _image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise GatewayError(400, "unsupported_image", "Only PNG, JPEG, GIF, and WebP image bytes are supported")


def validate_remote_image_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise GatewayError(400, "unsafe_image_url", "Image URL must be public HTTP(S) without credentials")
    if parsed.port not in (None, 80, 443):
        raise GatewayError(400, "unsafe_image_url", "Image URL port must be 80 or 443")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
        raise GatewayError(400, "unsafe_image_url", "Private image destinations are prohibited")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # DNS and redirect destinations are also checked by the controller's
        # network policy; the gateway never downloads directly around it.
        return
    if not address.is_global:
        raise GatewayError(400, "unsafe_image_url", "Private image destinations are prohibited")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_remote_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url: str, config: GatewayConfig) -> bytes:
    validate_remote_image_url(url)
    if not config.remote_image_proxy:
        raise GatewayError(400, "remote_images_disabled", "Remote image URLs require an explicitly configured controlled proxy")
    context = ssl.create_default_context(cafile=str(config.ca_bundle))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": config.remote_image_proxy, "https": config.remote_image_proxy}),
        urllib.request.HTTPSHandler(context=context), _CheckedRedirect(),
    )
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "Skill-DCI-Inference/1"}), timeout=30) as response:
            data = response.read(config.max_image_bytes + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GatewayError(502, "image_download_failed", "Controlled image download failed") from error
    if len(data) > config.max_image_bytes:
        raise GatewayError(413, "image_too_large", "Image exceeds the configured byte limit")
    _image_type(data)
    return data


def _validate_parameters(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    known = {"model", "messages", "stream", "stream_options", "response_format", "n", "tools", "tool_choice",
             "parallel_tool_calls", "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed",
             "frequency_penalty", "presence_penalty", "user", "provider", "reasoning", "logprobs",
             "top_logprobs", "stop", "modalities", "audio", "transforms", "thinking"}
    if set(payload) - known:
        raise GatewayError(400, "unsupported_parameter", "Unsupported chat parameter(s): " + ", ".join(sorted(set(payload) - known)))
    if payload.get("n", 1) != 1:
        raise GatewayError(400, "unsupported_n", "Only n=1 is supported")
    if payload.get("tools") or payload.get("tool_choice") not in (None, "none"):
        raise GatewayError(400, "tools_unsupported", "This compatibility endpoint performs one inference without tools")
    if payload.get("logprobs") or payload.get("top_logprobs"):
        raise GatewayError(400, "logprobs_unsupported", "Codex CLI does not expose token log probabilities")
    if payload.get("audio") or payload.get("modalities", ["text"]) != ["text"]:
        raise GatewayError(400, "output_modality_unsupported", "Only text output is supported")
    if payload.get("stop"):
        raise GatewayError(400, "stop_unsupported", "API stop sequences are not implemented by the Codex CLI bridge")
    if "thinking" in payload and payload["thinking"] != {"type": "disabled"}:
        raise GatewayError(400, "thinking_unsupported", "Only thinking={type:disabled} is accepted as a recorded, unapplied provider control")
    fmt = payload.get("response_format") or {"type": "text"}
    if not isinstance(fmt, dict) or fmt.get("type") not in ("text", "json_object"):
        raise GatewayError(400, "response_format_unsupported", "Supported response formats are text and json_object")
    if set(fmt) != {"type"}:
        raise GatewayError(400, "response_format_unsupported", "Only a response_format type is accepted")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise GatewayError(400, "invalid_stream", "stream must be a boolean")
    sampling = {key: payload[key] for key in ("temperature", "top_p", "max_tokens", "max_completion_tokens", "seed",
                "frequency_penalty", "presence_penalty", "provider", "reasoning", "transforms", "thinking") if key in payload}
    differences = ["requested model is mapped to the configured Codex model",
                   "message roles and ordering are serialized into a single CLI inference prompt",
                   "Codex CLI developer context remains present; this is not native chat-completions inference",
                   "tool execution and native web search are disabled; an observed tool call fails the request"]
    if sampling:
        differences.append("requested sampling/provider/thinking controls are recorded but not applied; the configured Codex reasoning effort remains in effect")
    if payload.get("stream"):
        differences.append("SSE is buffered until the one CLI inference completes")
    return str(fmt["type"]), sampling, differences


def prepare_request(payload: Mapping[str, Any], directory: Path, request_id: str, config: GatewayConfig,
                    downloader: Callable[[str, GatewayConfig], bytes] = download_image) -> PreparedRequest:
    response_format, unapplied, differences = _validate_parameters(payload)
    model = payload.get("model")
    messages = payload.get("messages")
    if not isinstance(model, str) or not model:
        raise GatewayError(400, "missing_model", "model must be a nonempty string")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "invalid_messages", "messages must be a nonempty array")
    image_dir = directory / "input/exec/request_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    encoded: list[dict[str, Any]] = []
    images: list[str] = []
    image_manifest: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "assistant", "tool", "function"):
            raise GatewayError(400, "invalid_message_role", "Invalid chat message role")
        if set(message) - {"role", "content", "name", "tool_call_id", "tool_calls", "function_call"}:
            raise GatewayError(400, "unsupported_message_field", "Unsupported chat message field")
        content = message.get("content")
        normalized = {key: value for key, value in message.items() if key != "content"}
        if content is None and (message.get("tool_calls") or message.get("function_call")):
            normalized["content"] = None
        elif isinstance(content, str):
            normalized["content"] = content
        elif isinstance(content, list):
            blocks = []
            for block_index, block in enumerate(content):
                if not isinstance(block, dict):
                    raise GatewayError(400, "invalid_content", "Content blocks must be objects")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    blocks.append({"type": "text", "text": block["text"]})
                    continue
                if block.get("type") != "image_url":
                    raise GatewayError(400, "content_type_unsupported", "Only text and image_url content blocks are supported")
                if len(images) >= config.max_images:
                    raise GatewayError(400, "too_many_images", "Too many image attachments")
                image = block.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if not isinstance(url, str):
                    raise GatewayError(400, "invalid_image_url", "image_url requires a URL string")
                if url.startswith("data:"):
                    header, separator, body = url.partition(",")
                    if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
                        raise GatewayError(400, "invalid_data_uri", "Image data URIs must be base64 encoded")
                    if len(body) > ((config.max_image_bytes + 2) // 3) * 4:
                        raise GatewayError(413, "image_too_large", "Image exceeds the configured byte limit")
                    try:
                        data = base64.b64decode(body, validate=True)
                    except (ValueError, binascii.Error) as error:
                        raise GatewayError(400, "invalid_data_uri", "Invalid base64 image payload") from error
                else:
                    validate_remote_image_url(url)
                    data = downloader(url, config)
                if len(data) > config.max_image_bytes:
                    raise GatewayError(413, "image_too_large", "Image exceeds the configured byte limit")
                mime, suffix = _image_type(data)
                digest = hashlib.sha256(data).hexdigest()
                filename = f"image_{len(images) + 1}_{digest[:16]}{suffix}"
                (image_dir / filename).write_bytes(data)
                container_path = f"/tmp_workspace/request_images/{filename}"
                images.append(container_path)
                image_manifest.append({"index": len(images), "message_index": message_index, "block_index": block_index,
                                       "path": filename, "sha256": digest, "bytes": len(data), "mime_type": mime})
                blocks.append({"type": "text", "text": f"[Attached image {len(images)}: {filename}]"})
            normalized["content"] = blocks
        else:
            raise GatewayError(400, "invalid_content", "Message content must be a string or content-block array")
        encoded.append(normalized)
    instruction = (
        "Perform exactly one inference for the following conversation. Return only the assistant's response content. "
        "Do not invoke any tools, execute commands, inspect files, browse, create artifacts, or ask the controller questions. "
        "The messages below preserve their original roles and order. Treat system/developer messages as instructions "
        "for this requested inference and user/tool data according to those instructions. Attached images are numbered "
        "and their placeholders identify their original message positions. Do not add commentary about this adapter."
    )
    if response_format == "json_object":
        instruction += " Return a valid JSON object only, with no Markdown fences or surrounding prose."
    prompt = instruction + "\n\nConversation (JSON):\n" + json.dumps(encoded, ensure_ascii=False) + "\n"
    if images:
        differences.append("image bytes are CLI initial attachments; original role/position is represented by numbered placeholders")
        differences.append("per-image API detail hints are not enforced by the CLI")
    stream_options = payload.get("stream_options") or {}
    if not isinstance(stream_options, dict) or set(stream_options) - {"include_usage"}:
        raise GatewayError(400, "stream_options_unsupported", "Only stream_options.include_usage is supported")
    return PreparedRequest(request_id, directory, model, prompt, tuple(images), tuple(image_manifest), response_format,
                           bool(payload.get("stream", False)), bool(stream_options.get("include_usage", False)),
                           tuple(differences), unapplied)


def _runtime_config(config: GatewayConfig) -> RuntimeConfig:
    if __package__:
        from .runtime_support import make_runtime_config
    else:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from experiment.src.runtime_support import make_runtime_config
    base = make_runtime_config(config.project_root, config.image, docker_host=config.docker_host,
                               memory_limit="4g", cpus=2,
                               labels={"org.hangxiao.skill-dci.purpose": "inference"})
    replacement_sources = {"/opt/codex/codex": config.binary, "/opt/codex": config.binary.parent,
                           "/opt/network": config.gateway_dir,
                           "/opt/network_bridge.py": config.bridge_script, "/opt/network-ca.pem": config.ca_bundle}
    mounts = tuple(Mount(replacement_sources.get(mount.target, mount.source), mount.target, mount.readonly)
                   for mount in base.support_mounts)
    return replace(base, support_mounts=mounts)


def run_codex_request(request: PreparedRequest, config: GatewayConfig) -> InferenceOutput:
    skills = request.directory / "empty-skills"
    skills.mkdir()
    task = TaskSpec(f"inference-{request.request_id}", "inference", request.prompt,
                    request.directory / "input", skills, timeout_seconds=config.timeout_seconds)
    runtime = TaskRuntime(_runtime_config(config), task, request.directory / "run", f"inference-{request.request_id}")
    backend = CodexBackend(CodexSettings(model=config.actual_model, reasoning_effort=config.reasoning_effort,
                           cli_version=config.cli_version, binary=f"/opt/codex/{config.binary.name}", input_images=request.input_images,
                           no_tools=True, inference_only=True))
    isolated_auth: Path | None = None
    try:
        backend.prepare(runtime)
        isolated_auth = runtime.codex_home / "auth.json"
        # Copy opaque CLI credentials, never parse/print them or send them to an
        # HTTP provider. The configured source is a project-owned auth copy.
        shutil.copyfile(config.auth_file, isolated_auth)
        isolated_auth.chmod(0o600)
        result = backend.run(runtime)
        if result.status == "timeout":
            raise GatewayError(504, "inference_timeout", "The isolated Codex inference exceeded its fixed timeout")
        if result.status != "completed":
            raise GatewayError(502, "inference_failed", "The isolated Codex inference did not complete successfully")
        final = runtime.workspace / ".codex-last-message.txt"
        if not final.is_file():
            raise GatewayError(502, "missing_content", "Codex did not produce response content")
        content = final.read_text(encoding="utf-8")
        if not content.strip():
            raise GatewayError(502, "empty_content", "Codex returned empty response content")
        return InferenceOutput(content, result.usage, result.elapsed_seconds)
    finally:
        try:
            runtime.cleanup()
        finally:
            # Only our per-request credential copy is removed; never the source.
            if isolated_auth is not None:
                isolated_auth.unlink(missing_ok=True)


def openai_usage(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    input_tokens, output_tokens = raw.get("input_tokens"), raw.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    usage: dict[str, Any] = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                             "total_tokens": input_tokens + output_tokens}
    if isinstance(raw.get("cached_input_tokens"), int):
        usage["prompt_tokens_details"] = {"cached_tokens": raw["cached_input_tokens"]}
    if isinstance(raw.get("reasoning_output_tokens"), int):
        usage["completion_tokens_details"] = {"reasoning_tokens": raw["reasoning_output_tokens"]}
    return usage


class InferenceEngine:
    def __init__(self, config: GatewayConfig,
                 runner: Callable[[PreparedRequest, GatewayConfig], InferenceOutput] = run_codex_request,
                 downloader: Callable[[str, GatewayConfig], bytes] = download_image):
        self.config, self.runner, self.downloader = config, runner, downloader
        self.slots = threading.BoundedSemaphore(config.max_concurrent)
        self.audit_lock = threading.Lock()

    def _audit(self, event: Mapping[str, Any]) -> None:
        self.config.inference_root.mkdir(parents=True, exist_ok=True)
        path = self.config.inference_root / "events.jsonl"
        with self.audit_lock:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def complete(self, payload: Mapping[str, Any], *,
                 request_context: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], PreparedRequest]:
        if not self.slots.acquire(timeout=self.config.queue_timeout_seconds):
            raise GatewayError(429, "gateway_busy", "The private inference gateway is at its configured concurrency limit")
        request_id = uuid.uuid4().hex
        directory = self.config.inference_root / request_id
        context = dict(request_context or {})
        started_at = time.time()
        audit_base = {"request_id": request_id, "started_at_unix": started_at,
                      "requested_model": payload.get("model") if isinstance(payload.get("model"), str) else None,
                      "actual_model": self.config.actual_model, **context}
        try:
            directory.mkdir(parents=True, mode=0o700)
            request_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            digest = hashlib.sha256(request_bytes).hexdigest()
            audit_base["request_sha256"] = digest
            self._audit({**audit_base, "status": "started"})
            # Never mount this request log into a task solver. It can contain a
            # verifier's private rubric and belongs to the controller only.
            (directory / "request.json").write_bytes(request_bytes)
            request = prepare_request(payload, directory, request_id, self.config, self.downloader)
            manifest = {"request_id": request_id, "request_sha256": digest,
                        "requested_model": request.requested_model, "actual_model": self.config.actual_model,
                        "reasoning_effort": self.config.reasoning_effort, "cli_version": self.config.cli_version,
                        "timeout_seconds": self.config.timeout_seconds, "differences": request.differences,
                        "unapplied_parameters": request.unapplied_parameters, "images": request.image_manifest,
                        "response_format": request.response_format, "created_at_unix": time.time(),
                        "request_context": context}
            (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
            output = self.runner(request, self.config)
            if request.response_format == "json_object":
                try:
                    decoded = json.loads(output.content)
                except json.JSONDecodeError as error:
                    raise GatewayError(502, "invalid_json_content", "Codex did not return the requested JSON object") from error
                if not isinstance(decoded, dict):
                    raise GatewayError(502, "invalid_json_content", "Codex returned JSON that is not an object")
            response = {"id": f"chatcmpl-skilldci-{request_id}", "object": "chat.completion",
                        "created": int(time.time()), "model": self.config.actual_model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": output.content},
                                     "finish_reason": "stop"}],
                        "system_fingerprint": f"codex-cli-{self.config.cli_version}",
                        "x_skill_dci": {"requested_model": request.requested_model, "provider_substitution": True,
                                        "sampling_parameters_enforced": False, "buffered_stream": request.stream}}
            usage = openai_usage(output.usage)
            if usage is not None:
                response["usage"] = usage
            (directory / "response.json").write_text(json.dumps(response, ensure_ascii=False, indent=2))
            (directory / "status.json").write_text(json.dumps({"status": "completed", "elapsed_seconds": output.elapsed_seconds}))
            self._audit({**audit_base, "status": "completed", "http_status": 200, "finished_at_unix": time.time()})
            return response, request
        except GatewayError as error:
            if directory.is_dir():
                (directory / "status.json").write_text(json.dumps({"status": "error", "code": error.code}))
            self._audit({**audit_base, "status": "error", "http_status": error.status,
                         "error_code": error.code, "finished_at_unix": time.time()})
            error.recorded = True
            raise
        except Exception as error:
            if directory.is_dir():
                (directory / "status.json").write_text(json.dumps({"status": "error", "exception_type": type(error).__name__}))
            self._audit({**audit_base, "status": "error", "http_status": 502,
                         "error_code": "gateway_execution_error", "finished_at_unix": time.time()})
            wrapped = GatewayError(502, "gateway_execution_error", "Private inference execution failed; consult the controller's request log")
            wrapped.recorded = True
            raise wrapped from error
        finally:
            self.slots.release()


def sse_response(response: Mapping[str, Any], *, include_usage: bool = False) -> bytes:
    common = {key: response[key] for key in ("id", "created", "model")}
    common["object"] = "chat.completion.chunk"
    frames = [
        {**common, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {"content": response["choices"][0]["message"]["content"]}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    if include_usage and "usage" in response:
        frames.append({**common, "choices": [], "usage": response["usage"]})
    return ("".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames) + "data: [DONE]\n\n").encode()


class GatewayHandler(BaseHTTPRequestHandler):
    server: "GatewayServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log URLs, headers, request bodies, auth, or rubric contents.
        return

    def _authorized(self) -> bool:
        key = self.server.api_key
        values = self.headers.get_all("Authorization", [])
        return key is not None and len(values) == 1 and hmac.compare_digest(values[0].encode(), ("Bearer " + key).encode())

    def _client_identity(self) -> str | None:
        # Only the authenticated controller proxy may assert the original client
        # identity. Never replace it with a hash of the private service key.
        if not self._authorized():
            return None
        values = self.headers.get_all(CLIENT_AUTHORIZATION_HEADER, [])
        if len(values) != 1 or not valid_authorization_hash(values[0]):
            return None
        return values[0]

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Skill-DCI-Provider", "codex-cli-chatgpt-compatibility")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, status: int, body: Any) -> None:
        self._send(status, json.dumps(body, ensure_ascii=False).encode())

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(401, GatewayError(401, "unauthorized", "Invalid private gateway credential").body())
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": self.server.engine.config.actual_model,
                        "object": "model", "created": 0, "owned_by": "skill-dci-codex-bridge"}]})
        else:
            self._json(404, GatewayError(404, "unsupported_endpoint", "Only /v1/models and /v1/chat/completions are supported").body())

    def do_POST(self) -> None:
        client_identity = None
        try:
            if not self._authorized():
                client_identity = authorization_sha256(self.headers.get("Authorization", ""))
                raise GatewayError(401, "unauthorized", "Invalid private gateway credential")
            client_identity = self._client_identity()
            if client_identity is None:
                raise GatewayError(400, "invalid_client_identity", "Authenticated proxy must supply one valid client identity")
            if self.path != "/v1/chat/completions":
                raise GatewayError(404, "unsupported_endpoint", "This endpoint is not supported; image/audio generation is unavailable")
            if self.headers.get("Transfer-Encoding"):
                raise GatewayError(400, "transfer_encoding_unsupported", "Send JSON with Content-Length")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError as error:
                raise GatewayError(400, "invalid_content_length", "Invalid Content-Length") from error
            if length < 0:
                raise GatewayError(411, "content_length_required", "Content-Length is required")
            if length > self.server.engine.config.max_body_bytes:
                raise GatewayError(413, "request_too_large", "Request exceeds the configured byte limit")
            self.connection.settimeout(30)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise GatewayError(400, "incomplete_request", "Request body ended early")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise GatewayError(400, "invalid_json", "Request body must be JSON") from error
            if not isinstance(payload, dict):
                raise GatewayError(400, "invalid_request", "Request body must be an object")
            response, request = self.server.engine.complete(payload, request_context={"authorization_sha256": client_identity})
            if request.stream:
                self._send(200, sse_response(response, include_usage=request.include_stream_usage), "text/event-stream")
            else:
                self._json(200, response)
        except GatewayError as error:
            if not error.recorded:
                self.server.engine._audit({
                    "request_id": uuid.uuid4().hex, "status": "error", "http_status": error.status,
                    "error_code": error.code, "started_at_unix": time.time(), "finished_at_unix": time.time(),
                    "authorization_sha256": client_identity,
                    "actual_model": self.server.engine.config.actual_model,
                })
            self._json(error.status, error.body())
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: GatewayConfig, engine: InferenceEngine | None = None):
        if config.api_key_file is None:
            raise ValueError("Private inference gateway requires an api_key_file")
        self.api_key = read_service_key(config.api_key_file, config.project_root)
        self.engine = engine or InferenceEngine(config)
        super().__init__(("127.0.0.1", config.port), GatewayHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--image")
    parser.add_argument("--image-manifest", type=Path)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--gateway-dir", type=Path)
    parser.add_argument("--ca-bundle", type=Path, required=True)
    parser.add_argument("--bridge-script", type=Path)
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--docker-host")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--remote-image-proxy")
    parser.add_argument("--api-key-file", type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    image = args.image
    if not image:
        manifest = contained(args.image_manifest or root / "experiment/manifests/runtime_image.json", root)
        image_data = json.loads(manifest.read_text())
        image = image_data.get("id") or image_data["tag"]
    config = GatewayConfig(
        project_root=root, image=image, auth_file=args.auth_file,
        binary=args.binary or root / "experiment/runtime/bin/codex",
        gateway_dir=args.gateway_dir or root / "experiment/runtime/gateways/shared",
        ca_bundle=args.ca_bundle, bridge_script=args.bridge_script or root / "experiment/src/network_bridge.py",
        port=args.port, docker_host=args.docker_host, actual_model=args.model,
        reasoning_effort=args.reasoning_effort, timeout_seconds=args.timeout,
        max_concurrent=args.max_concurrent, remote_image_proxy=args.remote_image_proxy,
        api_key_file=args.api_key_file,
    )
    server = GatewayServer(config)
    print(json.dumps({"status": "listening", "host": "127.0.0.1", "port": server.server_port,
                      "actual_model": config.actual_model, "reasoning_effort": config.reasoning_effort}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
