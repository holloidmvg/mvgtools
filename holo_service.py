#!/usr/bin/env python3
"""Stop or restart the device software, whichever version this device runs.

Two layouts exist in the field:

  old  (interim_main): one holo_gui unit runs ~/gui/client_app.py, which serves
       the Dash gui on :8050 and forks the "hollohw" hardware process.
  new  (new-ui): the software lives in ~/gui_new and runs as holo_gui or, when
       installed --headless, holo_device (device API on 127.0.0.1:1234), next to
       holo_bridge (:8080), mosquitto (:1883/:9001) and nginx (:80).

Either way logs land in ~/gui/log, whatever the software's own directory.

usage: holo_service.py {status,stop,restart} [--style old|new] [--home DIR] [--timeout S]
       holo_service.py filesync [--home DIR] [--force]   # write a GCS-only /etc/holloid/filesync.json
       holo_service.py install [--no-server|--with-server] [--full [--https]] [--stale-ui] [-- INSTALLER ARGS]
                                # install ~/gui_new as checked out: its UI and bridge, then restart

Uses only the standard library so it runs on the device's system python.
Run as root or a sudoer: systemctl and journalctl are called through sudo
when needed. Run `install` as the device user, not under sudo: only the
installer gets sudo, and with --full it keeps the forwarded ssh agent (`ssh -A`)
for git.
"""
import argparse
import glob
import hashlib
import json
import os
import pwd
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

DEFAULT_HOME = "/home/mage"

# The same pattern install_device.sh uses to find a copy of the device software.
DEVICE_SOFTWARE = re.compile(r"python[^ ]* (.* )?([^ ]*/)?client_(app|device)\.py( |$)|^hollohw")
BRIDGE = re.compile(r"remote\.bridge\.run_bridge")

GUI_PORT = 8050
DEVICE_API_PORT = 1234
BRIDGE_PORT = 8080
BROKER_PORT = 1883
NGINX_PORT = 80

# Where the device software reads its file-sync targets, both versions alike.
FILESYNC_CONFIG = "/etc/holloid/filesync.json"
GCS_BROKER_HOST = "10.77.0.3"
GCS_BROKER_PORT = 5672

# What install_device.sh links a new-ui device to, and where it keeps the link
# once written. With --no-server it neither writes nor checks one.
INSTALLER = "remote/deploy/install_device.sh"
UPLINK_CONF = "/opt/holloid/cloud.conf"
SERVER_HOST = "10.77.0.17"
SERVER_PORT = 8883
VPN_CONFIGS = ["/etc/openvpn/client/*.conf", "/etc/openvpn/*.conf"]
DEVICE_CONFIG = "/etc/holloid/configuration.json"

# The device API must answer this many checks in a row, without a restart in
# between, before the start counts. A config error often lets the process come
# up and die a few seconds later.
STABLE_CHECKS = 3
CHECK_INTERVAL_S = 2

ERROR_LINE = re.compile(r"Traceback|Error|Exception|CRITICAL|FATAL|ERROR|failed", re.IGNORECASE)

# Errors that never stop a start by themselves: the link to the GCS broker
# (VPN or credentials) and its retries. They get a one-line note, not the headline.
BROKER_NOISE = re.compile(r"pika|AMQP|ACCESS_REFUSED|AuthenticationError|gcs_broker|broker\.py"
                          r"|connection_workflow|io_services_utils|blocking_connection"
                          r"|while authenticating")
# Where the device software says in its own words which phase went wrong.
LANDMARK = re.compile(r"Init hardware exception|Hardware Init failed|Device stopped with an error"
                      r"|Device API did not start")
