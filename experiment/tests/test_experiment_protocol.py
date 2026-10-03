from pathlib import Path
import json
import tempfile
import unittest
from experiment.src.experiment_protocol import schedule, atomic_json


class ProtocolTests(unittest.TestCase):
    def test_each_baseline_task_appears_once_in_stable_order(self):
        ids = [f"task-{i}" for i in range(24)]
        rows = schedule(ids)
        self.assertEqual(rows, schedule(list(reversed(ids))))
        self.assertEqual(len(rows), 24)
        self.assertEqual({r["task_id"] for r in rows}, set(ids))
        self.assertEqual({r["condition"] for r in rows}, {"A0"})

    def test_atomic_json_preserves_unicode_and_leaves_no_partial_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested/state.json"
            atomic_json(path, {"状态": "completed"})
            self.assertEqual(json.loads(path.read_text()), {"状态": "completed"})
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__": unittest.main()
