"""V5 task-scoped memory runtime and solver/grader resource boundary.

The controller supplies the immutable build pool and a fresh memory directory
inside this run. No memory is discovered from, or promoted to, another run.
The original task tools remain available; one V5 MCP server adds the explicit
search and memory-decision actions. All three V5 resource mounts are removed
before hidden grading material enters the container.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import tomllib

from experiment.src.experiment_protocol import atomic_json, sha256
from experiment.src.task_runtime import RuntimeErrorWithStage, TaskRuntime


RESOURCE_MODES = {"/pool": True, "/memory": False, "/opt/v5": True}
REQUIRED_MODES = {**RESOURCE_MODES, "/usr/local/bin/rg": True}


def _mount_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in value.split(","):
        key, _, value = part.partition("=")
        if key in fields:
            raise ValueError("Duplicate Docker mount field")
        fields[key] = value
    return fields


def grading_create_args(original: tuple[str, ...]) -> tuple[str, ...]:
    """Keep captured solver configuration except the three V5 resources.

    Reconstruct from the original Docker command, never from a second config
    renderer whose defaults might differ from those used by the solver.
    """
    if not original or original[0] != "run":
        raise ValueError("Expected captured Docker run arguments")
    output = ["create"]
    removed: set[str] = set()
    index = 1
    while index < len(original):
        item = original[index]
        if item == "--detach":
            index += 1
            continue
        if item == "--mount":
            if index + 1 == len(original):
                raise ValueError("Missing mount argument")
            mount = original[index + 1]
            fields = _mount_fields(mount)
            destination = fields.get("dst")
            if destination in RESOURCE_MODES:
                if destination in removed:
                    raise ValueError("Duplicate resource mount")
                if fields.get("type") != "bind" or not fields.get("src"):
                    raise ValueError("Expected a source-bound resource mount")
                if ("readonly" in fields) != RESOURCE_MODES[destination]:
                    raise ValueError("Unexpected resource mount access mode")
                if "readonly" in fields and fields["readonly"]:
                    raise ValueError("Ambiguous read-only mount flag")
                removed.add(destination)
            else:
                output += [item, mount]
            index += 2
            continue
        output.append(item)
        index += 1
    if removed != set(RESOURCE_MODES):
        raise ValueError("Expected exactly one pool, memory and V5 code mount")
    return tuple(output)


class MemoryTaskRuntime(TaskRuntime):
    def __init__(self, *args, pool_manifest_sha256: str,
                 memory_initial_sha256: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._solver_run_args: tuple[str, ...] | None = None
        self.native_environment = {
            "schema_version": 1,
            "condition": "DCI_TASK_LOCAL_MEMORY",
            "run_id": self.run_dir.name,
            "solver": {
                "pool_manifest_sha256": pool_manifest_sha256,
                "memory_initial_sha256": memory_initial_sha256,
                "pool_verified": False,
                "memory_verified": False,
            },
            "grader": None,
        }

    def docker(self, *args: str, **kwargs):
        if args and args[0] == "run":
            if self._solver_run_args is not None:
                raise ValueError("Solver container may only be created once")
            grading_create_args(args)
            self._solver_run_args = args
        return super().docker(*args, **kwargs)

    def _inspect(self) -> dict:
        return json.loads(self.docker("inspect", self.name).stdout)[0]

    def _save_native_environment(self) -> None:
        atomic_json(self.run_dir / "native_environment.json", self.native_environment)

    def _validate_resource_configuration(self, parsed: dict) -> dict:
        servers = parsed.get("mcp_servers", {})
        if set(servers) != {"v5"} or not isinstance(servers["v5"], dict) or not servers["v5"].get("command"):
            raise ValueError("V5 requires exactly one nonempty MCP server named v5")
        features = parsed.get("features", {})
        if features.get("memories") is not False:
            raise ValueError("Global Codex memories must be explicitly disabled")
        if any(features.get(name) is False for name in ("shell_tool", "unified_exec")):
            raise ValueError("V5 must retain native Codex shell tools")
        expected = {}
        for mount in self.config.support_mounts:
            destination = str(PurePosixPath(mount.target))
            if destination in expected:
                raise ValueError("Duplicate support mount destination")
            if any(destination == root or destination.startswith(root + "/")
                   for root in ("/corpus", "/skills", "/memories", "/root/.codex/memories", "/root/.agents")):
                raise ValueError("Legacy resources or global memory must not be mounted")
            # A nested bind could silently shadow files inside the approved tree.
            if any(destination.startswith(root + "/") for root in RESOURCE_MODES):
                raise ValueError("Nested V5 resource mounts are prohibited")
            expected[destination] = mount
        for destination, readonly in REQUIRED_MODES.items():
            mount = expected.get(destination)
            if mount is None or mount.readonly is not readonly:
                raise ValueError(f"Required mount is absent or has wrong access mode: {destination}")
        memory = expected["/memory"].source.resolve()
        if memory == self.run_dir or not memory.is_relative_to(self.run_dir):
            raise ValueError("Memory must be a private subdirectory of this run")
        for other in (self.workspace, self.codex_home, self.staging):
            if memory.is_relative_to(other) or other.is_relative_to(memory):
                raise ValueError("Memory must be separate from workspace and controller state")
        pool, code = (expected[name].source.resolve() for name in ("/pool", "/opt/v5"))
        if any(memory.is_relative_to(other) or other.is_relative_to(memory) for other in (pool, code)):
            raise ValueError("Memory cannot overlap immutable resource trees")
        return expected

    def prepare(self, config_text: str) -> None:
        parsed = tomllib.loads(config_text)
        expected = self._validate_resource_configuration(parsed)
        super().prepare(config_text)
        info = self._inspect()
        mounts = {item["Destination"]: item for item in info["Mounts"]}
        if len(mounts) != len(info["Mounts"]):
            raise ValueError("Duplicate effective Docker mount destination")
        records = {}
        for destination, readonly in REQUIRED_MODES.items():
            actual = mounts.get(destination)
            source = expected[destination].source.resolve()
            if (not actual or actual["Type"] != "bind" or actual["RW"] is readonly
                    or Path(actual["Source"]).resolve() != source):
                raise ValueError(f"Solver mount is absent, wrong source or wrong access: {destination}")
            records[destination] = {
                "destination": destination, "read_only": readonly,
                "source": str(source.relative_to(self.config.project_root.resolve())), "type": "bind",
            }
        # Fresh per-run Codex home is the only host location at this path. No
        # inherited memory catalog is allowed even if a caller changed an image.
        if (self.codex_home / "memories").exists():
            raise ValueError("Unexpected inherited Codex memory in fresh solver home")
        server = parsed["mcp_servers"]["v5"]
        self.native_environment["solver"].update({
            "container_id": info["Id"], "mount_verified": True,
            "pool_verified": True, "memory_verified": True, "code_verified": True,
            "pool_mount": records["/pool"], "memory_mount": records["/memory"],
            "code_mount": records["/opt/v5"], "rg_mount": records["/usr/local/bin/rg"],
            "memory_task_scoped": True, "memory_shared_between_tasks": False,
            "global_codex_memories_enabled": False, "native_shell_tools_retained": True,
            "mcp_servers": ["v5"],
            "v5_mcp": {"command": server["command"], "args": server.get("args", []),
                       "environment_keys": sorted(server.get("env", {}))},
            "config_sha256": sha256(self.codex_home / "config.toml"),
        })
        self._save_native_environment()

    def detach_resources_for_grading(self) -> None:
        if self.state != "solver_stopped" or not self._solver_run_args:
            raise RuntimeErrorWithStage(self.state, "Stop solver before detaching resources")
        if self.native_environment["grader"] is not None:
            raise ValueError("Resources were already detached")
        self._assert_owned()
        before = self._inspect()
        if before["State"]["Running"]:
            raise ValueError("Solver is still running")
        self.docker("rm", self.name)
        self.created = False
        self.docker(*grading_create_args(self._solver_run_args))
        self.created = True
        after = self._inspect()
        if after["State"]["Running"] or any(item["Destination"] in RESOURCE_MODES for item in after["Mounts"]):
            raise ValueError("Grader container unexpectedly running or exposes V5 resources")
        sort_mounts = lambda rows: sorted(rows, key=lambda item: item["Destination"])
        expected_mounts = sort_mounts([item for item in before["Mounts"] if item["Destination"] not in RESOURCE_MODES])
        actual_mounts = sort_mounts(after["Mounts"])
        old_config, new_config = dict(before["Config"]), dict(after["Config"])
        for config in (old_config, new_config):
            config.pop("Hostname", None)
            environment = config.get("Env", [])
            if len({entry.split("=", 1)[0] for entry in environment}) != len(environment):
                raise ValueError("Duplicate environment names prevent exact comparison")
            config["Env"] = sorted(environment)
        for name in ("AttachStdout", "AttachStderr"):
            if old_config.get(name) is not False or new_config.get(name) is not True:
                raise ValueError("Unexpected inert entrypoint attachment flags")
            old_config.pop(name)
            new_config.pop(name)
        if actual_mounts != expected_mounts or old_config != new_config:
            raise ValueError("Non-resource grading container configuration changed")
        old_host, new_host = dict(before["HostConfig"]), dict(after["HostConfig"])
        old_host["Mounts"] = sorted([item for item in old_host.get("Mounts", [])
                                      if item["Target"] not in RESOURCE_MODES], key=lambda item: item["Target"])
        new_host["Mounts"] = sorted(new_host.get("Mounts", []), key=lambda item: item["Target"])
        if old_host.get("OomKillDisable") is None and new_host.get("OomKillDisable") is False:
            old_host["OomKillDisable"] = False
        if old_host != new_host:
            raise ValueError("Non-resource grading host configuration changed")
        self.native_environment["grader"] = {
            "resources_absent": True, "pool_absent": True, "memory_absent": True, "v5_code_absent": True,
            "mounts_verified": True, "recreated_without_resources": True,
            "same_nonresource_configuration": True, "solver_stopped_before_recreation": True,
            "container_id_before": before["Id"], "container_id_after": after["Id"],
            "warmup_repeated": False,
            "permitted_container_metadata_changes": ["Hostname", "AttachStdout", "AttachStderr"],
        }
        self._save_native_environment()

    def grade(self, **kwargs):
        self.detach_resources_for_grading()
        return TaskRuntime.grade(self, **kwargs)
