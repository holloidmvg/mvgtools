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


class Install(unittest.TestCase):
    def test_saved_server(self):
        conf = "connection uplink\naddress 10.77.0.42:9883\ntopic holloid/# both 1\n"
        self.assertEqual(hs.saved_server(conf), ("10.77.0.42", 9883))
        self.assertEqual(hs.saved_server("connection uplink\naddress 10.77.0.42\n"), ("10.77.0.42", hs.SERVER_PORT))
        self.assertIsNone(hs.saved_server(None))
        self.assertIsNone(hs.saved_server("# left unset (--no-server)\n"))

    def test_command_leaves_the_checkout_alone(self):
        with mock.patch.object(hs.os, "geteuid", return_value=1000):
            cmd = hs.install_command("/home/mage/gui_new", "mage", True, [])
        self.assertEqual(cmd, ["sudo", "--preserve-env=SSH_AUTH_SOCK", "/home/mage/gui_new/remote/deploy/install_device.sh",
                               "--gui-dir", "/home/mage/gui_new", "--gui-user", "mage", "--headless"])
        with mock.patch.object(hs.os, "geteuid", return_value=0):
            cmd = hs.install_command("/home/mage/gui_new", "mage", False, ["--rename"], branch="fix-x", https=True)
        self.assertEqual(cmd[0], "/home/mage/gui_new/remote/deploy/install_device.sh")
        self.assertEqual(cmd[-6:], ["--headless", "--branch", "fix-x", "--https", "--no-server", "--rename"])

    def test_no_server_when_the_server_does_not_answer(self):
        with mock.patch.object(hs.glob, "glob", return_value=["/etc/openvpn/client/hm-2p99.conf"]), \
                mock.patch.object(hs, "read_file", return_value="connection uplink\naddress 10.77.0.42:8883\n"), \
                mock.patch.object(hs, "port_open", return_value=False) as probe:
            self.assertIn("10.77.0.42:8883 does not answer", hs.no_server_reason())
        probe.assert_called_with(8883, "10.77.0.42")
        with mock.patch.object(hs.glob, "glob", return_value=["/etc/openvpn/client/hm-2p99.conf"]), \
                mock.patch.object(hs, "read_file", return_value=None), \
                mock.patch.object(hs, "port_open", return_value=True) as probe:
            self.assertIsNone(hs.no_server_reason())
        probe.assert_called_with(hs.SERVER_PORT, hs.SERVER_HOST)
        with mock.patch.object(hs.glob, "glob", return_value=[]):
            self.assertIn("no OpenVPN config", hs.no_server_reason())


class UiStamp(unittest.TestCase):
    # ui_stamp also gave the stamp the vite build wrote for holo-control 8949f3b7,
    # checked by hand: these only pin down what it hashes.
    def ui(self, tmp, files):
        for rel, text in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="") as f:
                f.write(text)
        return tmp

    def test_line_endings_and_tests_do_not_count(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.ui(a, {"src/App.tsx": "x\n", "src/lib/y.ts": "y\n", "index.html": "<html>\n"})
            self.ui(b, {"src/App.tsx": "x\r\n", "src/lib/y.ts": "y\n", "index.html": "<html>\n",
                        "src/lib/y.test.ts": "t\n", "src/lib/y.cases.ts": "c\n"})
            self.assertEqual(hs.ui_stamp(a), hs.ui_stamp(b))
            self.ui(b, {"src/lib/y.ts": "changed\n"})
            self.assertNotEqual(hs.ui_stamp(a), hs.ui_stamp(b))

    def test_stale_bundle_is_refused(self):
        with tempfile.TemporaryDirectory() as gui:
            ui = os.path.join(gui, "remote", "ui")
            self.ui(ui, {"src/App.tsx": "x\n", "dist/index.html": "<html>\n"})
            self.ui(ui, {"dist/.src-stamp": hs.ui_stamp(ui)})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(hs.check_ui_bundle(gui))
                self.ui(ui, {"src/App.tsx": "edited, not rebuilt\n"})
                self.assertFalse(hs.check_ui_bundle(gui))


if __name__ == "__main__":
    unittest.main()
