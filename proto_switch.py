#!/usr/bin/env python3
"""Switch a device between its own identity ("main") and the prototype one.

In the prototype state the device looks like an uninitialised device: it has
the prototype ssh host keys, an empty deployment, no configuration or
tailoring, and the fresh-device authorized_keys. The main state is whatever
the device had when `record` was last run.

usage: proto_switch.py record [--force]   # back up the main configuration
       proto_switch.py proto  [--force]   # switch to the prototype configuration
       proto_switch.py main   [--force]   # restore the recorded main configuration
       proto_switch.py status             # say which configuration is active

What is switched:
  /etc/ssh/ssh_host*key*         main: as recorded; proto: unpacked from /etc/ssh/proto_keys.tar
  /etc/holloid/deployment.json   main: as recorded; proto: an empty JSON object
  /etc/holloid/configuration.json, tailoring.json
                                 main: as recorded, file or symlink; proto: absent
  ~mage/.ssh/authorized_keys     main: as recorded; proto: a copy of ~mage/.ssh/fresh_keys

Run as root. Uses only the standard library so it runs on the device's system
python.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile

SSH_DIR = "/etc/ssh"
HOST_KEY_PATTERN = "ssh_host*key*"
PROTO_KEYS_TAR = "/etc/ssh/proto_keys.tar"

HOLLOID_DIR = "/etc/holloid"
DEPLOYMENT = HOLLOID_DIR + "/deployment.json"
CONFIGURATION = HOLLOID_DIR + "/configuration.json"
TAILORING = HOLLOID_DIR + "/tailoring.json"
# What deployment.json holds in the prototype state: valid JSON, nothing in it.
PROTO_DEPLOYMENT = b"{}\n"

DEFAULT_HOME = "/home/mage"
# Outside /etc/holloid on purpose, so nothing that reads that directory ever
# sees the backup. It holds private host keys, so only root may read it.
DEFAULT_BACKUP = "/var/backups/holloid-main"

MANIFEST = "manifest.json"
STATE = "state"
# The state file says which configuration is live. "partial" means a switch
# started and did not finish: the live files are a mix of both.
MAIN, PROTO, PARTIAL = "main", "proto", "partial"

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


class Refused(Exception):
    """A precondition failed before anything was changed."""


# --- paths ----------------------------------------------------------------------

class Paths:
    """Device paths, all resolved under `root` so the tests can use a scratch tree.

    Paths are written as they are on the device ("/etc/ssh/..."); live() turns
    one into the file to touch and saved() into its copy in the backup.
    """

    def __init__(self, root="/", home=DEFAULT_HOME, backup=DEFAULT_BACKUP):
        self.root = root
        self.home = home
        self.backup = backup
        self.authorized_keys = home + "/.ssh/authorized_keys"
        self.fresh_keys = home + "/.ssh/fresh_keys"

    def live(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def backup_dir(self):
        return self.live(self.backup)

    def saved(self, path):
        return os.path.join(self.backup_dir(), "files", path.lstrip("/"))

    def on_device(self):
        return os.path.abspath(self.root) == "/"


def host_keys(paths):
    """The host key files that are live now, as device paths."""
    ssh_dir = paths.live(SSH_DIR)
    try:
        names = os.listdir(ssh_dir)
    except FileNotFoundError:
        return []
    return sorted(SSH_DIR + "/" + name for name in names
                  if fnmatch.fnmatch(name, HOST_KEY_PATTERN)
                  and (os.path.isfile(os.path.join(ssh_dir, name))
                       or os.path.islink(os.path.join(ssh_dir, name))))


def recorded_items(paths):
    """Everything `record` saves, besides the host keys."""
    return [DEPLOYMENT, CONFIGURATION, TAILORING, paths.authorized_keys]


# --- reading the live state ---------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def describe(live_path):
    """What is at live_path: a symlink (with its target), a file, or nothing.

    A symlink is described as a link and never followed, because restoring it
    means restoring the link, not a copy of whatever it points at.
    """
    if os.path.islink(live_path):
        return {"kind": "symlink", "target": os.readlink(live_path)}
    if not os.path.lexists(live_path):
        return {"kind": "absent"}
    st = os.stat(live_path)
    if not stat.S_ISREG(st.st_mode):
        raise Refused("%s is neither a file nor a symlink; sort it out by hand first" % live_path)
    return {"kind": "file", "sha256": sha256_file(live_path), "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid, "gid": st.st_gid}


def same(entry, now):
    """Whether two descriptions are the same content. Owner and mode don't count."""
    if entry["kind"] != now["kind"]:
        return False
    if entry["kind"] == "symlink":
        return entry["target"] == now["target"]
    if entry["kind"] == "file":
        return entry["sha256"] == now["sha256"]
    return True


