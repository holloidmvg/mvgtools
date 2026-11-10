# mvgtools

Utilities for working with Holloid devices and the `holo-control` device
software, kept outside `holo-control` so they never mix with its history.

| File | What it is |
|---|---|
| [`holo_service.py`](holo_service.py) | Stop, restart, check, configure and install the device software on a device |
| [`test_holo_service.py`](test_holo_service.py) | Unit tests for `holo_service.py`, run against the logs in [`testdata/`](testdata) |
| [`proto_switch.py`](proto_switch.py) | Switch a device between its own identity and the prototype (uninitialised) one |
| [`test_proto_switch.py`](test_proto_switch.py) | Unit tests for `proto_switch.py`, run on a scratch tree laid out like a device |
| [`fixgeany.py`](fixgeany.py) | Sets up the Geany editor the way I like it (font, colours, keybindings) |

Everything uses only the Python standard library.

---

## holo_service.py

Stops, restarts and installs the device software, whichever version a device
runs, then tells you whether it really came up and, if not, why.

### The two layouts

| | old (`interim_main`) | new (`new-ui`) |
|---|---|---|
| Software directory | `~/gui` | `~/gui_new` |
| Device unit | `holo_gui`: runs `client_app.py`, which serves the Dash GUI on :8050 and forks the `hollohw` hardware process | `holo_gui`, or `holo_device` when installed `--headless` (device API on 127.0.0.1:1234) |
| Helper units | – | `holo_bridge` (:8080) |
| Infrastructure | – | `mosquitto` (:1883/:9001), `nginx` (:80) |
| Logs | `~/gui/log` | `~/gui/log` (the same place) |

The layout is detected in this order: what is actually running, then which
systemd units are installed and where they point, then whether `~/gui_new`
exists. Running processes come first because `install_device.sh --no-start`
can stage `~/gui_new` on a device that still runs the old software. If both
versions appear to be running, the script refuses to start anything: two
copies must never drive the hardware at once. Use `--style old|new` to skip
detection.

### Usage

Copy the single file to the device and run it there with the system
`python3`. It needs no virtualenv or packages.

```
holo_service.py status   [--style old|new] [--home DIR]
holo_service.py stop     [--style old|new] [--home DIR]
holo_service.py restart  [--style old|new] [--home DIR] [--timeout S]
holo_service.py filesync [--home DIR] [--force]
holo_service.py install  [--no-server | --with-server] [--full [--https]] [--stale-ui]
                         [--home DIR] [--timeout S] [-- INSTALLER ARGS]
```

`--home` is the device user's home (default `/home/mage`). `--timeout` is how
long a start may take to settle (default 90 s).

Run `status`, `stop`, `restart` and `filesync` as root or as a sudoer:
`systemctl`, `journalctl`, `kill` and friends are called through `sudo` when
needed. Run `install` as the **device user, not under sudo**. Only the
installer is given sudo, and with `--full` it keeps a forwarded ssh agent
(`ssh -A`) so the installer's git can fetch.

#### `status`
Shows each unit's state, whether it is enabled, its restart count, and every
device-software process that is running.

#### `stop`
Stops the units in reverse start order. The bridge goes first, because it can
restart the device unit out of band. Then it kills any stray device software:
`hollohw` children, or a copy started by hand. These get SIGTERM, then
SIGKILL after 15 s.

#### `restart`
Runs `stop`, then starts the infrastructure, then the units. The start counts
only once **every check passes 3 times in a row, 2 s apart, without a unit
restarting in between**. A configuration error often lets the process come up
and die a few seconds later. The checks are:

- **old:** `holo_gui` active, a `hollohw` process, the Dash GUI on :8050.
- **new:** the device unit active, the device API on :1234, the Dash GUI on
  :8050 (not when headless), `holo_bridge` active, the bridge on :8080,
  mosquitto on :1883, nginx on :80.

The wait stops early when systemd has given up on a unit or restarted it.

Passing checks only mean the *software* runs, because the device API comes up
even when the hardware does not. So after the checks pass, the device log
written since the start is searched for the software's own hardware-failure
messages. If one is there, the result is **"Started, but the hardware did not
initialise"**, with a summary of the cause, and the exit code is 1.

#### Diagnosis of a failed start
The diagnosis reads the unit's journal and the newest log in `~/gui/log`, and
leads with the **first** real failure, not the last. A failed hardware init
cascades: components that were never created crash later code, so the last
traceback is usually a consequence. To keep the cause up front:

- The first real traceback is shown with its innermost frames.
- Later `'NoneType' object has no attribute / is not iterable / is not
  subscriptable` errors are marked as consequences.
- Errors from the GCS broker link (pika, AMQP, `ACCESS_REFUSED`, …) and
  undervoltage warnings are reduced to one-line notes. They never stop a start
  by themselves.

#### `filesync`
Writes a GCS-only `/etc/holloid/filesync.json`: one `broker` target at
10.77.0.3:5672, owned by the device user, mode 600. Both layouts read this
file. The login is a deliberate dummy (`dummy_user`/`dummy_password`). The
device software connects in the background and keeps running when the login
is refused, so this is enough to get a device going until the real login is
filled in (UI: Admin tab > File sync). An existing file is left alone unless
you pass `--force`. The device software reads the file only at startup, so run
`restart` afterwards.

#### `install`
Runs `~/gui_new/remote/deploy/install_device.sh --headless` on `~/gui_new`
**as it is checked out**, then checks the result. It does no git itself.

