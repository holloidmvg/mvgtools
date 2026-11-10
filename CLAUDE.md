# mvgtools: notes for Claude

Read `README.md` first: it says what each tool does and how to run it. This
file holds what the README doesn't, which is how the tools are built, what
they depend on and what has been decided. Personal working preferences are in
`CLAUDE.local.md`. That file is git-ignored, so it exists only on the owner's
machine.

## What this repo is

These are tools for the Holloid device software (`holo-control`), kept apart
from it. The remote is `git@github.com:holloidmvg/mvgtools.git`. The default branch
is `main`. Work is done on feature branches, which are merged into `main`
when the owner says so. `holo-service-diagnosis` was merged on 2026-11-10.
Check `git branch -avv` before you commit. Pushing publishes to GitHub, so
**ask before every push**.

History: `holo_service.py` was first committed into holo-control (2026-10-20).
Those commits were removed, it lived briefly in `~/projects/holo-support`, and
it moved here when this repo was created (2026-10-21). Tools for holo-control
are built here and never in holo-control itself.

## holo-control: read it, never write it

`~/projects/holo-control` is the device software these tools work with.
**Read-only:** read files, grep, `git log/show/diff/blame` freely. Never edit,
commit, check out, reset, stash or add files there.

- The working branch is `_new-ui-igor`. **Ignore `main`**, which is due to be
  replaced. Don't diff or compare against it, and don't treat it as a base.
- Other branches you will see: `interim_main` (the "old" layout), `new-ui`,
  and `v6_tmp`.

## Where holo_service.py is coupled to holo-control

These must stay in step with holo-control. When one of them changes there,
`holo_service.py` has to follow:

| holo_service.py | holo-control |
|---|---|
| `DEVICE_SOFTWARE` regex | the same pattern in `remote/deploy/install_device.sh` (`DEVICE_SOFTWARE=`) |
| `install_command()` flags (`--gui-dir --gui-user --headless --branch --https --no-server`) | `remote/deploy/install_device.sh` |
| `ui_stamp()`, `UI_NOT_SHIPPED`, `UI_OUTSIDE_SRC` | `remote/ui/src/lib/srcStamp.ts` and `remote/ui/vite.config.ts`: the **same algorithm** must produce the same hash |
| `default_filesync()` | `holo_control_core/src/holo_control_core/filesync/config.example.json`. The dummy login is lowercase on purpose, because the config check refuses the example's placeholders. |
| `LANDMARK`, `HARDWARE_FAILED`, `LOG_PREFIX` | the messages and log format of the device software (`client_app.py` / `client_device.py`) |
| `UPLINK_CONF`, `SERVER_HOST/PORT` | what `install_device.sh` writes to `/opt/holloid/cloud.conf` |

## Conventions

- **Standard library only**, because `holo_service.py` runs on the device's
  system `python3`, outside any venv. It is a single file you can copy to a
  device. Keep it that way. **The devices run Python 3.11** (per the owner,
  2026-11-10), so anything up to 3.11 is fine. The existing code uses `%`
  formatting and `universal_newlines=True` out of habit, not need. Match the
  style around an edit, but don't keep it on account of old devices.
- Output uses the `ok()` / `bad()` / `info()` / `heading()` helpers. Messages
  say what happened *and what to do about it* (for example "pass --force to
  replace it").
- Comments and docstrings explain *why*, in plain sentences.
- Exit codes: 0 ok, 1 failed, 2 layout unknown or conflicting.
- **Tests:** `python3 -m unittest test_holo_service`. pytest is not installed.
  Run the tests after every change to `holo_service.py`. Fixtures in
  `testdata/` are real (or reconstructed) device logs. Add one when you fix a
  diagnosis that a real log got wrong.
- Throwaway test scripts go in the session scratchpad, not here. Only tools
  the owner asks for go in this repo.
- **Commits:** the subject is an imperative summary of the behaviour
  ("Report a start whose hardware did not initialise"). The body explains the
  reason and the real incident behind it. End with the Co-Authored-By line.
  Commit only when asked. `user.name` here is `MvG`.

## Domain facts that are easy to get wrong

- **The headless test device has its VPN off on purpose**, so it won't send
  junk to the cloud. pika / AMQP "connection workflow failed" errors every
  ~10 s, and `ACCESS_REFUSED` to the GCS broker (10.77.0.3), are expected
  there. Don't try to fix connectivity. That's why `BROKER_NOISE` exists and
  `install` falls back to `--no-server`. If the noise itself bothers you, the
  cause is in holo-control: `client_device.py` never calls
  `configure_log_levels()`.
- **The device API comes up even when the hardware fails.** Ports answering
  ≠ device working. This is why `verify()` reads the log after the checks
  pass. Incident: 2026-10-22, an Igor that didn't answer still read
  "Started".
- **The last traceback is usually a consequence.** A component that was
  never created crashes later code with `'NoneType' ...` errors. The cause is
  the first traceback. Incident: 2026-10-22 on `hm-2p99`, an Igor pump
  configured without pins.
- **Two copies of the device software must never run at once**, because they
  would both drive the hardware. Every start path refuses to start while old
  processes are still alive.
- Logs land in `~/gui/log` for both layouts, even for software in
  `~/gui_new`.
- In holo-control, `hardware/pumpmc_double.py` has
  `ComponentPumpDoublePush_Igor.__init__` raising unconditionally, which also
  disables `ComponentDilutionPumpDoublePush_ArduinoBox`. **This is
  intentional:** only decommissioned devices used it, and it is kept as a
  template. Don't flag it in reviews.

## Open ends

- `fixgeany.py` is unrelated to the device and has no tests.
