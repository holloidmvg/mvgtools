#!/usr/bin/env python3
"""Round trips of proto_switch.py on a scratch tree laid out like a device.
Standard library only: python3 -m unittest test_proto_switch
"""
import contextlib
import io

import json
import os
import shutil
import tarfile
import tempfile
import unittest

import proto_switch as ps


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def read(path):
    with open(path) as f:
        return f.read()


def make_tar(path, members):
    with tarfile.open(path, "w") as tar:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))


class Device(unittest.TestCase):
    """A device in its main configuration, with a configuration symlink and a plain tailoring file."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.paths = ps.Paths(self.root)
        self.ssh = self.live("/etc/ssh")
        self.holloid = self.live("/etc/holloid")
        write(self.ssh + "/ssh_host_ed25519_key", "main ed25519\n", 0o600)
        write(self.ssh + "/ssh_host_ed25519_key.pub", "main ed25519 pub\n")
        write(self.ssh + "/ssh_host_rsa_key", "main rsa\n", 0o600)
        write(self.ssh + "/ssh_host_rsa_key.pub", "main rsa pub\n")
        write(self.ssh + "/sshd_config", "config\n")
        make_tar(self.ssh + "/proto_keys.tar", {"./ssh_host_ed25519_key": "proto ed25519\n",
                                                "./ssh_host_ed25519_key.pub": "proto ed25519 pub\n"})
        write(self.holloid + "/deployment.json", '{"device_model": "cleaning"}\n')
        write(self.holloid + "/2p41_configuration_20260417.json", '{"version": 5}\n')
        os.symlink("2p41_configuration_20260417.json", self.holloid + "/configuration.json")
        write(self.holloid + "/tailoring.json", '{"dilution": false}\n')
        write(self.holloid + "/filesync.json", "{}\n")
        write(self.live("/home/mage/.ssh/authorized_keys"), "ssh-ed25519 OWNER\n", 0o600)
        write(self.live("/home/mage/.ssh/fresh_keys"), "ssh-ed25519 FACTORY\n", 0o600)
        self.snapshot = self.tree()

    def live(self, path):
        return self.paths.live(path)

    def tree(self):
        """Everything outside the backup, as {path: content or '-> target'}."""
        found = {}
        backup = self.paths.backup_dir()
        for dirpath, dirnames, filenames in os.walk(self.root):
            if dirpath.startswith(backup):
                continue
            for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, self.root)
                if os.path.islink(path):
                    found[rel] = "-> " + os.readlink(path)
                elif not rel.endswith(".tar"):
                    found[rel] = read(path)
        return found

    def run_quiet(self, fn, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = fn(self.paths, *args)
        return result, out.getvalue()

    def record(self, force=False):
        return self.run_quiet(ps.record, force)

    def proto(self, force=False):
        return self.run_quiet(ps.to_proto, force, False)

    def main(self, force=False):
        return self.run_quiet(ps.to_main, force, False)


class RoundTrip(Device):
    def test_proto_state(self):
        self.record()
        self.assertTrue(self.proto()[0])
        self.assertEqual(json.loads(read(self.holloid + "/deployment.json")), {})
        self.assertFalse(os.path.lexists(self.holloid + "/configuration.json"))
        self.assertFalse(os.path.lexists(self.holloid + "/tailoring.json"))
        # The symlink's target and unrelated files are left alone.
        self.assertTrue(os.path.exists(self.holloid + "/2p41_configuration_20260417.json"))
        self.assertTrue(os.path.exists(self.holloid + "/filesync.json"))
        self.assertEqual(sorted(n for n in os.listdir(self.ssh) if n.startswith("ssh_host")),
                         ["ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"])
        self.assertEqual(read(self.ssh + "/ssh_host_ed25519_key"), "proto ed25519\n")
        self.assertEqual(os.stat(self.ssh + "/ssh_host_ed25519_key").st_mode & 0o777, 0o600)
        self.assertEqual(read(self.live("/home/mage/.ssh/authorized_keys")), "ssh-ed25519 FACTORY\n")
        self.assertEqual(ps.read_state(self.paths), ps.PROTO)

    def test_main_restores_everything(self):
        self.record()
        self.proto()
        self.assertTrue(self.main()[0])
        self.assertEqual(self.tree(), self.snapshot)
        self.assertTrue(os.path.islink(self.holloid + "/configuration.json"))
        self.assertEqual(os.stat(self.ssh + "/ssh_host_rsa_key").st_mode & 0o777, 0o600)
        self.assertEqual(ps.read_state(self.paths), ps.MAIN)

    def test_absent_file_stays_absent(self):
        os.unlink(self.holloid + "/tailoring.json")
        snapshot = self.tree()
        self.record()
        self.proto()
        self.main()
        self.assertEqual(self.tree(), snapshot)

    def test_switching_twice_is_harmless(self):
        self.record()
        self.proto()
        self.proto()
        self.main()
        self.main()
        self.assertEqual(self.tree(), self.snapshot)


class Guards(Device):
    def test_proto_without_record_refused(self):
        with self.assertRaises(ps.Refused):
            self.proto()
        self.assertEqual(self.tree(), self.snapshot)

    def test_record_in_proto_state_refused(self):
        self.record()
        self.proto()
        with self.assertRaises(ps.Refused):
            self.record()
        self.main()
        self.assertEqual(self.tree(), self.snapshot)

    def test_record_with_proto_keys_refused(self):
        # No state file to go by, but the keys give it away.
        for name in os.listdir(self.ssh):
            if name.startswith("ssh_host"):
                os.unlink(os.path.join(self.ssh, name))
        write(self.ssh + "/ssh_host_ed25519_key", "proto ed25519\n", 0o600)
        with self.assertRaises(ps.Refused):
            self.record()

    def test_unrecorded_change_blocks_proto(self):
        self.record()
        write(self.holloid + "/tailoring.json", '{"dilution": true}\n')
        with self.assertRaises(ps.Refused) as caught:
            self.proto()
        self.assertIn("tailoring.json", str(caught.exception))
        self.assertTrue(os.path.lexists(self.holloid + "/configuration.json"))
        self.assertTrue(self.proto(force=True)[0])

    def test_rerecord_keeps_previous(self):
        self.record()
        write(self.holloid + "/tailoring.json", '{"dilution": true}\n')
        self.record()
        self.assertTrue(os.path.isdir(self.paths.backup_dir() + ".previous"))
        self.proto()
        self.main()
        self.assertEqual(read(self.holloid + "/tailoring.json"), '{"dilution": true}\n')

    def test_missing_fresh_keys_changes_nothing(self):
        self.record()
        os.unlink(self.live("/home/mage/.ssh/fresh_keys"))
        with self.assertRaises(ps.Refused):
            self.proto()
        del self.snapshot["home/mage/.ssh/fresh_keys"]
        self.assertEqual(self.tree(), self.snapshot)

    def test_damaged_backup_changes_nothing(self):
        self.record()
        self.proto()
        before = self.tree()
        write(self.paths.saved("/etc/ssh/ssh_host_rsa_key"), "garbage\n")
        with self.assertRaises(ps.Refused):
            self.main()
        self.assertEqual(self.tree(), before)

    def test_tar_with_foreign_member_refused(self):
        self.record()
        make_tar(self.ssh + "/proto_keys.tar", {"ssh_host_ed25519_key": "k\n", "../../etc/passwd": "x\n"})
        with self.assertRaises(ps.Refused):
            self.proto()
        self.assertEqual(read(self.ssh + "/ssh_host_ed25519_key"), "main ed25519\n")


if __name__ == "__main__":
    unittest.main()
