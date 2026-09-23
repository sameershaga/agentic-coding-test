# Daily planner systemd scheduling

The example user timer runs `daily-planner schedule` at 19:30 local time, before
the existing Daily GN-HF Runner's 20:00 timer. It creates today's deterministic
plan. It enqueues that plan only when `automatic_enqueue` is explicitly set to
`true` in `config/daily-planner.json`. The safe default is `false`.

The repository does not install or enable these units automatically. From the
repository root, install them for the current user after reviewing the config:

```console
mkdir -p ~/.config/systemd/user
sed "s|@REPOSITORY_ROOT@|$PWD|g" systemd/daily-planner.service \
  > ~/.config/systemd/user/daily-planner.service
cp systemd/daily-planner.timer ~/.config/systemd/user/daily-planner.timer
systemctl --user daemon-reload
systemctl --user enable --now daily-planner.timer
```

If the repository path contains characters special to `sed`, edit the copied
service manually. Confirm that the placeholder is gone before enabling it.

```console
systemctl --user status daily-planner.timer
systemctl --user list-timers daily-planner.timer daily-gnhf.timer
journalctl --user -u daily-planner.service
```

`Persistent=true` starts one missed activation when the user manager next
becomes available. Same-day plan identity and enqueue receipts make repeated
activations safe. The command never runs GN-HF. Enqueue still goes through
`scripts/daily-gnhf enqueue`, and the 20:00 runner remains responsible for task
selection and execution.

On WSL2, systemd and its user manager must be enabled. Timers cannot run while
the WSL environment, host, or user manager is stopped. A persistent timer can
catch up only after the environment starts again. Installing this planner timer
does not install the runner timer.

Remove the planner schedule with:

```console
systemctl --user disable --now daily-planner.timer
rm ~/.config/systemd/user/daily-planner.timer
rm ~/.config/systemd/user/daily-planner.service
systemctl --user daemon-reload
```
