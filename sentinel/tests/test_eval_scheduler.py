"""Tests for the nightly eval scheduler and the POST /admin/eval/run endpoint.

Covers:
- start_eval_scheduler() starts exactly one daemon thread; a second call is a no-op.
- The accuracy-floor warning path fires when accuracy is below EVAL_ACCURACY_FLOOR.
- POST /admin/eval/run returns the expected response shape (mocked run_golden_set).
"""

from __future__ import annotations

import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sentinel.eval.run_eval import CaseScore
from sentinel.tools.audit_log import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_case_score(correct: bool = True) -> CaseScore:
    """Minimal CaseScore fixture — all fields required by the dataclass."""
    return CaseScore(
        case_id="test-case-1",
        asset_type="text",
        category="No Violation",
        expected_outcome="allow",
        predicted_outcome="allow" if correct else "reject",
        final_decision="allow" if correct else "reject",
        reviewer="specialist",
        ticket_id=None,
        quarantined=False,
        correct=correct,
    )


# ---------------------------------------------------------------------------
# Scheduler thread tests
# ---------------------------------------------------------------------------

class SchedulerStartTests(unittest.TestCase):
    """Verify the threading behaviour of start_eval_scheduler()."""

    def setUp(self):
        # Re-import the module fresh so the _started Event is clear for each
        # test class.  We reset the Event directly here too so individual test
        # methods within the class can be isolated.
        import sentinel.eval.scheduler as _sched
        self._sched = _sched
        _sched._started.clear()

    def tearDown(self):
        # Clear after each test so the event doesn't leak between tests.
        self._sched._started.clear()

    def test_start_creates_exactly_one_daemon_thread(self):
        """start_eval_scheduler() spawns a background thread that is a daemon."""
        # Capture the Thread object that start_eval_scheduler creates so we can
        # assert on it directly — no need to search threading.enumerate() and
        # potentially pick up threads from other tests.
        created_threads: list[threading.Thread] = []
        real_Thread = threading.Thread

        def capturing_Thread(*args, **kwargs):
            t = real_Thread(*args, **kwargs)
            created_threads.append(t)
            return t

        gate = threading.Event()

        def _loop_gate():
            gate.wait(timeout=5)

        with patch.object(self._sched, "_eval_loop", side_effect=_loop_gate):
            with patch("sentinel.eval.scheduler.threading.Thread", side_effect=capturing_Thread):
                self._sched.start_eval_scheduler()
            gate.set()

        self.assertEqual(len(created_threads), 1)
        self.assertTrue(created_threads[0].daemon)

    def test_second_call_does_not_start_a_second_thread(self):
        """A second start_eval_scheduler() call is silently ignored."""
        with patch.object(self._sched, "_eval_loop", return_value=None):
            self._sched.start_eval_scheduler()
            # Count threads after first call.
            count_after_first = sum(
                1 for t in threading.enumerate()
                if t.name == "sentinel-eval-scheduler"
            )
            self._sched.start_eval_scheduler()
            count_after_second = sum(
                1 for t in threading.enumerate()
                if t.name == "sentinel-eval-scheduler"
            )

        self.assertEqual(count_after_first, count_after_second)


# ---------------------------------------------------------------------------
# Accuracy floor warning tests
# ---------------------------------------------------------------------------

