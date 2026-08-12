# Rocky deployment

The application listens only on `127.0.0.1:8766`. When it is exposed through
Tailscale Funnel, its application login is the access boundary. It has three
accounts: Sean Ahern and Jen Ahern can record drives. Daniel Ahern can be given
view-only access to the log, CSV backup, and DMV PDF. The account that starts
or saves a drive is recorded as that drive's supervising driver.

Install the checked-out stack tip:

```sh
sudo dnf install -y epel-release
sudo dnf install -y python3.13 python3.13-pip sqlite poppler-utils
./driving-log bootstrap
./driving-log install --public-scheme https
loginctl enable-linger "$USER"
```

`install` finds this machine's Tailscale DNS name, creates the git-ignored
`driving-log-runtime/` directory, copies a legacy home-directory log there if
one exists, writes the private environment file, and installs/reloads the user
systemd units. It does not start the service yet.

Before starting, set a password for each account. Each command prompts twice;
the tool salts and hashes the password directly into the private environment
file, so nothing needs to be copied or pasted:

```sh
./driving-log --set-password Sean
./driving-log --set-password Jen
./driving-log --set-password Daniel
./driving-log start
tailscale funnel --bg --https=8443 http://127.0.0.1:8766
```

Re-running `./driving-log install` after an update reloads the units and restarts
the web service and archive timer when they are already active. This keeps the
running application schema support in step with migrations performed by
one-shot commands.

Before changing Funnel, save `tailscale serve status --json` and verify the
existing HTTP port 80 handler still forwards to Wordle on `127.0.0.1:8765`.
Never bind this application to a LAN or tailnet address. The service refuses to
start unless Sean and Jen's password hashes are configured; Daniel's account is
available after its password is set. The installer creates the
separate session-signing secret automatically.

The git-ignored `driving-log-runtime/` directory holds the mode-`0600`
environment file and database, plus mode-`0700` archive and restore-request
directories. The installer safely copies an existing legacy home-directory
database and its archives there once; it leaves the original intact as a
fallback.
Configure `DRIVING_LOG_EXTERNAL_ARCHIVE_DIR` to protect against loss of the
Rocky disk.

Microsoft Forms remains an adapter contract only. No workbook ID or
credentials were available for this release.

The restore helper is used only for a restore initiated by the running web
application, so it deliberately restarts that application after the helper
finishes. A direct CLI restore instead requires the web service to be stopped
and does not invoke the helper unit.
