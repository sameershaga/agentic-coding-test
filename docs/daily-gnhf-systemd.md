# Daily GN-HF systemd scheduling

The example user timer runs `daily-gnhf catch-up` at 20:00 local time. The
runner's queue lock, deterministic task IDs, sequential execution policy, and
catch-up limit remain authoritative if the timer and a manual invocation happen
at the same time. The units do not require root and are not installed or enabled
by this repository.

## Install the user units

Copy the examples to a temporary location and replace `@REPOSITORY_ROOT@` in the
service with this repository's absolute path. Do not leave the placeholder in an
installed unit.

```console
mkdir -p ~/.config/systemd/user
sed "s|@REPOSITORY_ROOT@|$PWD|g" systemd/daily-gnhf.service \
  > ~/.config/systemd/user/daily-gnhf.service
cp systemd/daily-gnhf.timer ~/.config/systemd/user/daily-gnhf.timer
systemctl --user daemon-reload
systemctl --user enable --now daily-gnhf.timer
```

Run those commands from the repository root. If the path contains characters
that are special to `sed`, edit the copied service manually instead. Inspect the
installed schedule and recent execution with:

```console
systemctl --user status daily-gnhf.timer
systemctl --user status daily-gnhf.service
systemctl --user list-timers daily-gnhf.timer
journalctl --user -u daily-gnhf.service
```

The service invokes the conservative `catch-up` command, not an unbounded batch.
It therefore uses `catch_up`, `catch_up_mode`, and `max_catch_up_tasks` from
`config/daily-gnhf.json`. With the default sequential mode, an active task also
prevents another task from being claimed.

## Persistent timer behavior

`Persistent=true` makes systemd remember that the calendar event was missed. If
the user manager was inactive at 20:00, systemd starts the service after the user
manager next becomes available. The queue then independently identifies dated
pending tasks and applies its configured catch-up limit. Persistent timers do not
run jobs while the computer is powered off.

## WSL2 limitations

Verify that the WSL distribution is using systemd before installing the units:

```console
systemctl --user is-system-running
systemctl --user list-timers
```

If these commands report that systemd is not running, enable systemd for the WSL
distribution according to the installed WSL version and distribution guidance,
then restart the distribution. The user manager and timer can run only while the
WSL environment is running. Windows shutdown, sleep, or a stopped WSL virtual
machine prevents execution. After WSL and its user manager start again,
`Persistent=true` can trigger one missed timer activation, and the runner's
conservative catch-up policy decides which queued missed task is eligible.

User lingering can keep a user manager active on a conventional Linux host, but
it does not keep WSL running while Windows is off. This example does not enable
lingering or change machine-level WSL settings.

## Remove the schedule

```console
systemctl --user disable --now daily-gnhf.timer
rm ~/.config/systemd/user/daily-gnhf.timer
rm ~/.config/systemd/user/daily-gnhf.service
systemctl --user daemon-reload
```