def summary(entry):
    if entry["kind"] == "symlink":
        return "symlink -> %s" % entry["target"]
    if entry["kind"] == "file":
        return "file %s" % entry["sha256"][:12]
    return "absent"


def differences(paths, manifest):
    """How the live files differ from the recording, one line per path."""
    diffs = []
    for path, entry in manifest["items"].items():
        now = describe(paths.live(path))
        if not same(entry, now):
            diffs.append("%s: recorded %s, now %s" % (path, summary(entry), summary(now)))
    for path in host_keys(paths):
        if path not in manifest["items"]:
            diffs.append("%s: not recorded, now %s" % (path, summary(describe(paths.live(path)))))
    return diffs


def read_state(paths):
    try:
        with open(os.path.join(paths.backup_dir(), STATE)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def write_state(paths, state):
    write_file(os.path.join(paths.backup_dir(), STATE), (state + "\n").encode(), 0o600, 0, 0)


def load_manifest(paths):
    try:
        with open(os.path.join(paths.backup_dir(), MANIFEST)) as f:
            return json.load(f)
    except FileNotFoundError:
        raise Refused("no recording in %s: run `record` while the device is in its main "
                      "configuration first" % paths.backup_dir())


def proto_keys(paths):
    """The host keys in proto_keys.tar as {name: bytes}.

    The archive is read member by member rather than extracted, so a stray
    directory, link or path in it can never land anywhere but /etc/ssh.
    """
    tar_path = paths.live(PROTO_KEYS_TAR)
    if not os.path.isfile(tar_path):
        raise Refused("%s is missing; the prototype host keys come from there" % tar_path)
    keys = {}
    try:
        with tarfile.open(tar_path) as tar:
            for member in tar.getmembers():
                name = os.path.normpath(member.name)
                if member.isdir() and name == ".":
                    continue
                if not member.isfile() or "/" in name or not fnmatch.fnmatch(name, HOST_KEY_PATTERN):
                    raise Refused("%s holds %r, which is not an ssh_host*key* file; "
                                  "it should hold only the host keys" % (tar_path, member.name))
                keys[name] = tar.extractfile(member).read()
    except tarfile.TarError as e:
        raise Refused("cannot read %s: %s" % (tar_path, e))
    if not any(not name.endswith(".pub") for name in keys):
        raise Refused("%s holds no private host key" % tar_path)
    return keys


def looks_like_proto(paths):
    """Whether the live host keys are the prototype ones. None when there is no archive to compare."""
    try:
        proto = set(hashlib.sha256(data).hexdigest() for data in proto_keys(paths).values())
    except Refused:
        return None
    live = [describe(paths.live(path)) for path in host_keys(paths)]
    return bool(live) and all(entry.get("sha256") in proto for entry in live)


# --- writing --------------------------------------------------------------------

def chown(path, uid, gid):
    # Only root can give files away. On a device the script runs as root; the
    # tests run as a normal user on a scratch tree, where ownership is moot.
    if os.geteuid() == 0:
        os.chown(path, uid, gid, follow_symlinks=False)


def write_file(path, data, mode, uid, gid):
    """Replace path with data in one step, so it is never seen half-written."""
    tmp = "%s.proto_switch.tmp" % path
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_symlink(path, target):
    tmp = "%s.proto_switch.tmp" % path
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)


def remove(path):
    if os.path.lexists(path):
        os.unlink(path)


def restore(paths, path, entry):
    """Put one recorded item back as it was."""
    live = paths.live(path)
    if entry["kind"] == "absent":
        remove(live)
    elif entry["kind"] == "symlink":
        write_symlink(live, entry["target"])
    else:
        with open(paths.saved(path), "rb") as f:
            write_file(live, f.read(), entry["mode"], entry["uid"], entry["gid"])


