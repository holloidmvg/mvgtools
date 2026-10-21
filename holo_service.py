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

Uses only the standard library so it runs on the device's system python.
Run as root or a sudoer: systemctl and journalctl are called through sudo
when needed.
"""
import argparse
import glob
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

# The device API must answer this many checks in a row, without a restart in
# between, before the start counts. A config error often lets the process come
# up and die a few seconds later.
STABLE_CHECKS = 3
CHECK_INTERVAL_S = 2

ERROR_LINE = re.compile(r"Traceback|Error|Exception|CRITICAL|FATAL|ERROR|failed", re.IGNORECASE)

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

    checks = checks_for(layout, restarts_before)
    success = wait_for(checks, timeout_s)

    heading("Result")
    for check in checks:
        (ok if check.passed else bad)("%-22s %s" % (check.name, check.detail))
    if success:
        print("\n" + color("Started: every check passed %d times in a row." % STABLE_CHECKS, "1;32"))
        return True, started_at

    print("\n" + color("Start FAILED after %ds." % timeout_s, "1;31"))
    failed_units = []
    for check in checks:
        if not check.passed and check.unit and check.unit not in failed_units:
            failed_units.append(check.unit)
    diagnose(layout, failed_units, started_at)
    return False, started_at


# --- diagnosis ------------------------------------------------------------------

def journal(unit, since, lines=40):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since - 1))
    proc = run(["journalctl", "-u", unit, "--since", stamp, "--no-pager", "-o", "short-iso", "-n", str(lines)])
    return proc.stdout.rstrip()


def newest_log(log_dir, since):
    logs = [p for p in glob.glob(os.path.join(log_dir, "log_*.log*")) if os.path.getmtime(p) >= since - 2]
    return max(logs, key=os.path.getmtime) if logs else None


def read_tail(path, lines):
    try:
        with open(path, errors="replace") as f:
            return f.read().splitlines()[-lines:]
    except PermissionError:
        return run(["tail", "-n", str(lines), path]).stdout.splitlines()


def last_traceback(lines):
    """The last 'Traceback ...' block through its exception line, if any."""
    start = None
    for i, line in enumerate(lines):
        if "Traceback (most recent call last)" in line:
            start = i
    if start is None:
        return []
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()
                                or "Traceback" in lines[end] or "During handling" in lines[end]):
        end += 1
    return lines[start:end + 1]


def show(title, lines):
    print("\n" + color("--- %s ---" % title, "1"))
    for line in lines:
        print("  " + (color(line, "31") if ERROR_LINE.search(line) else line))


def diagnose(layout, units, since):
    for unit in units:
        text = journal(unit, since)
        lines = text.splitlines() if text else ["(no journal entries since the start)"]
        tb = last_traceback(lines)
        if tb:
            show("%s: last traceback in the journal" % unit, tb)
        show("journalctl -u %s (since start, last 40 lines)" % unit, lines)

    if layout.device_unit in units:
        path = newest_log(layout.log_dir, since)
        if path is None:
            show("device software log", ["no log file in %s written since the start: the software died "
                                         "before it set up logging, so the journal above is all there is"
                                         % layout.log_dir])
        else:
            lines = read_tail(path, 400)
            tb = last_traceback(lines)
            if tb:
                show("%s: last traceback" % path, tb)
            errors = [l for l in lines if ERROR_LINE.search(l)][-15:]
            if errors:
                show("%s: last error lines" % path, errors)
            show("%s: last 25 lines" % path, lines[-25:])

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


# --- main -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["status", "stop", "restart", "filesync"])
    parser.add_argument("--style", choices=["old", "new"],
                        help="skip detection and treat the device as this version")
    parser.add_argument("--home", default=DEFAULT_HOME, help="home of the device user (default %(default)s)")
    parser.add_argument("--timeout", type=int, default=90,
                        help="seconds to wait for a start to settle (default %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="filesync: replace an existing %s" % FILESYNC_CONFIG)
    args = parser.parse_args()

    # Both versions read the same file, so this needs no detection.
    if args.action == "filesync":
        return 0 if install_filesync(args.home, args.force) else 1

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
