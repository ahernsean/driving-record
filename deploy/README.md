# Rocky deployment

The application listens only on `127.0.0.1:8766`. Tailscale Serve is the
network authorization boundary; there is deliberately no application login.

Install the checked-out stack tip:

```sh
./driving-log bootstrap
./driving-log install --public-host HOSTNAME:8443
loginctl enable-linger "$USER"
./driving-log start
tailscale serve --bg --https=8443 http://127.0.0.1:8766
```

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
