# Private Codex inference compatibility gateway

`src/codex_inference_gateway.py` adapts text/image chat-completion requests to
the supported Codex CLI using an isolated copy of the existing ChatGPT login.
It does not send an OAuth token to the OpenRouter or OpenAI HTTP API.

The controller starts the service and routes the approved OpenRouter chat path
through the project egress proxy. The service listens only on `127.0.0.1` and
supports `GET /v1/models` and `POST /v1/chat/completions`.

```bash
python3 -B -m experiment.benchmarks.wildclaw_bench.src.codex_inference_gateway \
  --image hangxiao-skill-dci-runtime-v2 \
  --auth-file experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json \
  --ca-bundle experiment/benchmarks/wildclaw_bench/runtime/network-ca.pem \
  --remote-image-proxy http://127.0.0.1:18081 \
  --api-key-file experiment/benchmarks/wildclaw_bench/runtime/inference-private/service-key \
  --port 18083
```

Use the controller's actual prepared image name. `--docker-host` selects a
dedicated daemon socket when supplied; no daemon installation or configuration
occurs here. CLI, relay, socket-directory and CA paths can also be supplied
explicitly. `runtime_support.make_runtime_config` provides the common mounts,
proxy environment, and relay startup. `--api-key-file` is required and uses the
private service key created by `inference_access.ensure_service_key`. It is
independent of ChatGPT model authentication. The gateway validates file ownership,
mode 0600, and regular-file/single-link constraints before listening.

Each request gets a fresh `experiment/benchmarks/wildclaw_bench/runtime/inference/<request-id>/`
directory with mode 0700 and an owned disposable container. Only that request's
prompt and input images enter its workspace. It has no benchmark trajectory
library, task inputs, other runs, or grader files mounted. Its own copied auth
is removed in cleanup. Container networking remains `none`; permitted requests
use the controller-owned Unix-socket proxy. The gateway never changes a rubric.

## Explicit compatibility differences

- The actual model is `gpt-6-astra`, effort `medium`, with a fixed 600-second
  inference timeout by default, regardless of the requested OpenRouter model.
  Both requested and actual model identifiers are recorded per request.
- Message roles and order are encoded into the prompt. They are not native API
  role boundaries, and the Codex CLI's own developer context remains present.
  The prompt requests one answer without tools. Shell, browser, native search,
  MCP, apps, plugins, memories, image generation and related tool features are
  disabled for inference. An observed tool call fails the request.
- PNG, JPEG, GIF and WebP inputs support data URIs, or public HTTP(S) image URLs
  downloaded through the explicitly configured controller proxy. Up to 16 input
  images are attached byte-for-byte using `codex --image`. Invalid images,
  forbidden URLs and failed downloads return errors instead of silently dropping
  images. Image paths and hashes are recorded in the private request manifest.
- Text output is returned unchanged. `response_format=json_object` adds a JSON
  instruction and validates the completed answer; invalid JSON returns an error
  without repair. No JSON format is imposed on ordinary text/float requests.
- Accepted sampling/budget fields that the CLI cannot enforce, including
  temperature and `max_tokens`, are listed as unapplied in the manifest.
  Tools, token log-probabilities, stop sequences, multiple choices, unsupported
  message types and unsupported endpoints return explicit errors.
- `stream=true` returns valid **buffered** SSE after inference completes, ending
  in `[DONE]`; this is not incremental token streaming. Usage is derived from
  available CLI token records, and missing usage remains unavailable. ChatGPT
  subscription dollar cost is not inferred.

This bridge is a documented provider adaptation, not reproduction of the
original OpenRouter judge model. Brave search, image generation, audio and
other third-party API requirements are not satisfied by this endpoint.

## Grading failure correlation

`experiment/benchmarks/wildclaw_bench/runtime/inference/events.jsonl` is a private mode-0600 audit index.
Each accepted request writes `started` followed by `completed` or `error`;
rejected HTTP requests also write an error event. Events include `request_id`,
`authorization_sha256`, `request_sha256`, `requested_model`, `actual_model`,
timestamps, `http_status`, and `error_code` when applicable. No request prompt,
rubric, or raw authorization value appears in the shared index.

For each benchmark run or grading retry the controller opens an
`inference_access.inference_client` context. Its random client API key is passed
to that run, while only `client.authorization_sha256` is recorded. The private
proxy checks the registered client, authenticates to the gateway using the
separate service key, and passes the original client hash in a trusted header.
The gateway accepts that header only after service authentication. The context
removes the client lease on exit. Check the recorded client hash and grading
interval for errors and incomplete requests before accepting scores.
Some frozen graders swallow API failures and can otherwise emit a numeric zero.
Keep their raw result for audit and mark the evaluation unavailable/degraded
when inference failed; do not treat that zero as demonstrated task failure.

Private per-request files hold the original JSON body, conversion manifest,
CLI transcript/logs and response/status. They must not be exposed to a solver.
Inference exceptions return bounded messages; diagnostic details remain local.

## Offline validation

The gateway, lifecycle and compatibility tests use mocks and synthetic requests.
They do not make model calls. The separately audited transcript compatibility
tests inspect only selected public development-task helpers.

```bash
python3 -B -m pytest -q \
  experiment/benchmarks/wildclaw_bench/tests/test_codex_runtime.py \
  experiment/benchmarks/wildclaw_bench/tests/test_codex_inference_gateway.py \
  experiment/benchmarks/wildclaw_bench/tests/test_runtime_hooks.py \
  experiment/benchmarks/wildclaw_bench/tests/test_grading_compatibility.py \
  --basetemp experiment/benchmarks/wildclaw_bench/.cache/tmp/backend-compatibility-tests \
  -o cache_dir=experiment/benchmarks/wildclaw_bench/.cache/pytest
```
