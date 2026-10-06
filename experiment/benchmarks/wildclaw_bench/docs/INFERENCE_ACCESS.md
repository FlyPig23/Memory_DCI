# Private inference access

The inference gateway now requires a project-owned service credential. A client reaches it through the controlled proxy only while that client's random token is registered for an experiment run. The migration used port 18083; the controller confirmed the previous unauthenticated service on 18082 was stopped.

The access path is:

1. The controller enters `inference_client(project_root, run_id)`. It receives a fresh 32-byte random URL-safe `client.api_key` and `client.authorization_sha256`. The latter is SHA256 of the complete `Bearer <token>` header and is the value to persist as `gateway_authorization_hash`.
2. The private route document stores only that hash with the run ID, lease ID, and registration time. It preserves the existing `inference` destination and DCI routes. Normal and exceptional context exits remove only this lease. A controller killed without running cleanup must have its obsolete registrations removed before reuse; there is no automatic lease expiry.
3. The proxy accepts inference requests only when their complete Authorization header matches a registered client. It replaces that header with the private service credential and creates `X-Skill-DCI-Client-Authorization-SHA256` itself, discarding any client-supplied identity assertion.
4. The gateway validates its service credential before trusting that identity header. Chat-completion requests also require one valid client hash. Their manifests and events retain the original client hash, without storing the service credential or its hash. Direct requests without service authentication receive HTTP 401.

`ensure_service_key(project_root)` returns `experiment/benchmarks/wildclaw_bench/runtime/inference-private/service-key`, generating it once. The directory is mode 0700 and the file mode 0600. Reads check project containment, symbolic links, regular-file ownership, single-link status, size, and permissions. The key is passed to the gateway by filename through required `--api-key-file`; it is never placed in a solver environment or route JSON. Each solver/judge receives only its client token. TaskRuntime protects each run directory with mode 0700.

The registration API is:

```python
from experiment.benchmarks.wildclaw_bench.src.inference_access import inference_client

with inference_client(project_root, run_id) as client:
    record["gateway_authorization_hash"] = client.authorization_sha256
    # Supply client.api_key as this run's provider API key.
    # Keep solver and judge execution inside this context.
```

The optional `token=` argument supports controlled migration of an already running task. It was not needed for this migration because the controller waited until all solving and grading had stopped. Client objects omit the token from their representation. Concurrent route mutations share the existing controller lock and use private, unique atomic-write files.

Existing network restrictions remain in place. The original OAuth literal/URL/base64 export detection and sanitized blocked-audit URL are preserved, as are DCI registration and endpoint restrictions. This change adds neither model calls nor network/experiment budget changes.

Validation completed on 2026-09-08:

- The combined access, gateway, and egress test suite passed **30 tests and 13 subtests**. HTTP gateway tests use a mock inference runner. Coverage includes private-file validation, concurrent key creation, independent/revoked registrations, spoofed identity rejection, service-key replacement, audit identity, DCI compatibility, and OAuth leak protection.
- `experiment/benchmarks/wildclaw_bench/manifests/inference_access_smoke.json` records the actual proxy/gateway checks: unauthenticated direct GET 401; service-authenticated direct GET 200; unknown proxy client 403; registered proxy client 200; revoked client 403.
- A registered empty chat request returned HTTP 400 `missing_model` before any inference. Its two started/error events carried the correct client hash. Neither the service credential nor client token appeared in those audit records. The temporary smoke registration was removed.

Repeat the synthetic suite with:

```bash
experiment/benchmarks/wildclaw_bench/.venv/bin/python -B -m pytest \
  experiment/benchmarks/wildclaw_bench/tests/test_egress_policy.py \
  experiment/benchmarks/wildclaw_bench/tests/test_inference_access.py \
  experiment/benchmarks/wildclaw_bench/tests/test_codex_inference_gateway.py \
  -q -o cache_dir=experiment/benchmarks/wildclaw_bench/.cache/pytest-inference-access
```
