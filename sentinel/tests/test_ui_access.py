"""Tests for the Streamlit UI access-control helpers.

The production tab triggers paid model calls, so its gate must fail closed:
no configured password never means "any password passes", and a malformed
run-cap value must fall back to the default instead of disabling the cap.
"""

import unittest
from unittest.mock import patch

from sentinel.ui_uploads import (
    DEFAULT_UI_MAX_LIVE_RUNS,
    UI_MAX_LIVE_RUNS_ENV,
    UI_PASSWORD_ENV,
    ui_live_run_cap,
    verify_ui_password,
)


class VerifyUiPasswordTests(unittest.TestCase):
    def test_correct_password_passes(self):
        with patch.dict("os.environ", {UI_PASSWORD_ENV: "hunter2"}):
            self.assertTrue(verify_ui_password("hunter2"))

    def test_wrong_password_fails(self):
        with patch.dict("os.environ", {UI_PASSWORD_ENV: "hunter2"}):
            self.assertFalse(verify_ui_password("hunter3"))

    def test_no_configured_password_never_verifies(self):
        # An unset password means "no gate", not "everything verifies" — the
        # caller decides whether to gate; verification itself must fail closed.
        with patch.dict("os.environ", {UI_PASSWORD_ENV: ""}):
            self.assertFalse(verify_ui_password(""))
            self.assertFalse(verify_ui_password("anything"))

    def test_configured_password_is_stripped(self):
        with patch.dict("os.environ", {UI_PASSWORD_ENV: "  hunter2  "}):
            self.assertTrue(verify_ui_password("hunter2"))


class UiLiveRunCapTests(unittest.TestCase):
    def test_default_when_unset(self):
        with patch.dict("os.environ", {UI_MAX_LIVE_RUNS_ENV: ""}):
            self.assertEqual(ui_live_run_cap(), DEFAULT_UI_MAX_LIVE_RUNS)

    def test_explicit_value(self):
        with patch.dict("os.environ", {UI_MAX_LIVE_RUNS_ENV: "5"}):
            self.assertEqual(ui_live_run_cap(), 5)

    def test_zero_means_unlimited(self):
        with patch.dict("os.environ", {UI_MAX_LIVE_RUNS_ENV: "0"}):
            self.assertEqual(ui_live_run_cap(), 0)

    def test_garbage_falls_back_to_default_not_unlimited(self):
        with patch.dict("os.environ", {UI_MAX_LIVE_RUNS_ENV: "lots"}):
            self.assertEqual(ui_live_run_cap(), DEFAULT_UI_MAX_LIVE_RUNS)

    def test_negative_falls_back_to_default_not_unlimited(self):
        with patch.dict("os.environ", {UI_MAX_LIVE_RUNS_ENV: "-1"}):
            self.assertEqual(ui_live_run_cap(), DEFAULT_UI_MAX_LIVE_RUNS)


if __name__ == "__main__":
    unittest.main()
