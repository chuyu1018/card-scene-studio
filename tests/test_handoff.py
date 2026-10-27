import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from handoff import enqueue, listener_state, wait_for_project


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "projects" / "example" / "project.json"
        self.project.parent.mkdir(parents=True)
        self.project.write_text("{}")

    def start(self, timeout=2):
        ready = threading.Event()
        result = []
        thread = threading.Thread(target=lambda: result.append(wait_for_project(self.root, timeout, lambda _: ready.set())))
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        self.assertTrue(ready.wait(1))
        return thread, result

    def test_exactly_one_delivery_to_matching_live_listener(self):
        thread, result = self.start()
        token = listener_state(self.root)["listener_id"]
        self.assertFalse(enqueue(self.root, "wrong-token", self.project))
        self.assertTrue(enqueue(self.root, token, self.project))
        self.assertFalse(enqueue(self.root, token, self.project))
        thread.join(1)
        self.assertEqual(result[0]["project_path"], str(self.project.resolve()))
        self.assertEqual(result[0]["status"], "submitted")
        self.assertFalse(listener_state(self.root)["active"])

    def test_second_listener_and_external_project_are_rejected(self):
        thread, _ = self.start()
        with self.assertRaises(ValueError):
            wait_for_project(self.root, 0.1)
        external = self.root / "project.json"
        external.write_text("{}")
        token = listener_state(self.root)["listener_id"]
        with self.assertRaises(ValueError):
            enqueue(self.root, token, external)
        self.assertTrue(enqueue(self.root, token, self.project))
        thread.join(1)

    def test_timeout_cleans_marker_and_late_submission_is_not_accepted(self):
        thread, result = self.start(0.1)
        token = listener_state(self.root)["listener_id"]
        thread.join(1)
        self.assertEqual(result, [{"status": "timeout"}])
        self.assertFalse(enqueue(self.root, token, self.project))
        self.assertFalse((self.root / ".handoff/listener.json").exists())

    def test_crash_damaged_or_expired_marker_can_recover(self):
        folder = self.root / ".handoff"
        folder.mkdir()
        for content in ("{", json.dumps({"listener_id": "a" * 32, "pid": 1, "expires_at": 0})):
            (folder / "listener.json").write_text(content)
            thread, result = self.start()
            token = listener_state(self.root)["listener_id"]
            self.assertTrue(enqueue(self.root, token, self.project))
            thread.join(1)
            self.assertEqual(result[0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