def sync_dir(path):
    """Flush a directory's entries to disk; a Pi is often switched off at the plug."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- sshd -----------------------------------------------------------------------

def reload_sshd(paths):
    """Make sshd serve the host keys now on disk. Open sessions stay up."""
    heading("sshd")
    test = subprocess.run(["sshd", "-t"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if test.returncode != 0:
        bad("`sshd -t` rejects the new setup, so sshd was not reloaded:\n%s" % test.stdout.strip())
        info("keep this session open until it is fixed: the running sshd still has the old keys")
        return False
    for unit in ("ssh", "sshd"):
        if subprocess.run(["systemctl", "reload", unit], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            ok("reloaded %s" % unit)
            break
    else:
        bad("could not reload ssh or sshd; run `systemctl reload ssh` before you log out")
        return False
    for path in host_keys(paths):
        if path.endswith(".pub"):
            out = subprocess.run(["ssh-keygen", "-lf", paths.live(path)], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True).stdout.strip()
            if out:
                info(out)
    info("clients that knew the other host key will warn that it changed; on the client, "
         "`ssh-keygen -R <host>` forgets the old one")
    return True


# --- commands -------------------------------------------------------------------

def record(paths, force):
    heading("Recording the main configuration")
    state = read_state(paths)
    if state in (PROTO, PARTIAL) and not force:
        raise Refused("the device is in the %s state, so recording now would overwrite the main "
                      "backup with something else; run `main` first, or pass --force if you "
                      "really mean to record what is live" % state)
    if looks_like_proto(paths) and not force:
        raise Refused("the live host keys are the prototype ones, so this is not the main "
                      "configuration; run `main` first, or pass --force")
    keys = host_keys(paths)
    if not any(not key.endswith(".pub") for key in keys):
        raise Refused("no private host key in %s, nothing to record" % paths.live(SSH_DIR))

    # Build the new backup next to the old one and swap it in at the end, so
    # a failed record leaves the last good backup as it was.
    final = paths.backup_dir()
    new = final + ".new"
    previous = final + ".previous"
    if os.path.lexists(new):
        shutil.rmtree(new)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    os.mkdir(new, 0o700)

    items = {}
    staged = Paths(paths.root, paths.home, paths.backup + ".new")
    for path in keys + recorded_items(paths):
        entry = describe(paths.live(path))
        items[path] = entry
        if entry["kind"] == "file":
            dest = staged.saved(path)
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            shutil.copyfile(paths.live(path), dest)
            os.chmod(dest, 0o600)
            if sha256_file(dest) != entry["sha256"]:
                raise Refused("%s changed while it was copied; run `record` again" % path)
        (ok if entry["kind"] != "absent" else info)("%-40s %s" % (path, summary(entry)))
    if items[paths.authorized_keys]["kind"] == "absent":
        info(color("%s does not exist; `main` will leave the device without it"
                   % paths.authorized_keys, "33"))

    manifest = {"recorded": datetime.datetime.now().isoformat(timespec="seconds"),
                "host": socket.gethostname(), "items": items}
    write_file(os.path.join(new, MANIFEST), (json.dumps(manifest, indent=2) + "\n").encode(), 0o600, 0, 0)
    write_file(os.path.join(new, STATE), (MAIN + "\n").encode(), 0o600, 0, 0)

    if os.path.lexists(previous):
        shutil.rmtree(previous)
    if os.path.lexists(final):
        os.rename(final, previous)
    os.rename(new, final)
    sync_dir(os.path.dirname(final))
    ok("saved in %s%s" % (final, " (the one before it is in %s)" % previous if os.path.exists(previous) else ""))
    return True


def check_unsaved(paths, manifest, action, force):
    """Refuse to overwrite a main configuration that changed since `record`."""
    if read_state(paths) != MAIN:
        return
    diffs = differences(paths, manifest)
    if not diffs:
        return
    if force:
        info(color("discarding changes made since `record`, as --force says:", "33"))
        for line in diffs:
            info("  " + line)
        return
    raise Refused("the main configuration changed since it was recorded (%s), and `%s` would "
                  "lose that:\n      %s\n    run `record` to keep the changes, or pass --force to "
                  "discard them" % (manifest["recorded"], action, "\n      ".join(diffs)))


def to_proto(paths, force, reload):
    manifest = load_manifest(paths)
    check_unsaved(paths, manifest, "proto", force)
    # Read everything the switch needs before changing anything.
    keys = proto_keys(paths)
    fresh = paths.live(paths.fresh_keys)
    try:
        with open(fresh, "rb") as f:
            fresh_data = f.read()
    except FileNotFoundError:
        raise Refused("%s is missing; the prototype authorized_keys is copied from it" % fresh)
    if not fresh_data.strip():
        raise Refused("%s is empty; with it nobody could log in by key" % fresh)
    ssh_user_dir = os.stat(os.path.dirname(paths.live(paths.authorized_keys)))

    heading("Switching to the prototype configuration")
    write_state(paths, PARTIAL)

    deployment = paths.live(DEPLOYMENT)
    now = describe(deployment)
    owner = now if now["kind"] == "file" else manifest["items"][DEPLOYMENT]
    if owner["kind"] != "file":
        owner = {"mode": 0o644, "uid": 0, "gid": 0}
    write_file(deployment, PROTO_DEPLOYMENT, owner["mode"], owner["uid"], owner["gid"])
    ok("%s: empty" % DEPLOYMENT)
    for path in (CONFIGURATION, TAILORING):
        existed = os.path.lexists(paths.live(path))
        remove(paths.live(path))
        ok("%s: %s" % (path, "removed" if existed else "absent already"))
    sync_dir(paths.live(HOLLOID_DIR))

    # The new keys go in before the old ones go, so /etc/ssh never lacks one.
    for name, data in sorted(keys.items()):
        write_file(paths.live(SSH_DIR + "/" + name), data, 0o644 if name.endswith(".pub") else 0o600, 0, 0)
    for path in host_keys(paths):
        if os.path.basename(path) not in keys:
            remove(paths.live(path))
    sync_dir(paths.live(SSH_DIR))
    ok("host keys: %s" % ", ".join(sorted(keys)))

    # Last, so the way back in by key is the step most likely to have run.
    write_file(paths.live(paths.authorized_keys), fresh_data, 0o600, ssh_user_dir.st_uid, ssh_user_dir.st_gid)
    sync_dir(os.path.dirname(paths.live(paths.authorized_keys)))
    ok("%s: copied from %s" % (paths.authorized_keys, os.path.basename(paths.fresh_keys)))

    write_state(paths, PROTO)
    return reload_sshd(paths) if reload else True


def to_main(paths, force, reload):
    manifest = load_manifest(paths)
    check_unsaved(paths, manifest, "main", force)
    # Check the whole backup before changing anything.
    for path, entry in manifest["items"].items():
        if entry["kind"] != "file":
            continue
        saved = paths.saved(path)
        if not os.path.isfile(saved) or sha256_file(saved) != entry["sha256"]:
            raise Refused("the backup of %s (%s) is missing or damaged; nothing was changed" % (path, saved))

    heading("Restoring the main configuration recorded %s" % manifest["recorded"])
    write_state(paths, PARTIAL)
    recorded_keys = [path for path in manifest["items"] if path.startswith(SSH_DIR + "/")]
    # authorized_keys goes last, so the way back in by key is the step most
    # likely to have run.
    for path in sorted(manifest["items"], key=lambda p: p == paths.authorized_keys):
        restore(paths, path, manifest["items"][path])
        if path not in recorded_keys:
            ok("%-40s %s" % (path, summary(manifest["items"][path])))
    for path in host_keys(paths):
        if path not in manifest["items"]:
            remove(paths.live(path))
    for directory in (HOLLOID_DIR, SSH_DIR, os.path.dirname(paths.authorized_keys)):
        if os.path.isdir(paths.live(directory)):
            sync_dir(paths.live(directory))
    ok("host keys: %s" % ", ".join(os.path.basename(p) for p in recorded_keys))

    write_state(paths, MAIN)
    return reload_sshd(paths) if reload else True


def status(paths):
    heading("Status")
    state = read_state(paths)
    try:
        manifest = load_manifest(paths)
    except Refused as e:
        bad(str(e))
        return False
    info("recorded:  %s on %s, in %s" % (manifest["recorded"], manifest.get("host", "?"), paths.backup_dir()))
    info("state:     %s" % {MAIN: "main", PROTO: "prototype",
                            PARTIAL: color("partial: a switch did not finish; run `main` or `proto` again", "33")
                            }.get(state, state))
    proto = looks_like_proto(paths)
    if proto is not None:
        info("host keys: %s" % ("the prototype ones" if proto else "not the prototype ones"))
    diffs = differences(paths, manifest)
    if state == MAIN:
        if diffs:
            info(color("changed since `record`:", "33"))
            for line in diffs:
                info("  " + line)
        else:
            ok("live files match the recording")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["record", "proto", "main", "status"])
    parser.add_argument("--force", action="store_true",
                        help="record: even in the prototype state; proto/main: discard changes made "
                             "to the main configuration since `record`")
    parser.add_argument("--no-reload", dest="reload", action="store_false",
                        help="proto/main: leave sshd alone (it keeps serving the old host keys)")
    parser.add_argument("--home", default=DEFAULT_HOME, help="home of the device user (default %(default)s)")
    parser.add_argument("--backup", default=DEFAULT_BACKUP, help="where `record` keeps the main "
                        "configuration (default %(default)s)")
    parser.add_argument("--root", default="/", help=argparse.SUPPRESS)  # for the tests
    args = parser.parse_args()

    paths = Paths(args.root, args.home, args.backup)
    if paths.on_device() and os.geteuid() != 0:
        bad("run as root (sudo %s %s): the files belong to root" % (sys.argv[0], args.action))
        return 1
    reload = args.reload and paths.on_device()
    try:
        if args.action == "record":
            done = record(paths, args.force)
        elif args.action == "proto":
            done = to_proto(paths, args.force, reload)
        elif args.action == "main":
            done = to_main(paths, args.force, reload)
        else:
            done = status(paths)
    except Refused as e:
        bad(str(e))
        return 2
    return 0 if done else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
