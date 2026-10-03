# Codex ChatGPT runtime adapter

Implementation: `src/task_runtime.py`, `src/codex_backend.py`. Reference runner:
`vendor/WildClawBench` at commit `316334ccc4a87b9b5635ad73da99b4dfc0b3887e`.
No data downloads, model calls, image installation, or daemon changes occur on
import or in the offline tests.

## Controller interface

```python
from pathlib import Path
from experiment.src.task_runtime import RuntimeConfig, TaskRuntime, load_task_spec
from experiment.src.codex_backend import CodexBackend, CodexSettings, MCPServer

task = load_task_spec(task_md, vendor_root, workspace_root=downloaded_workspace,
                      timeout_seconds=1800)
config = RuntimeConfig(
    project_root=project_root,
    image=prepared_image,
    docker_host=private_socket_or_none,
    network="none",
    support_mounts=approved_support_mounts,
    environment=task_and_proxy_environment,
    startup_commands=proxy_relay_and_readiness_commands,
)
runtime = TaskRuntime(config, task, private_run_directory, unique_run_id)
backend = CodexBackend(CodexSettings(mcp_servers=()))  # A0
# A3: use MCPServer(...), pointing ONLY to the DCI proxy, never to corpus files.
try:
    backend.prepare(runtime)
    # Controller provisions isolated ChatGPT auth into runtime.codex_home here.
    result = backend.run(runtime)
    evaluation = runtime.grade(
        transcript_path=result.transcript_path,
        transcript_loader=vendor_root / "src/utils/transcript_loader.py",
        grading_setup="",  # Only reviewed service restoration, if needed.
    )
finally:
    runtime.cleanup()
```

The default CLI path is `/opt/codex/codex`; supply a prepared image or a read-only
mount of the compatible CLI directory, including its code-mode companion, at `/opt/codex`. Version `0.153.4`, model
`gpt-6-astra`, and reasoning effort `ultra` are checked/configured explicitly.
`preflight` invokes only `--version` and `login status`; an actual solver call
happens exclusively in `backend.run`.

The caller may also supply a project-contained `RuntimeConfig.auth_file`.
It is mounted read-only and is never parsed by this adapter. A file bind may
prevent CLI credential refresh/atomic replacement. For reliable refresh, use
`auth_file=None`, call `prepare`, then have the controller provision an isolated
auth file into `runtime.codex_home/auth.json`. This directory is private to the
run and is already mounted; the CLI can update its own credentials normally.
Do not copy auth files into result bundles or print them. `codex-home/` contains
private runtime state, including auth if provisioned there; export only its
`sessions/` subtree when collecting shareable traces.

## Isolation and lifecycle

- Container names begin `hangxiao-skill-dci-`; all mutations verify the ownership
  label first. Every Docker command honors an optional `docker_host`.
- Only current task `exec`, optional `tmp`, declared skills, private scratch,
  approved support files, and isolated authentication enter the solver. The
  original task Markdown, `gt`, full project, trajectory archive, and corpus are
  not mounted. All host output paths must resolve inside the project.
- A0 has no DCI MCP server. A3 connects a small stdio proxy to a controller-owned
  evidence service so shell access cannot bypass the service's exposure budget.
- Root filesystem is read-only by default. Writable workspace, Codex home,
  OpenClaw compatibility home, and cache are private project binds; temporary
  runtime paths use tmpfs. Additional writable mounts must be inside this run.
- Networking defaults to `none`. A controller-provided Unix-socket HTTP relay
  may be started by `startup_commands`; these same commands restore the relay
  after verifier restart. Socket directories must contain only the controller's
  narrowly scoped proxy, never Docker or unrelated host control sockets.
- Codex native server-side web search is disabled by default because it bypasses
  container egress controls. Both groups retain the same browser/exec web tools.
- Social tasks pass a fixed-endpoint, read-only mock preflight after preparation
  and before model credentials are copied or Codex is started. It validates the
  required service set and `calls:list` contracts, records metadata only in
  `controller-staging/mock_preflight.json`, and blocks an unhealthy environment
  before any solver call. Empty call lists are valid. The final audit snapshot
  is still captured separately after solving; task warmup and solver budgets
  remain unchanged.
- Normal exit and timeout both stop the entire container. Grading is rejected
  unless that stop completed. Restarting launches only an inert `sleep`
  entrypoint; then gold and scoring code are introduced. Solver background
  processes cannot survive into the grading phase.
- Task warmup is not rerun for grading because doing so may reset task state.
  For in-memory mock services, `before_freeze_hook(runtime)` captures state;
  its return value is `runtime.freeze_hook_result`. After inert restart and relay
  readiness, `after_restart_hook(runtime)` restores a reviewed read-only replay
  before any gold is introduced. Capture failures still stop the entire solver;
  either hook failing produces `mock_state_error`, no score, and no grader call.
  Error details remain in private controller logs.
- `warmup_override` supports a controller-reviewed warmup after dependencies have
  been installed in the prepared image. Hashes of original and effective warmup
  are recorded. `user` can select the owner UID when not defined by the image.
- `prepare` makes its run directory mode 0700, including when another controller
  component created it first. Raw sessions and diagnostics remain private.

## Evaluation and records

The adapter calls the task's unchanged `grade(transcript=..., workspace_path=...)`
through the frozen official transcript loader. Codex response items are converted
to the OpenClaw-shaped message/tool records used by existing graders, including
modern custom-tool calls and structured image results. Reasoning summaries stay
`thinking` blocks and cannot count as user-visible warnings in safety graders.
Repeated `event_msg` text is excluded from conversion. Raw sessions remain
available for auditing this compatibility step.

Official `overall_score` wins; absent that field, numeric metrics are averaged
as in the frozen official summary. A missing/failed grader remains missing and
is never manufactured into a zero. Execution failure/timeout is separate from
grading status, so available partial deliverables can still be scored. Explicit
judge errors or degraded fallback modes retain `raw_overall_score` but exclude
`overall_score`; known development-task missing/invalid artifact errors preserve
legitimate zero scores, while unknown top-level errors require review. The
controller must also associate gateway failures with the grading interval;
see [INFERENCE_GATEWAY.md](INFERENCE_GATEWAY.md).

`execution.json`, `usage.json`, `evaluation.json`, `score.json` (when available),
converted transcript and private logs are written in the run directory. Token
usage uses the last cumulative session record, not a sum of cumulative records.
Unavailable counts and ChatGPT subscription dollar cost are `null`, not zero.
No claim of an exact API dollar cost is made for subscription execution.

ChatGPT solver authentication does not satisfy independent Brave, image
generation, or judge API dependencies. The root controller must validate those
dependencies and any provider changes before freezing the formal experiment.

## Validation

Run from the project root:

```bash
python3 -B -m unittest experiment.tests.test_codex_runtime -v
```

Tests use synthetic inputs and mocked Docker calls. Their temporary directories
are created and removed under `experiment/`. Passing these tests does not claim
that actual image warmup, OAuth inference, multimedia tools, or judge endpoints
have passed integration smoke tests.
