# Project service lifecycle

`scripts/service_control.py` plans, starts, checks and stops only the three named
project services: `proxy`, `bridge`, and `gateway`. It does not adopt processes
discovered by name or port. Existing services started elsewhere remain unmanaged
until the controller explicitly records their previously verified ownership
evidence. This implementation has not operated on the existing live services.

```bash
python3 -B experiment/scripts/service_control.py plan
python3 -B experiment/scripts/service_control.py status
python3 -B experiment/scripts/service_control.py start
python3 -B experiment/scripts/service_control.py stop
```

`--service proxy|bridge|gateway|all` selects the target. All-service startup uses
dependency order; shutdown reverses it. Bridge/gateway startup requires ready,
recorded dependencies. `--gateway-port` defaults to 18083, `--proxy-port` to 18081.
`--image` and `--docker-host` configure the inference runtime. `plan` and `status`
do not create state directories or launch processes.

Startup uses the project's `.venv/bin/python` with explicit argv, private logs,
and project-local temporary/cache paths. Gateway startup calls
`ensure_service_key` and passes only the key-file path via required
`--api-key-file`. The actual key never appears in argv, registry output or logs.
The gateway's fixed inference configuration is `gpt-6-astra`, `medium`, 600 s.

The proxy keeps `ssl_insecure=false`. Its `--ignore-hosts` list tunnels the
listed native ChatGPT endpoints without MITM interception. Codex still verifies
the official server certificate; this does not disable HTTPS verification.
Other permitted HTTP(S) requests use the fixed project egress addon.

This script does not rewrite inference routes or client leases. After a planned
service migration, the root controller selects the verified gateway URL in the
private route registry and registers each run using `inference_client`. Both
changes must match the chosen gateway port before task scheduling resumes.

## Ownership registry and stopping

The private registry is `experiment/runtime/services/state.json`, mode 0600,
inside a mode-0700 directory. Its schema is:

```json
{
  "schema": "skill-dci-services-v1",
  "project_root": "/absolute/project/root",
  "owner_uid": 123,
  "boot_id": "Linux boot UUID",
  "services": {
    "proxy": {
      "pid": 456,
      "uid": 123,
      "start_ticks": 789,
      "argv": ["/absolute/python", "-B", "/absolute/mitmdump", "..."],
      "cwd": "/absolute/project/root",
      "boot_id": "Linux boot UUID",
      "port": 18081,
      "unix_socket": null,
      "status": "running"
    }
  }
}
```

The controller must use actual `/proc` start ticks and the complete exact argv,
never the abbreviated example. Startup automatically adds launch IDs, log paths,
timestamps and, for the bridge, the socket file's device/inode/UID identity.
The Python API exposes `ServiceController`, `default_specs`, and read-only
`process_identity`; there is intentionally no discovery/adoption command.

Before signalling, the registry's project/user/boot identity and the live
process's UID, PID start time, exact argv and cwd must match. Signals use Linux
pidfds to prevent PID reuse between verification and signalling. A changed
identity, unknown registry entry or previous boot causes refusal. The script
never signals a process group or searches for targets using process names.

Normal stop sends SIGTERM to the recorded process. Only explicit `--force`
permits SIGKILL after its grace period, with the same identity checks. Stop
should occur after the run scheduler has drained active work; lifecycle control
manages service processes, not their experiment containers.

An occupied port or existing unowned socket blocks startup without replacement.
Readiness additionally verifies the listening socket's kernel inode belongs to
the recorded PID. A stopped bridge socket is removed only when its stored
filesystem identity still matches, its UID is correct, and it has no live
listener. Logs and registry history remain available after stopping.

The tests mock process creation, signals and listener ownership; they exercise
PID reuse, argv/UID changes, unknown ports and sockets, private file permissions,
targeted stopping and dependency checks without touching any actual service.

```bash
python3 -B -m pytest -q experiment/tests/test_service_control.py \
  --basetemp experiment/.cache/tmp/service-control-tests \
  -o cache_dir=experiment/.cache/pytest
```
