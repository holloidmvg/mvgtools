#!/usr/bin/env python3
"""Checks that holo_service.py's start diagnosis names the cause, not the last
domino. Standard library only: python3 test_holo_service.py

testdata/log_start_failed_no_pins.log is the 2026-10-22 hm-2p99 failure (an
Igor pump configured without pins), rebuilt from the tool's output; the frames
of the hardware traceback follow holo-control's call path.
testdata/log_started_no_igor_reply.log is the same day's start that passed
every check while the Igor did not answer; its traceback is as logged.
"""
import contextlib
import io
import os
import tempfile
import types
import unittest
from unittest import mock

import holo_service as hs

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "testdata", name)) as f:
        return f.read().splitlines()


class Diagnosis(unittest.TestCase):
    def setUp(self):
        self.lines = fixture("log_start_failed_no_pins.log")

    def test_cause_is_first_real_failure(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cause = hs.summarize(self.lines, "fixture")
        self.assertEqual(cause.exception, "RuntimeError: A minimum of one pin must be specified")
        self.assertIn("pumpmc_core.py:270", hs.short_frame(cause.frames[-1]))
        self.assertIn("Init hardware exception", cause.phase())
        text = out.getvalue()
        self.assertLess(text.index("A minimum of one pin"), text.index("has no attribute 'on'"))
        self.assertIn("broker refused the login (403)", text)
        self.assertIn("undervoltage (vcgencmd 0x50005)", text)

    def test_broker_and_none_errors_are_not_causes(self):
        failures = hs.tracebacks(self.lines)
        self.assertEqual([f.noise for f in failures], [True, False, False, False])
        self.assertEqual([f.follow_on for f in failures], [False, False, True, True])

    def test_error_lines_leave_out_noise_and_keep_frames(self):
        errors = hs.error_lines(self.lines)
        self.assertFalse(any("ACCESS_REFUSED" in e or "authenticat" in e for e in errors))
        self.assertNotIn("Traceback (most recent call last):", [e.strip() for e in errors])
        i = errors.index("RuntimeError: A minimum of one pin must be specified")
        self.assertIn("pumpmc_core.py", errors[i - 1])

    def test_started_without_hardware_is_noticed(self):
        # 2026-10-22 13:14: the Igor did not answer, yet every start check passed.
        layout = types.SimpleNamespace(log_dir=os.path.join(HERE, "testdata"))
        with mock.patch.object(hs, "newest_log",
                               return_value=os.path.join(HERE, "testdata", "log_started_no_igor_reply.log")):
            path, lines = hs.hardware_failure(layout, 0)
        self.assertIsNotNone(path)
        with contextlib.redirect_stdout(io.StringIO()):
            cause = hs.summarize(lines, "fixture")
        self.assertEqual(cause.exception, "RuntimeError: Unable to configure a pump")

    def test_started_with_hardware_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log_2026-10-22_131422.log")
            with open(path, "w") as f:
                f.write("2026-10-22_13:14:22.980 INFO device.py:1340 Init hardware: started\n"
                        "2026-10-22_13:14:23.500 INFO device.py:1368 Init hardware: finished\n")
            layout = types.SimpleNamespace(log_dir=tmp)
            self.assertEqual(hs.hardware_failure(layout, os.path.getmtime(path)), (None, None))

    def test_chained_exception_keeps_root(self):
        lines = ["2026-10-22_11:00:00.000 ERROR x.py:1 boom",
                 "Traceback (most recent call last):",
                 '  File "/a/root.py", line 5, in f',
                 "KeyError: 'pins'",
                 "",
                 "During handling of the above exception, another exception occurred:",
                 "",
                 "Traceback (most recent call last):",
                 '  File "/a/wrap.py", line 9, in g',
                 "RuntimeError: wrapped"]
        failures = hs.tracebacks(lines)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].exception, "KeyError: 'pins'")


if __name__ == "__main__":
    unittest.main()
