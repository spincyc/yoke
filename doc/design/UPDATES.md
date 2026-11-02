# Linux endpoint updates

Status: normative application update contract, version 1 (ADR 0018).
Applies to an onboard Linux Yoke agent, including the proposed Radxa ZERO
3E endpoint. For ESP32 application firmware see [ESP32-UPDATES.md](ESP32-UPDATES.md).
Hardware operation and power-cut durability have not been measured.

## Authority and transport

Provision OpenSSH independently, disable unused authentication methods, and
authorize the operator's own SSH key through normal host administration.
Verify the device SSH host key on first connection. Yoke creates no account,
password, remote listener, update key, or hidden access mechanism. Transfer
the artifact using SFTP/SCP over Ethernet, then explicitly run the local
updater through SSH with administrative permission. Neither Thermite nor a
central server needs to exist. A third-party SFTP/SSH client has the same
access and artifact format as the supplied command-line examples.

The whole-artifact SHA-256 argument is mandatory. Obtain it through the
operator's trusted release approval path, independently of an untrusted
download location. A hash establishes integrity against that approved
digest; it is not a publisher signature or an automatic trust decision.
The SSH operator and host administrator can already replace software and
are trusted. A holder of the YWP site PSK alone cannot install an update.
No update payload or command is sent through multicast or `yoke.core`.

## Persistent layout

| Path | Owner and purpose |
|---|---|
| `/opt/yoke/updater.py` | root-owned, fixed maintenance tool, stdlib only |
| `/opt/yoke/releases/<release>/venv` | root-owned installed release; never replaced in place |
| `/opt/yoke/releases/<release>/release.json` | verified artifact identity and manifest |
| `/opt/yoke/current` | atomic relative symlink to `releases/<release>` |
| `/opt/yoke/update-state.json` | durable transaction journal and last outcome |
| `/opt/yoke/.update.lock` | exclusive local update lock, never unlinked |
| `/etc/yoke/` | existing config, identity, and site key; updater never writes here |
| `/var/lib/yoke/` | existing pairing, sequence, and driver state; updater never writes here |

The agent account must not be able to modify `/opt/yoke`. Update commands
must use the fixed maintenance script, not the candidate virtual environment.
The script also works as `python -m yoke.update` for local build and test
use. A host Python 3.12+ with `venv` and bundled `ensurepip` is required.
The host OS, external drivers, and Python ABI are separate maintenance
responsibilities. No package manager contacts the network during staging.

Install the fixed tool and recovery unit from the same reviewed checkout
when provisioning the service (see [DEPLOYMENT.md](DEPLOYMENT.md)):

```sh
sudo install -m 0644 src/yoke/update.py /opt/yoke/updater.py
sudo install -m 0644 examples/yoke-update-recover.service /etc/systemd/system/
sudo install -m 0644 examples/yoked.service /etc/systemd/system/
sudo systemctl daemon-reload
```

The endpoint unit MUST use `Type=notify`, `NotifyAccess=main`, and
`ExecStart=/opt/yoke/current/venv/bin/python -m yoke.cli agent ...`.
It MUST require the recovery unit and start after it. Recovery must run
before any candidate service can start. The baseline installation must
already have a commissioned state and working `current` release. The
fresh-install `initial` release may predate `release.json`; it is a valid
rollback baseline but cannot be selected as a new staged update.

## Build and transfer

Build a dependency-free `yoke-<version>-py3-none-any.whl` from the reviewed
checkout on a build machine with Python 3.12+. Build tooling may download
dependencies there; the endpoint does not. The bundle command accepts only
the Yoke pure Python wheel, refuses runtime dependency declarations and unsafe ZIP
paths, and writes a
new artifact without overwriting an existing file:

```sh
python -m pip wheel --no-deps --wheel-dir .scratch/update-build .
PYTHONPATH=src python -m yoke.update bundle \
  --wheel .scratch/update-build/yoke-0.1.0-py3-none-any.whl \
  --release 2026.11.02-r1 --output .scratch/update-build/yoke-2026.11.02-r1.tar
```

Record the printed `sha256` in the approved release record. The command
emits sorted JSON. Transfer to an operator-controlled incoming directory
on the device; the incoming location is not executable release storage:

```sh
scp .scratch/update-build/yoke-2026.11.02-r1.tar operator@device:/var/tmp/
ssh -t operator@device
```

On the device, substitute the independently approved 64-character digest:

```sh
sudo /usr/bin/python3 /opt/yoke/updater.py stage /var/tmp/yoke-2026.11.02-r1.tar \
  --sha256 APPROVED_SHA256 --dry-run
sudo /usr/bin/python3 /opt/yoke/updater.py stage /var/tmp/yoke-2026.11.02-r1.tar \
  --sha256 APPROVED_SHA256
sudo /usr/bin/python3 /opt/yoke/updater.py status
sudo /usr/bin/python3 /opt/yoke/updater.py activate 2026.11.02-r1 --dry-run
sudo /usr/bin/python3 /opt/yoke/updater.py activate 2026.11.02-r1
```

