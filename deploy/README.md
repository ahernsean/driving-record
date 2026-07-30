# Rocky deployment

The application listens only on `127.0.0.1:8766`. Tailscale Serve is the
network authorization boundary; there is deliberately no application login.

Install the checked-out stack tip:

```sh
sudo dnf install -y epel-release
sudo dnf install -y python3.13 python3.13-pip sqlite poppler-utils
./driving-log bootstrap
./driving-log install --public-host HOSTNAME:8443 --public-scheme http
loginctl enable-linger "$USER"
./driving-log start
tailscale serve --bg --http=8443 http://127.0.0.1:8766
```

Re-running `./driving-log install` after an update reloads the units and restarts
the web service and archive timer when they are already active. This keeps the
running application schema support in step with migrations performed by
one-shot commands.

Before changing Serve, save `tailscale serve status --json` and verify the
existing HTTP port 80 handler still forwards to Wordle on
`127.0.0.1:8765`. Never use Funnel and never bind this application to a LAN or
tailnet address.

The environment file is mode `0600` at
`~/.config/driving-log/environment`. Mutable state is mode `0700` beneath
`~/.local/state/driving-log`; the database and archives are mode `0600`.
Configure `DRIVING_LOG_EXTERNAL_ARCHIVE_DIR` to protect against loss of the
Rocky disk.

Microsoft Forms remains an adapter contract only. No workbook ID or
credentials were available for this release.

The restore helper is used only for a restore initiated by the running web
application, so it deliberately restarts that application after the helper
finishes. A direct CLI restore instead requires the web service to be stopped
and does not invoke the helper unit.