- **Default:** the installer serves the checkout's committed UI bundle and
  updates the bridge, but leaves the device software running on the Python it
  last loaded. So this script then stops and restarts the device software and
  checks the start, as `restart` does.
- **`--full`:** also passes the current branch (`--branch`), so the installer
  rebuilds the venv, packages and unit. This needs a branch checked out (not a
  detached HEAD) and git access: a forwarded ssh agent, a key of the device
  user's own, or `--https`. The installer fast-forwards the branch to
  `origin/<branch>` if that is ahead. Without `--full`, `~/gui_new/venv` must
  already exist.

Checks done before the installer runs:

- **UI bundle freshness.** `remote/ui/dist/.src-stamp` must match a hash of
  `remote/ui/src` (plus `index.html`, `vite.config.ts`, `package*.json`,
  `.env`; test files left out). This is the same hash the vite build stamps.
  A mismatch means the device would serve an older UI, so the install is
  refused unless you pass `--stale-ui`.
- **Device config.** Missing `/etc/holloid/configuration.json` or
  `filesync.json` is reported.
- **Server link.** With neither `--no-server` nor `--with-server`, the script
  passes `--no-server` to the installer when there is no OpenVPN config, or
  when the server (from `/opt/holloid/cloud.conf`, else 10.77.0.17:8883) does
  not answer. Otherwise the installer would end by waiting for a link that
  never comes.

Arguments after `--` go to `install_device.sh` unchanged.

### Exit codes
`0` success · `1` the action failed (including "started without hardware") ·
`2` the layout could not be determined, or both versions are running ·
`130` interrupted.

Set `NO_COLOR=1` to turn off coloured output. Colour is also off when stdout
is not a terminal.

### Tests

```
python3 -m unittest test_holo_service      # or: python3 test_holo_service.py
```

The fixtures are real starts from 2026-10-22 on `hm-2p99`:

- `testdata/log_start_failed_no_pins.log`: a start that failed on an Igor
  pump configured without pins (rebuilt from the tool's output).
- `testdata/log_started_no_igor_reply.log`: a start that passed every check
  while the Igor did not answer (as logged).

---

## proto_switch.py

Switches a device between its own configuration ("main") and the prototype
one, in which it identifies as an uninitialised device. It is independent of
the device software and does not stop or start it.

```
sudo proto_switch.py record [--force]
sudo proto_switch.py proto  [--force] [--no-reload]
sudo proto_switch.py main   [--force] [--no-reload]
sudo proto_switch.py status
```

| | main | proto |
|---|---|---|
| `/etc/ssh/ssh_host*key*` | as recorded | unpacked from `/etc/ssh/proto_keys.tar`; other host keys removed |
| `/etc/holloid/deployment.json` | as recorded | `{}` |
| `/etc/holloid/configuration.json` | as recorded: file, symlink or absent | absent |
| `/etc/holloid/tailoring.json` | as recorded: file, symlink or absent | absent |
| `~mage/.ssh/authorized_keys` | as recorded | a copy of `~mage/.ssh/fresh_keys` |

A symlink is recorded and restored as a link. Its target is never touched.

#### `record`
Saves the live main configuration in `/var/backups/holloid-main` (root
only, because it holds private host keys): copies of the files, plus a
`manifest.json` with each file's hash, mode and owner, or a link's target.
The backup before it is kept as `holloid-main.previous`. Run it again
whenever the main configuration changes. It refuses while the device is in
the prototype state, or while the live host keys are the prototype ones,
because that would overwrite the main backup with the prototype.

#### `proto` and `main`
Everything is checked before anything is changed: the recording exists and
is intact, `proto_keys.tar` holds only `ssh_host*key*` files, and
`fresh_keys` exists and is not empty. While the device is in the main state,
both commands also refuse if the live files changed since `record`, because
switching would lose those changes. Run `record` to keep them, or pass
`--force` to discard them.

`authorized_keys` is switched last. Afterwards `sshd -t` checks the result and
sshd is reloaded, so it serves the new host keys. Open sessions stay up.
Clients that knew the other host key will warn that it changed:
`ssh-keygen -R <host>` on the client forgets the old one.

A switch that is interrupted leaves the state as `partial`. Running `main` or
`proto` again finishes it.

#### `status`
Shows when the recording was made, which state is active, whether the live
host keys are the prototype ones and, in the main state, what has changed
since `record`.

Exit codes: `0` success · `1` failed · `2` refused before anything was
changed.

```
python3 -m unittest test_proto_switch
```

---

## fixgeany.py

```
python3 fixgeany.py
```

Rewrites Geany's configuration on Linux (`$XDG_CONFIG_HOME/geany`) or Windows
(`%APPDATA%\geany`):

- **Font:** Hack, sized from the screen width. 9 below 2000 px, 12 below
  2800 px, 18 above that.
- **Editor:** the solarized-dark scheme, tab width 2, long-line marker at
  column 80, no indent detection, no auto-closing or snippets, no recent-files
  list. Removes any `pref_main*` and `use_*` keys.
- **Keybindings:** Ctrl+Shift+( / ) to indent and unindent, Ctrl+Shift+J to
  join lines, Ctrl+Shift+Z to redo. With the shiftcolumn plugin, Ctrl+9 / Ctrl+0
  shift left and right.
- **GTK:** prefers the dark theme. On Windows this needs Administrator rights,
  because the file is under Program Files.

Close Geany before running it, or Geany will overwrite the changes when it
exits.