Staging verifies first, reserves a fresh release directory, checks the
installed CLI with `--help`, and publishes a completion marker atomically.
The virtual environment is created at its permanent path so its absolute
console-script paths remain valid. An incomplete release cannot be activated.
The currently running service is untouched. No source build, dependency
resolution, arbitrary manifest command, configuration copy, or migration
runs. Dry runs validate and report their intended action without creating
directories, taking a persistent lock, stopping a service, or installing.
Keep the approved artifact and digest off-device for disaster recovery.

## Activation, rollback, and recovery

Activation verifies the unit's notify contract, executable path, required
boot recovery ordering, and fixed maintenance script. The recovery oneshot
must already be active, so starting a candidate cannot run offline recovery
in the middle of the transaction. A normal service start establishes that
condition. When first adding update support to an existing installation,
stop `yoked`, start `yoke-update-recover.service`, then start `yoked` before
activating a release.

Activation takes the exclusive lock, durably records the previous release,
stops the old service, replaces `current` atomically, and starts the new service. Start
waits for READY, then repeated active checks establish a short local
readiness window. All external commands have finite deadlines and argv
vectors; there is no shell interpolation. SSH loss does not expire a
pairing or commission a new identity.

On failed start or health, the updater stops the candidate, restores the
previous link, and starts and checks the previous release. An automatic
rollback is a nonzero update result even when recovery succeeds. It retains
the failed candidate for inspection but does not make it a manual rollback
target. No state backup is restored automatically: replacing a sequence
history with an older copy would be unsafe. A package declaring a state
format other than 1 is refused before installation.

For an intentional rollback after a healthy activation:

```sh
sudo /usr/bin/python3 /opt/yoke/updater.py rollback --dry-run
sudo /usr/bin/python3 /opt/yoke/updater.py rollback
```

If activation was interrupted, the boot recovery unit restores the previous
link before the agent starts. To recover immediately on a running host:

```sh
sudo /usr/bin/python3 /opt/yoke/updater.py recover
```

`recover --offline` only restores the link and journal; it never starts or
stops services. It is for the ordered boot unit or an already stopped
endpoint. If stopping, restoring, restarting, or checking the previous
release fails, the journal reports `recovery-required`; investigate with
`status` and `journalctl -u yoked`. No successful outcome is inferred.
An interrupted staging directory can remain under `releases/<release>`
without `release.json`; it is never a published release or boot target.
Remove it only when no updater holds the lock, or choose a new release id.
The tool does not garbage-collect old releases.

## Artifact and outcome schemas

An artifact is an uncompressed USTAR archive, at most 96 MiB, containing
exactly two regular files at its root: `manifest.json` and the wheel.
No links, devices, sparse members, duplicate paths, or extension headers
are accepted. `manifest.json` is finite unique-key UTF-8 JSON, at most
16 KiB, with exactly this schema (field order is immaterial):

```json
{
  "format": "yoke-linux-release-v1",
  "release": "2026.11.02-r1",
  "state_format": 1,
  "wheel": {
    "filename": "yoke-0.1.0-py3-none-any.whl",
    "sha256": "64 lowercase hex characters",
    "size": 12345
  }
}
```

Release identifiers match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and may not
be `.` or `..`. The wheel is at most 64 MiB compressed and expanded, uses
stored or deflate ZIP compression, contains at most 4096 entries, and is
restricted to `yoke/` and its one `yoke-*.dist-info/` directory. It must declare `Name: yoke`, no
runtime `Requires-Dist`, `Root-Is-Purelib: true`, and only `Tag: py3-none-any`.
The checkout's optional development tools may appear in `Requires-Dist`
only with the exact marker `extra == 'dev'` (double quotes are also accepted)
and a declared `Provides-Extra: dev`. Other marker expressions and
unconditional dependencies are refused. Installation never selects an extra.
The approved digest binds every archive byte; the manifest also binds the
wheel's exact size and SHA-256. There is no executable hook field.

All commands emit one sorted JSON object with `schema_version: 1`.
`status` includes `current`, published `releases`, and the last transaction
`state` with `phase`, `active`, `previous`, `candidate`, and `detail`.
Phases are `idle`, `staging`, `staged`, `stage-failed`, `activating`,
`healthy`, `rolling-back`, `rolled-back`, and `recovery-required`.
Exit 0 means the requested operation succeeded; 2 means validation or an
operation error; 3 means activation failed but rollback succeeded; 4 means
recovery is required. Status is local update metadata, available through
SSH even when YWP is unavailable. It is not a claim about media or the
physical target. Service logs remain in the normal systemd journal.

The updater's offline tests cover verified installation, bad archives and
digests, serialized updates, failure before/after link replacement,
readiness failure, rollback failure, interrupted transaction recovery,
explicit rollback, and persistent-state preservation. Board provisioning,
real SSH authorization, systemd notification timing, filesystem power-loss
behavior, and physical devices still require a hardware acceptance run.