# What the device software logs when the hardware did not come up, even though
# the software itself (and its device API) carries on.
HARDWARE_FAILED = re.compile(r"Hardware Init failed|Init hardware exception")
# What a component that was never created looks like once the code reaches for
# it: almost always a consequence of an earlier failure, not a cause.
FOLLOW_ON = re.compile(r"'NoneType' object (has no attribute|is not iterable|is not subscriptable)")
LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d_(\d\d:\d\d:\d\d\.\d+) (?:DEBUG|INFO|WARNING|ERROR|CRITICAL) ")
JOURNAL_PREFIX = re.compile(r"^\S+ \S+ [^\s:]+\[\d+\]: ")
FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)')
UNDERVOLTAGE = re.compile(r"undervoltage.*?(0x[0-9a-fA-F]+)")

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def color(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else text


def ok(msg):
    print("  " + color("OK  ", "32") + msg)


def bad(msg):
    print("  " + color("FAIL", "1;31") + " " + msg)


def info(msg):
    print("  " + msg)


def heading(msg):
    print("\n" + color("=== %s ===" % msg, "1"))


# --- system helpers -------------------------------------------------------------

def privileged(cmd):
    return cmd if os.geteuid() == 0 else ["sudo"] + cmd


def run(cmd, check=False):
    proc = subprocess.run(privileged(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    if check and proc.returncode != 0:
        raise RuntimeError("%s failed:\n%s" % (" ".join(cmd), proc.stdout.strip()))
    return proc


def unit_props(unit):
    """A few `systemctl show` properties; LoadState is 'not-found' for a unit that does not exist."""
    names = ["LoadState", "ActiveState", "SubState", "UnitFileState", "WorkingDirectory",
             "ExecStart", "NRestarts", "MainPID", "Result"]
    out = subprocess.run(["systemctl", "show", unit + ".service"] + ["-p" + n for n in names],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         universal_newlines=True).stdout
    props = dict.fromkeys(names, "")
    for line in out.splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    return props


def unit_exists(unit):
    props = unit_props(unit)
    return props["LoadState"] not in ("", "not-found", "masked")


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def processes():
    """Every process as (pid, cmdline, cwd). cwd is '' when unreadable."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if not cmdline:
            continue
        try:
            cwd = os.readlink("/proc/%s/cwd" % entry)
        except OSError:
            cwd = ""
        found.append((int(entry), cmdline, cwd))
    return found


def device_software_procs():
    return [p for p in processes() if DEVICE_SOFTWARE.search(p[1])]


def bridge_procs():
    return [p for p in processes() if BRIDGE.search(p[1])]


def software_dir(cmdline, cwd):
    """The install a device-software process runs from: the script's directory, else its cwd."""
    match = re.search(r"(\S*)/client_(app|device)\.py", cmdline)
    if match and match.group(1).startswith("/"):
        return match.group(1)
    return cwd


# --- detection ------------------------------------------------------------------

class Layout(object):
    def __init__(self, style, home, evidence):
        self.style = style
        self.home = home
        self.evidence = evidence
        self.gui_dir = os.path.join(home, "gui_new" if style == "new" else "gui")
        self.log_dir = os.path.join(home, "gui", "log")
        if style == "new":
            # --headless installs holo_device and masks holo_gui.
            self.device_unit = "holo_device" if unit_exists("holo_device") else "holo_gui"
            self.helper_units = ["holo_bridge"]
            self.infra_units = ["mosquitto", "nginx"]
        else:
            self.device_unit = "holo_gui"
            self.helper_units = []
            self.infra_units = []
        self.headless = self.device_unit == "holo_device"

    @property
    def units(self):
        """The units this script stops and restarts, in start order."""
        return self.helper_units + [self.device_unit] if self.style == "new" else [self.device_unit]


def detect(home):
    """Decide old vs new from what runs, then from the units, then from ~/gui_new.

    Running processes come first because `install_device.sh --no-start` can
    stage ~/gui_new on a device that still runs the old software.
    """
    gui_new = os.path.join(home, "gui_new")
    evidence = []
    votes = []

    for pid, cmdline, cwd in device_software_procs():
        where = software_dir(cmdline, cwd)
        if where.rstrip("/") == gui_new:
            votes.append("new")
            evidence.append("pid %d runs from %s: %s" % (pid, where, cmdline))
        elif where:
            votes.append("old")
            evidence.append("pid %d runs from %s: %s" % (pid, where, cmdline))
    for pid, cmdline, _ in bridge_procs():
        votes.append("new")
        evidence.append("pid %d is the provisioner bridge" % pid)

    if not votes:
        gui = unit_props("holo_gui")
        if unit_exists("holo_device") or unit_exists("holo_bridge"):
            votes.append("new")
            evidence.append("nothing runs; holo_bridge/holo_device units are installed")
        elif gui["WorkingDirectory"].rstrip("/") == gui_new:
            votes.append("new")
            evidence.append("nothing runs; holo_gui points at %s" % gui_new)
        elif gui["LoadState"] == "loaded":
            votes.append("old")
            evidence.append("nothing runs; holo_gui points at %s" % (gui["WorkingDirectory"] or "?"))

    if os.path.isdir(gui_new):
        evidence.append("%s exists" % gui_new)
        if not votes:
            votes.append("new")
    else:
        evidence.append("%s does not exist" % gui_new)

    if not votes:
        return None, evidence
    if "new" in votes and "old" in votes:
        evidence.append(color("both versions appear to be running; two copies must never drive "
                              "the hardware at once", "1;31"))
        return "conflict", evidence
    return votes[0], evidence


# --- stop -----------------------------------------------------------------------

def kill_strays(timeout_s=15):
    """Kill device software running outside any unit we just stopped (stray hollohw children,
    a copy started by hand). Returns the processes that would not die."""
    strays = device_software_procs()
    if not strays:
        return []
    for pid, cmdline, _ in strays:
        info("stopping stray pid %d: %s" % (pid, cmdline))
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            run(["kill", str(pid)])
    deadline = time.time() + timeout_s
    while time.time() < deadline and any(os.path.exists("/proc/%d" % p[0]) for p in strays):
        time.sleep(1)
    for pid, _, _ in strays:
        if os.path.exists("/proc/%d" % pid):
            run(["kill", "-9", str(pid)])
    time.sleep(1)
    return device_software_procs()


def stop(layout):
    heading("Stopping %s-style software" % layout.style)
    failed = False
    # The bridge can restart the device unit out of band, so it goes first.
    for unit in reversed(layout.units):
        if not unit_exists(unit):
            info("%s: not installed, skipped" % unit)
            continue
        proc = run(["systemctl", "stop", unit])
        state = unit_props(unit)["ActiveState"]
        if proc.returncode == 0 and state in ("inactive", "failed"):
            ok("%s stopped" % unit)
        else:
            bad("%s did not stop (state %s) %s" % (unit, state, proc.stdout.strip()))
            failed = True
    left = kill_strays()
    if left:
        for pid, cmdline, _ in left:
            bad("still running, pid %d: %s" % (pid, cmdline))
        failed = True
    else:
        ok("no device software left running")
    if layout.infra_units:
        info("left running (shared system services): %s" % ", ".join(layout.infra_units))
    return not failed


# --- start and verify -----------------------------------------------------------

class Check(object):
    def __init__(self, name, test, unit=None):
        self.name = name
        self.test = test
        self.unit = unit
        self.passed = False
        self.detail = ""


def unit_running(unit, restarts_before):
    def test():
        props = unit_props(unit)
        if props["NRestarts"] != restarts_before.get(unit, props["NRestarts"]):
            return False, "restarted by systemd (NRestarts %s -> %s): it crashed" % (
                restarts_before.get(unit), props["NRestarts"])
        if props["ActiveState"] != "active":
            return False, "%s/%s (result: %s)" % (props["ActiveState"], props["SubState"], props["Result"])
        return True, "active, pid %s" % props["MainPID"]
    return test


def listening(port, what):
    def test():
        return (True, "%s answers on :%d" % (what, port)) if port_open(port) \
            else (False, "nothing answers on :%d" % port)
    return test


def hardware_process():
    def test():
        procs = [p for p in processes() if p[1].startswith("hollohw")]
        return (True, "hollohw pid %d" % procs[0][0]) if procs else (False, "no hollohw process")
    return test


def checks_for(layout, restarts_before):
    unit = layout.device_unit
    checks = [Check(unit, unit_running(unit, restarts_before), unit)]
    if layout.style == "old":
        checks.append(Check("hardware process", hardware_process(), unit))
        checks.append(Check("Dash gui :%d" % GUI_PORT, listening(GUI_PORT, "gui"), unit))
        return checks
    checks.append(Check("device API :%d" % DEVICE_API_PORT, listening(DEVICE_API_PORT, "device API"), unit))
    if not layout.headless:
        checks.append(Check("Dash gui :%d" % GUI_PORT, listening(GUI_PORT, "gui"), unit))
    checks.append(Check("holo_bridge", unit_running("holo_bridge", restarts_before), "holo_bridge"))
    checks.append(Check("bridge :%d" % BRIDGE_PORT, listening(BRIDGE_PORT, "bridge"), "holo_bridge"))
    checks.append(Check("mosquitto :%d" % BROKER_PORT, listening(BROKER_PORT, "broker"), "mosquitto"))
    checks.append(Check("nginx :%d" % NGINX_PORT, listening(NGINX_PORT, "web UI"), "nginx"))
    return checks


def wait_for(checks, timeout_s):
    """Poll until every check passes STABLE_CHECKS times in a row, or time runs out."""
    streak = 0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(CHECK_INTERVAL_S)
        for check in checks:
            check.passed, check.detail = check.test()
        streak = streak + 1 if all(c.passed for c in checks) else 0
        if streak >= STABLE_CHECKS:
            return True
        # A unit systemd gave up on will not come back within the wait.
        if any(not c.passed and ("failed/" in c.detail or "restarted by systemd" in c.detail)
               for c in checks):
            return False
    return False


def start(layout, timeout_s):
    heading("Starting %s-style software from %s" % (layout.style, layout.gui_dir))
    started_at = time.time()
    for unit in layout.infra_units:
        if unit_props(unit)["ActiveState"] != "active":
            proc = run(["systemctl", "start", unit])
            (ok if proc.returncode == 0 else bad)("%s started%s" % (unit, "" if proc.returncode == 0
                                                                   else ": " + proc.stdout.strip()))
    restarts_before = {}
    for unit in layout.units:
        if not unit_exists(unit):
            bad("%s is not installed (or masked); nothing to start" % unit)
            return False, started_at
        run(["systemctl", "reset-failed", unit])
        proc = run(["systemctl", "start", unit])
        restarts_before[unit] = unit_props(unit)["NRestarts"]
        if proc.returncode != 0:
            bad("systemctl start %s: %s" % (unit, proc.stdout.strip()))
        else:
            info("%s started, waiting for it to come up..." % unit)

    return verify(layout, checks_for(layout, restarts_before), timeout_s, started_at), started_at


def verify(layout, checks, timeout_s, started_at):
    """Wait for the checks, then say whether the software and the hardware came up,
    or diagnose why not."""
    success = wait_for(checks, timeout_s)

    heading("Result")
    for check in checks:
        (ok if check.passed else bad)("%-22s %s" % (check.name, check.detail))
    if success:
        # The device API comes up even when the hardware does not, so passing
        # checks only mean the software runs; the log says whether the hardware does.
        path, lines = hardware_failure(layout, started_at)
        if path is None:
            print("\n" + color("Started: every check passed %d times in a row." % STABLE_CHECKS, "1;32"))
            return True
        print("\n" + color("Started, but the hardware did not initialise: the device API is up, "
                           "the hardware is not.", "1;33"))
        summarize(lines, os.path.basename(path))
        print("\nFull log: %s" % path)
        return False

    print("\n" + color("Start FAILED after %ds." % timeout_s, "1;31"))
    failed_units = []
    for check in checks:
        if not check.passed and check.unit and check.unit not in failed_units:
            failed_units.append(check.unit)
    diagnose(layout, failed_units, started_at)
    return False


# --- diagnosis ------------------------------------------------------------------

def journal(unit, since, lines=40):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since - 1))
    proc = run(["journalctl", "-u", unit, "--since", stamp, "--no-pager", "-o", "short-iso", "-n", str(lines)])
    return proc.stdout.rstrip()


def newest_log(log_dir, since):
    logs = [p for p in glob.glob(os.path.join(log_dir, "log_*.log*")) if os.path.getmtime(p) >= since - 2]
    return max(logs, key=os.path.getmtime) if logs else None


def hardware_failure(layout, since):
    """The device log written since the start and its lines, if it reports that
    the hardware did not initialise; (None, None) otherwise."""
    path = newest_log(layout.log_dir, since)
    if path is None:
        return None, None
    lines = read_tail(path, 20000)
    if any(HARDWARE_FAILED.search(line) for line in lines):
        return path, lines
    return None, None


def read_tail(path, lines):
    try:
        with open(path, errors="replace") as f:
            return f.read().splitlines()[-lines:]
    except PermissionError:
        return run(["tail", "-n", str(lines), path]).stdout.splitlines()


class Failure:
    """One traceback in a log, with the log line that announced it."""

    def __init__(self, index, lines, context):
        self.index = index
        self.lines = lines
        self.context = context
        self.exception = lines[-1].strip()
        frames = [line for line in lines if FRAME.match(line)]
        # The frames of the first traceback only: with chained exceptions that
        # is the root, and its innermost frame is where things went wrong.
        self.frames = frames
        match = LOG_PREFIX.match(context)
        self.time = match.group(1) if match else None
        self.noise = bool(BROKER_NOISE.search(context) or BROKER_NOISE.search(self.exception))
        self.follow_on = bool(FOLLOW_ON.search(self.exception))

    def phase(self):
        """The announcing log line without its timestamp and level."""
        return LOG_PREFIX.sub("", self.context).split("Traceback")[0].strip()


def short_frame(line):
    match = FRAME.match(line)
    path = "/".join(match.group(1).split("/")[-2:])
    return "%s:%s in %s" % (path, match.group(2), match.group(3))


def tracebacks(lines):
    """Every traceback in the lines, oldest first; a chained exception
    ('During handling ...', 'The above exception ...') joins the one before it
    and keeps that one's exception, the root."""
    found = []
    i = 0
    while i < len(lines):
        if "Traceback (most recent call last)" not in lines[i]:
            i += 1
            continue
        end = i + 1
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
            end += 1
        block = lines[i:end + 1]
        before = [line.strip() for line in lines[max(0, i - 3):i] if line.strip()]
        previous = before[-1] if before else ""
        if found and previous.startswith(("During handling", "The above exception")):
            found[-1].lines += [previous] + block
        elif lines[i].split("Traceback")[0].strip():
            found.append(Failure(i, block, lines[i]))
        else:
            found.append(Failure(i, block, previous))
        i = end + 1
    return found


def notes_for(lines, failures):
    """One-liners for things worth knowing that did not stop the start."""
    notes = []
    if any("ACCESS_REFUSED" in line for line in lines):
        notes.append("GCS broker refused the login (403): its credentials, not the network")
    elif any(f.noise for f in failures):
        notes.append("GCS broker connection errors (VPN or broker down)")
    for line in lines:
        match = UNDERVOLTAGE.search(line)
        if match:
            notes.append("supply undervoltage (vcgencmd %s): check the power supply" % match.group(1))
            break
    return notes


def summarize(lines, source):
    """Print the first real failure as the cause, what followed from it, and the
    rest as notes. A crash cascades: the last traceback is the last domino."""
    failures = tracebacks(lines)
    real = [f for f in failures if not f.noise]
    cause = next((f for f in real if not f.follow_on), real[0] if real else None)

    print("\n" + color("--- Most likely cause (from %s) ---" % source, "1"))
    if cause is None:
        landmarks = [line for line in lines if LANDMARK.search(line)]
        if landmarks:
            info("no traceback, but the software reported:")
            for line in landmarks[:5]:
                info(color(LOG_PREFIX.sub("", line), "31"))
        else:
            info("no traceback or failure message found; see the log tails below")
    else:
        info("first failure%s%s" % (" at " + cause.time if cause.time else "",
                                    ", in: " + cause.phase() if cause.phase() else ""))
        for frame in cause.frames[-3:]:
            info("  " + short_frame(frame))
        info("  " + color(cause.exception, "1;31"))

        later = [f for f in real if f.index > cause.index]
        if later:
            print("\n" + color("--- What followed ---", "1"))
            for f in later:
                where = " (%s)" % short_frame(f.frames[-1]) if f.frames else ""
                info("%s%s%s" % (f.time + " " if f.time else "", f.exception, where))
                if f.follow_on:
                    info(color("    a component that was never created: follows from the cause above", "33"))

    notes = notes_for(lines, failures)
    if notes:
        print("\n" + color("--- Also noticed (not why the start failed) ---", "1"))
        for note in notes:
            info(note)
    return cause


def error_lines(lines):
    """The error lines without broker noise or bare 'Traceback' headers; each
    traceback's exception line comes with its innermost frame, so it says where."""
    failures = tracebacks(lines)
    noisy = set()
    innermost = {}
    for f in failures:
        span = range(f.index, f.index + len(f.lines))
        if f.noise:
            noisy.update(span)
        elif f.frames:
            innermost[f.index + len(f.lines) - 1] = f.frames[-1]
    errors = []
    for i, line in enumerate(lines):
        if i in noisy or BROKER_NOISE.search(line) or line.strip() == "Traceback (most recent call last):":
            continue
        if i in innermost:
            errors.append(innermost[i])
            errors.append(line)
        elif ERROR_LINE.search(line) and not line.startswith((" ", "\t")):
            errors.append(line)
    return errors


def show(title, lines):
    print("\n" + color("--- %s ---" % title, "1"))
    for line in lines:
        print("  " + (color(line, "31") if ERROR_LINE.search(line) else line))


def diagnose(layout, units, since):
    path = newest_log(layout.log_dir, since) if layout.device_unit in units else None
    journals = {}
    for unit in units:
        text = journal(unit, since, lines=200)
        journals[unit] = text.splitlines() if text else ["(no journal entries since the start)"]

    # The device log tells the story best. Each start writes a fresh one, so
    # read it whole: the cause is near the top, not in the tail.
    if path is not None:
        lines = read_tail(path, 20000)
        summarize(lines, os.path.basename(path))
    elif units:
        summarize([JOURNAL_PREFIX.sub("", line) for line in journals[units[0]]], "journalctl -u %s" % units[0])

    if path is not None:
        cause = next((f for f in tracebacks(lines) if not f.noise and not f.follow_on), None)
        if cause:
            show("%s: the cause's full traceback" % path, [cause.context] + cause.lines[1:])
        errors = error_lines(lines)[-15:]
        if errors:
            show("%s: last error lines (broker errors left out)" % path, errors)
        show("%s: last 25 lines" % path, lines[-25:])
    elif layout.device_unit in units:
        show("device software log", ["no log file in %s written since the start: the software died "
                                     "before it set up logging, so the journal is all there is"
                                     % layout.log_dir])

    # With a device log the journal is mostly startup chatter; without one it is all there is.
    for unit in units:
        count = 15 if path is not None else 40
        show("journalctl -u %s (since start, last %d lines)" % (unit, count), journals[unit][-count:])

    print("\nMore: journalctl -u %s -n 200 --no-pager   |   ls -t %s | head"
          % (" -u ".join(units) or layout.device_unit, layout.log_dir))


# --- filesync -------------------------------------------------------------------

def default_filesync(home):
    """One target, the GCS cloud syncer reached over RabbitMQ, as in
    holo_control_core/filesync/config.example.json.

    The login is a dummy on purpose: the device software connects in the
    background and keeps running when the login is refused, so this gets a
    device going until the real one is filled in (Admin tab > File sync). It
    is lowercase so it is not one of the example's placeholders, which the
    config check refuses outright.
    """
    return {
        "database_path": os.path.join(home, ".local", "state", "holo_gui", "filesync.sqlite3"),
        "targets": [
            {
                "name": "gcs",
                "type": "broker",
                "enabled": True,
                "host": GCS_BROKER_HOST,
                "port": GCS_BROKER_PORT,
                "username": "dummy_user",
                "password": "dummy_password",
            },
        ],
    }


def install_filesync(home, force=False):
    heading("File-sync config")
    if os.path.exists(FILESYNC_CONFIG) and not force:
        bad("%s already exists; left alone. Pass --force to replace it." % FILESYNC_CONFIG)
        targets = run(["cat", FILESYNC_CONFIG]).stdout
        try:
            names = ["%s (%s)" % (t.get("name"), t.get("type")) for t in json.loads(targets)["targets"]]
            info("its targets: %s" % ", ".join(names))
        except (ValueError, KeyError, TypeError):
            info("and it is not valid file-sync JSON")
        return False

    # The bridge runs as the device user and edits this file from the UI.
    owner = pwd.getpwuid(os.stat(home).st_uid).pw_name
    content = json.dumps(default_filesync(home), indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(content)
    try:
        proc = run(["install", "-D", "-m", "600", "-o", owner, f.name, FILESYNC_CONFIG])
    finally:
        os.unlink(f.name)
    if proc.returncode != 0:
        bad("could not write %s: %s" % (FILESYNC_CONFIG, proc.stdout.strip()))
        return False

    ok("wrote %s (owner %s, mode 600):" % (FILESYNC_CONFIG, owner))
    for line in content.splitlines():
        info("    " + line)
    info(color("the GCS login is a dummy: files will not reach the cloud until the real "
               "username and password are set (UI: Admin tab > File sync)", "33"))
    info("the device software reads this file only at startup: run `restart` to pick it up")
    return True


# --- install --------------------------------------------------------------------

def as_user(user, cmd):
    """cmd run as user, keeping the forwarded ssh agent so git can use it."""
    if pwd.getpwuid(os.geteuid()).pw_name == user:
        return cmd
    return ["sudo", "-u", user, "--preserve-env=SSH_AUTH_SOCK"] + cmd


def as_root(cmd):
    return cmd if os.geteuid() == 0 else ["sudo", "--preserve-env=SSH_AUTH_SOCK"] + cmd


def read_file(path):
    """The file's text, through sudo when it is not ours to read; None when missing."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except PermissionError:
        return run(["cat", path]).stdout


def saved_server(text):
    """(host, port) of the server link in install_device.sh's cloud.conf, or None
    when the text holds no link (a device installed --no-server)."""
    if not text or not re.search(r"^connection ", text, re.MULTILINE):
        return None
    match = re.search(r"^address\s+(\S+)", text, re.MULTILINE)
    if not match:
        return None
    host, _, port = match.group(1).partition(":")
    return host, int(port) if port.isdigit() else SERVER_PORT


def no_server_reason():
    """Why the installer should get --no-server, or None when the server answers.

    Without it, the installer ends by waiting for the server to accept the link
    and fails when it does not, as on a device with the VPN down."""
    if not any(glob.glob(pattern) for pattern in VPN_CONFIGS):
        return "no OpenVPN config under /etc/openvpn, so nothing to link to the server with"
    server = saved_server(read_file(UPLINK_CONF)) or (SERVER_HOST, SERVER_PORT)
    if port_open(server[1], server[0]):
        return None
    return "the server %s:%d does not answer (VPN down?)" % server


# What vite.config.ts hashes into remote/ui/dist/.src-stamp when it builds the UI
# (remote/ui/src/lib/srcStamp.ts): this must stay the same algorithm.
UI_NOT_SHIPPED = re.compile(r"\.(test|cases)\.[a-z]+$")
UI_OUTSIDE_SRC = ["index.html", "vite.config.ts", "package.json", "package-lock.json", ".env"]


def ui_stamp(ui_root):
    """The hash of the UI source the way the build stamps it into dist."""
    src = os.path.join(ui_root, "src")
    inputs = sorted(os.path.join(d, name) for d, _, names in os.walk(src) for name in names)
    inputs = [path for path in inputs if not UI_NOT_SHIPPED.search(path)]
    inputs += [os.path.join(ui_root, name) for name in UI_OUTSIDE_SRC]
    digest = hashlib.sha256()
    for path in inputs:
        if not os.path.exists(path):
            continue
        digest.update(os.path.relpath(path, ui_root).replace("\\", "/").encode())
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            digest.update(f.read().replace("\r\n", "\n").encode())
    return digest.hexdigest()


def check_ui_bundle(gui_dir):
    """Say whether the committed UI bundle, which is what the installer serves,
    was built from this checkout's UI source. False only when it was not."""
    ui_root = os.path.join(gui_dir, "remote", "ui")
    dist = os.path.join(ui_root, "dist")
    if not os.path.exists(os.path.join(dist, "index.html")):
        info(color("no UI bundle in remote/ui/dist: the installer builds one if this device has npm, "
                   "and fails if not", "33"))
        return True
    try:
        with open(os.path.join(dist, ".src-stamp")) as f:
            built_from = f.read().strip()
    except OSError:
        info(color("remote/ui/dist has no .src-stamp, so whether it matches remote/ui/src is unknown", "33"))
        return True
    if built_from != ui_stamp(ui_root):
        bad("remote/ui/dist was not built from this checkout's remote/ui/src: the device would serve "
            "the UI of an older build. Run `npm run build` in remote/ui and commit dist, "
            "or pass --stale-ui to install it anyway")
        return False
    ok("remote/ui/dist was built from this checkout's remote/ui/src")
    return True


def git_out(owner, gui_dir, *args):
    proc = subprocess.run(as_user(owner, ["git", "-C", gui_dir] + list(args)),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def describe_checkout(gui_dir, owner):
    """Print what is about to be installed; returns the branch, None when HEAD is detached."""
    branch = git_out(owner, gui_dir, "rev-parse", "--abbrev-ref", "HEAD")
    head = git_out(owner, gui_dir, "log", "-1", "--format=%h %s")
    changed = git_out(owner, gui_dir, "status", "--porcelain", "--untracked-files=no")
    info("%s: %s, at %s" % (gui_dir, "detached HEAD" if branch == "HEAD" else "branch " + str(branch), head))
    if changed:
        info(color("%d uncommitted change(s), installed as they are" % len(changed.splitlines()), "33"))
    return None if branch == "HEAD" else branch


def install_command(gui_dir, owner, server, extra, branch=None, https=False):
    cmd = [os.path.join(gui_dir, INSTALLER), "--gui-dir", gui_dir, "--gui-user", owner, "--headless"]
    if branch:
        cmd += ["--branch", branch]
        if https:
            cmd.append("--https")
    if not server:
        cmd.append("--no-server")
    return as_root(cmd + extra)


def install(home, server, full, https, stale_ui, extra, timeout_s):
    owner = pwd.getpwuid(os.stat(home).st_uid).pw_name
    gui_dir = os.path.join(home, "gui_new")

    heading("Before installing")
    if not os.path.exists(os.path.join(gui_dir, INSTALLER)):
        bad("no checkout with %s in %s: clone the branch there first" % (INSTALLER, gui_dir))
        return 1
    branch = describe_checkout(gui_dir, owner)
    if not full and not os.path.exists(os.path.join(gui_dir, "venv", "bin", "python")):
        bad("%s has no venv, so the device software was never installed from it: pass --full" % gui_dir)
        return 1
    if full:
        if branch is None:
            bad("--full needs a branch checked out: the installer checks it out again by name")
            return 1
        behind = git_out(owner, gui_dir, "rev-list", "--count", "HEAD..@{u}")
        info(color("--full: the installer fetches and fast-forwards %s to origin/%s if that is ahead%s"
                   % (branch, branch, " (it already is, by %s commit(s))" % behind if behind not in (None, "0")
                      else ""), "33"))
        if not https and not os.environ.get("SSH_AUTH_SOCK"):
            info(color("no forwarded ssh agent: the installer's git needs a key of %s's own "
                       "(else log in with `ssh -A`)" % owner, "33"))
    if not check_ui_bundle(gui_dir) and not stale_ui:
        return 1
    for path, consequence in ((DEVICE_CONFIG, "the device software raises on startup without it"),
                              (FILESYNC_CONFIG, "the device software will not start; `filesync` writes one")):
        if read_file(path) is None:
            bad("no %s: %s" % (path, consequence))
    if server is None:
        reason = no_server_reason()
        server = reason is None
        info("server link: " + ("the server answers, so the install ends by checking the link" if server
                                else color("--no-server, because " + reason, "33")))

    cmd = install_command(gui_dir, owner, server, extra, branch if full else None, https)
    heading("install_device.sh")
    info("$ " + " ".join(cmd))
    print(flush=True)
    started_at = time.time()
    code = subprocess.run(cmd, cwd=gui_dir).returncode

    layout = Layout("new", home, [])
    if code != 0:
        print("\n" + color("install_device.sh FAILED (exit %d); its last FAIL line above names the step."
                           % code, "1;31"))
        if server:
            info("if that was the server link check (uplink reads 0): `install --no-server` skips it")
        props = unit_props(layout.device_unit)
        if full and props["LoadState"] == "loaded" and props["ActiveState"] != "active":
            diagnose(layout, [layout.device_unit], started_at)
        return code
    if read_file(FILESYNC_CONFIG) is None:
        info("no %s, so the device software stays stopped; nothing more to check" % FILESYNC_CONFIG)
        return 0

    if full:
        # The installer started the device software and waited only for its API;
        # whether the hardware came up is in the log.
        heading("Checking the start")
        restarts_before = dict((unit, unit_props(unit)["NRestarts"]) for unit in layout.units)
        return 0 if verify(layout, checks_for(layout, restarts_before), timeout_s, started_at) else 1
    # Without --branch the installer leaves the device software running as it
    # was, on the Python it loaded at its last start.
    if not stop(layout):
        bad("not starting: the old processes are still there, and two copies must never drive the hardware")
        return 1
    success, _ = start(layout, timeout_s)
    return 0 if success else 1


# --- main -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["status", "stop", "restart", "filesync", "install"])
    parser.add_argument("--style", choices=["old", "new"],
                        help="skip detection and treat the device as this version")
    parser.add_argument("--home", default=DEFAULT_HOME, help="home of the device user (default %(default)s)")
    parser.add_argument("--timeout", type=int, default=90,
                        help="seconds to wait for a start to settle (default %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="filesync: replace an existing %s" % FILESYNC_CONFIG)
    server = parser.add_mutually_exclusive_group()
    server.add_argument("--no-server", dest="server", action="store_false", default=None,
                        help="install: pass --no-server (default: only when the server does not answer)")
    server.add_argument("--with-server", dest="server", action="store_true",
                        help="install: never pass --no-server")
    parser.add_argument("--full", action="store_true",
                        help="install: the device software too (venv, packages, unit); needs git access")
    parser.add_argument("--https", action="store_true", help="install --full: fetch over https")
    parser.add_argument("--stale-ui", action="store_true",
                        help="install: go ahead when remote/ui/dist was built from other source")
    args, extra = parser.parse_known_args()
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if extra and args.action != "install":
        parser.error("unrecognized arguments: %s" % " ".join(extra))

    # Both versions read the same file, so this needs no detection.
    if args.action == "filesync":
        return 0 if install_filesync(args.home, args.force) else 1
    # The installer stops whichever version runs today itself.
    if args.action == "install":
        return install(args.home, args.server, args.full, args.https, args.stale_ui, extra, args.timeout)

    heading("Detecting version")
    style, evidence = detect(args.home)
    for line in evidence:
        info(line)
    if args.style:
        if style not in (None, args.style):
            info(color("detected %s, but --style %s given; using %s" % (style, args.style, args.style), "33"))
        style = args.style
    if style is None:
        bad("could not tell which version is installed; pass --style old|new")
        return 2
    if style == "conflict":
        bad("old and new software both running; pass --style to pick the one to keep, "
            "then `stop` cleans up the other's processes too")
        return 2

    layout = Layout(style, args.home, evidence)
    print("\n  -> %s-style install in %s; units: %s"
          % (color(style, "1"), layout.gui_dir, ", ".join(layout.units + layout.infra_units)))

    if args.action == "status":
        heading("Status")
        for unit in layout.units + layout.infra_units:
            props = unit_props(unit)
            line = "%-12s %s/%s, enabled: %s, restarts: %s" % (unit, props["ActiveState"], props["SubState"],
                                                               props["UnitFileState"], props["NRestarts"])
            (ok if props["ActiveState"] == "active" else bad)(line)
        for pid, cmdline, _ in device_software_procs():
            info("pid %d: %s" % (pid, cmdline))
        return 0

    stopped = stop(layout)
    if args.action == "stop":
        return 0 if stopped else 1
    if not stopped:
        bad("not starting: the old processes are still there, and two copies must never drive the hardware")
        return 1
    success, _ = start(layout, args.timeout)
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