class AccuracyFloorWarningTests(unittest.TestCase):
    """Verify the _eval_loop() warning path when accuracy is below the floor."""

    def setUp(self):
        import sentinel.eval.scheduler as _sched
        self._sched = _sched
        _sched._started.clear()

    def tearDown(self):
        self._sched._started.clear()

    def test_warning_logged_when_accuracy_below_floor(self):
        """If compute_metrics returns accuracy < EVAL_ACCURACY_FLOOR, a WARNING is emitted."""
        import sentinel.eval.scheduler as sched_module

        bad_score = _make_case_score(correct=False)

        # compute_metrics on a single wrong case → accuracy = 0.0 which is below 0.88.
        with patch.object(sched_module, "run_golden_set", return_value=[bad_score]):
            with patch.object(sched_module, "write_report", return_value=Path("/tmp/run")):
                # Drive one loop iteration: sleep for 0 s, run, then raise to break the loop.
                sleep_calls = []

                def fake_sleep(seconds):
                    sleep_calls.append(seconds)
                    if len(sleep_calls) > 1:
                        raise StopIteration("stop loop")

                with patch("sentinel.eval.scheduler.time.sleep", side_effect=fake_sleep):
                    with self.assertLogs("sentinel.eval.scheduler", level="WARNING") as log_ctx:
                        try:
                            sched_module._eval_loop()
                        except StopIteration:
                            pass

        warning_messages = [r for r in log_ctx.output if "WARNING" in r and "below" in r.lower()]
        self.assertTrue(
            warning_messages,
            "Expected a WARNING log about accuracy below floor; got: " + str(log_ctx.output),
        )

    def test_no_warning_when_accuracy_above_floor(self):
        """If accuracy >= EVAL_ACCURACY_FLOOR no WARNING is emitted for the floor."""
        import sentinel.eval.scheduler as sched_module

        good_score = _make_case_score(correct=True)

        with patch.object(sched_module, "run_golden_set", return_value=[good_score]):
            with patch.object(sched_module, "write_report", return_value=Path("/tmp/run")):
                sleep_calls = []

                def fake_sleep(seconds):
                    sleep_calls.append(seconds)
                    if len(sleep_calls) > 1:
                        raise StopIteration("stop loop")

                with patch("sentinel.eval.scheduler.time.sleep", side_effect=fake_sleep):
                    import logging
                    with self.assertLogs("sentinel.eval.scheduler", level="INFO") as log_ctx:
                        try:
                            sched_module._eval_loop()
                        except StopIteration:
                            pass

        floor_warnings = [r for r in log_ctx.output if "WARNING" in r and "below" in r.lower()]
        self.assertEqual(floor_warnings, [])


# ---------------------------------------------------------------------------
# POST /admin/eval/run endpoint tests
# ---------------------------------------------------------------------------

class EvalRunEndpointTests(unittest.TestCase):
    """Test the POST /admin/eval/run route via the FastAPI test client."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmpdir.name)
        self.db_path = self.base_path / "audit.sqlite"
        self.eval_runs_dir = self.base_path / "eval_runs"
        self.eval_runs_dir.mkdir()
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        import sentinel.api as api_module

        app = api_module.create_app(
            db_path=self.db_path,
            upload_dir=self.base_path / "uploads",
            admin_token="admin-secret",
        )
        return TestClient(app)

    def _mock_run_golden_set(self, scores):
        """Return a context manager that patches run_golden_set in api.py's lazy import."""
        return patch("sentinel.eval.run_eval.run_golden_set", return_value=scores)

    def test_returns_summary_shape(self):
        """POST /admin/eval/run returns the expected JSON keys and types."""
        score = _make_case_score(correct=True)

        with self._mock_run_golden_set([score]):
            client = self._client()
            response = client.post(
                "/admin/eval/run",
                headers={"Authorization": "Bearer admin-secret"},
                json={"mode": "offline", "live_all": False},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("run_dir", payload)
        self.assertIn("accuracy", payload)
        self.assertIn("tier1_recall", payload)
        self.assertIn("benign_fpr", payload)
        self.assertIn("cases", payload)
        self.assertIn("below_floor", payload)
        self.assertEqual(payload["cases"], 1)
        self.assertIsInstance(payload["below_floor"], bool)

    def test_below_floor_true_when_accuracy_is_low(self):
        """below_floor is True when every case is wrong (accuracy = 0.0)."""
        score = _make_case_score(correct=False)

        with self._mock_run_golden_set([score]):
            client = self._client()
            response = client.post(
                "/admin/eval/run",
                headers={"Authorization": "Bearer admin-secret"},
                json={"mode": "offline"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["below_floor"])
        self.assertEqual(payload["accuracy"], 0.0)

    def test_below_floor_false_when_accuracy_is_perfect(self):
        """below_floor is False when all cases are correct (accuracy = 1.0)."""
        score = _make_case_score(correct=True)

        with self._mock_run_golden_set([score]):
            client = self._client()
            response = client.post(
                "/admin/eval/run",
                headers={"Authorization": "Bearer admin-secret"},
                json={"mode": "offline"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["below_floor"])
        self.assertEqual(payload["accuracy"], 1.0)

    def test_requires_admin_token(self):
        """POST /admin/eval/run without a valid token returns 401."""
        score = _make_case_score(correct=True)

        with self._mock_run_golden_set([score]):
            client = self._client()
            response = client.post(
                "/admin/eval/run",
                headers={"Authorization": "Bearer wrong-token"},
                json={"mode": "offline"},
            )

        self.assertEqual(response.status_code, 401)

    def test_default_body_is_accepted(self):
        """POST /admin/eval/run with no body uses default mode=live."""
        score = _make_case_score(correct=True)

        with self._mock_run_golden_set([score]):
            client = self._client()
            response = client.post(
                "/admin/eval/run",
                headers={"Authorization": "Bearer admin-secret"},
            )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
