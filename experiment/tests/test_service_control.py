"""Mocked service lifecycle checks; never start/stop an actual project service."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiment.scripts import service_control as module


class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="service-control-test-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.scratch.name)
        self.specs = module.default_specs(self.root)
        self.controller = module.ServiceController(self.root, self.specs)
        self.boot = patch.object(module, "boot_id", return_value="synthetic-boot")
        self.boot.start()
        self.identity = {"pid": 987654321, "uid": os.geteuid(), "start_ticks": 12345,
                         "argv": list(self.specs["proxy"].argv), "cwd": str(self.root), "process_state": "S"}

    def tearDown(self):
        self.boot.stop()
        self.scratch.cleanup()

    def record(self, name="proxy", **changes):
        self.controller._private_directory(self.controller.directory)
        state = self.controller._load()
        item = {key: value for key, value in self.identity.items() if key != "process_state"}
        spec = self.specs[name]
        item.update(argv=list(spec.argv), boot_id="synthetic-boot", port=spec.port,
                    unix_socket=str(spec.unix_socket) if spec.unix_socket else None)
        item.update(changes)
        state["services"][name] = item
        self.controller._save(state)
        return item

    def test_default_commands_keep_tls_validation_and_private_gateway_key(self):
        proxy = self.specs["proxy"].argv
        self.assertIn("ssl_insecure=false", proxy)
        self.assertIn("--quiet", proxy)
        self.assertIn("--ignore-hosts", proxy)
        self.assertIn("--api-key-file", self.specs["gateway"].argv)
        self.assertIn("18083", self.specs["gateway"].argv)
        self.assertEqual(self.specs["gateway"].argv[self.specs["gateway"].argv.index("--reasoning-effort") + 1], "medium")

    def test_unmanaged_status_is_read_only_and_never_adopts(self):
        with patch.object(module, "process_identity") as inspect:
            self.assertEqual(self.controller.status("proxy")["status"], "unmanaged")
            inspect.assert_not_called()
        self.assertFalse(self.controller.directory.exists())

    def test_unknown_port_owner_blocks_launch_without_signal(self):
        with patch.object(module, "endpoint_available", return_value=False), \
             patch.object(module.subprocess, "Popen") as launch, patch.object(module, "signal_recorded") as kill:
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                self.controller.start("proxy")
            launch.assert_not_called()
            kill.assert_not_called()
        self.assertFalse(self.controller.state_path.exists())

    def test_unknown_socket_is_not_deleted(self):
        path = self.specs["bridge"].unix_socket
        path.parent.mkdir(parents=True)
        path.write_text("synthetic preexisting object")
        with patch.object(self.controller, "status", return_value={"status": "running", "configuration_matches": True}), \
             patch.object(module.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "unowned"):
                self.controller.start("bridge")
            launch.assert_not_called()
        self.assertEqual(path.read_text(), "synthetic preexisting object")

    def test_pid_reuse_argv_change_and_other_uid_never_receive_signals(self):
        self.record()
        for current in (
            dict(self.identity, start_ticks=99999),
            dict(self.identity, argv=["other-process"]),
            {"pid": self.identity["pid"], "uid": os.geteuid() + 1, "foreign_uid": True},
        ):
            with self.subTest(current=current), patch.object(module, "process_identity", return_value=current), \
                 patch.object(module, "signal_recorded") as kill:
                self.assertEqual(self.controller.status("proxy")["status"], "identity_mismatch")
                with self.assertRaisesRegex(RuntimeError, "no process was signalled"):
                    self.controller.stop("proxy")
                kill.assert_not_called()

    def test_unrecorded_stop_is_rejected(self):
        with patch.object(module, "signal_recorded") as kill:
            with self.assertRaisesRegex(RuntimeError, "no recorded owner"):
                self.controller.stop("proxy")
            kill.assert_not_called()

    def test_targeted_stop_records_exit_and_retains_log_identity(self):
        record = self.record()
        current = [self.identity]

        def terminate(row, sig):
            self.assertEqual(row["pid"], record["pid"])
            self.assertEqual(sig, signal.SIGTERM)
            current[0] = None

        with patch.object(module, "process_identity", side_effect=lambda _: current[0]), \
             patch.object(module, "owns_listener", return_value=True), \
             patch.object(module, "signal_recorded", side_effect=terminate) as kill:
            result = self.controller.stop("proxy")
        self.assertEqual(result["status"], "stopped")
        kill.assert_called_once()
        stored = json.loads(self.controller.state_path.read_text())["services"]["proxy"]
        self.assertEqual(stored["argv"], record["argv"])
        self.assertIn("stopped_at_unix", stored)

    def test_explicit_force_only_targets_the_same_verified_identity(self):
        self.record()
        current = [self.identity]

        def terminate(row, sig):
            if sig == signal.SIGKILL:
                current[0] = None

        with patch.object(module, "process_identity", side_effect=lambda _: current[0]), \
             patch.object(module, "owns_listener", return_value=True), \
             patch.object(module, "signal_recorded", side_effect=terminate) as kill:
            self.controller.stop("proxy", timeout=0, force=True)
        self.assertEqual([call.args[1] for call in kill.call_args_list], [signal.SIGTERM, signal.SIGKILL])

    def test_pidfd_identity_recheck_prevents_race_signal(self):
        record = self.record()
        with patch.object(module.os, "pidfd_open", return_value=555), \
             patch.object(module.os, "close") as close, \
             patch.object(module, "process_identity", return_value=dict(self.identity, start_ticks=999)), \
             patch.object(module.signal, "pidfd_send_signal") as send:
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                module.signal_recorded(record, signal.SIGTERM)
            send.assert_not_called()
            close.assert_called_once_with(555)

    def test_mocked_launch_records_exact_process_and_private_logs(self):
        with patch.object(module, "endpoint_available", return_value=True), \
             patch.object(module.subprocess, "Popen", return_value=SimpleNamespace(pid=self.identity["pid"])) as launch, \
             patch.object(module, "process_identity", return_value=self.identity), \
             patch.object(module, "owns_listener", return_value=True):
            result = self.controller.start("proxy")
        self.assertEqual(result["status"], "running")
        self.assertEqual(launch.call_args.args[0], list(self.specs["proxy"].argv))
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual(launch.call_args.kwargs["cwd"], self.root)
        self.assertEqual(self.controller.state_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in (self.controller.directory / "logs").iterdir()))

    def test_bridge_and_gateway_require_recorded_dependencies(self):
        for name in ("bridge", "gateway"):
            with self.subTest(name=name), patch.object(module.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(RuntimeError, "dependency"):
                    self.controller.start(name)
                launch.assert_not_called()

    def test_dependency_with_different_launch_configuration_is_rejected(self):
        with patch.object(self.controller, "status", return_value={"status": "running", "configuration_matches": False}), \
             patch.object(module.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "requested configuration"):
                self.controller.start("gateway")
            launch.assert_not_called()

    def test_record_and_socket_paths_cannot_escape_project(self):
        with self.assertRaises(ValueError):
            module.ServiceController(self.root, self.specs, state_path=self.controller.directory / "../foreign.json")
        bad = dict(self.specs)
        bad["bridge"] = replace(bad["bridge"], unix_socket=self.root / "experiment/runtime/gateways/../../foreign.sock")
        with self.assertRaises(ValueError):
            module.ServiceController(self.root, bad)

    def test_symlinked_registry_and_modified_socket_are_not_followed(self):
        self.controller._private_directory(self.controller.directory)
        target = self.controller.directory / "other.json"
        target.write_text("synthetic unrelated content")
        self.controller.state_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.controller.status("proxy")
        self.controller.state_path.unlink()
        record = self.record("bridge", socket_file_identity={"device": 1, "inode": 2, "uid": os.geteuid(), "is_socket": True})
        with patch.object(module, "socket_file_identity", return_value={"device": 1, "inode": 3, "uid": os.geteuid(), "is_socket": True}):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                self.controller._remove_owned_stopped_socket(record)


if __name__ == "__main__":
    unittest.main()
