# Lantern

AeroFoil can serve the open [Lantern](https://github.com/JigSawFr/lantern) protocol next to its own web UI and
shop, using the protocol's server library ([`lantern-protocol`](https://github.com/JigSawFr/lantern-sdk),
pinned in `requirements.txt`). It is off by default.

Turn it on in **Settings → Shop → Lantern protocol**, then restart AeroFoil. Lantern is served at
`/api/lantern` and `/.well-known/lantern`.

- The server's identity is the one LAN discovery already uses (`config/discovery_uid`); its name is the
  discovery name.
- Paired devices and tokens are kept in `config/lantern-accounts.db`; the keys that seal cursors and sign
  peer grants are kept in `config/lantern-secrets.json`. Both are created readable by their owner alone and
  are part of your backups.
- With **Public shop** off, a client sees nothing until it has paired.
- If Lantern cannot start, AeroFoil logs the reason and carries on without it.

The code is in `app/lantern_native/` (not `app/lantern/`: `python app/app.py` puts `app/` first on the import
path, where it would hide the library).

## What is served

The library is the files AeroFoil has identified **and hashed**: a file appears once its SHA-256 is known (the
first pass over a large library takes a while; `GET /api/lantern-native/status`, admin only, shows the progress).
An app's id comes from its bytes, so the same file in two places is one app; a bundle holding a base, an update
and DLC is one app that includes the others. Files are served in place, with ranges and `Repr-Digest`.
`/v1/changes` says what really changed, also across a restart (`config/lantern-journal.db` and
`config/lantern-announced.db`).
