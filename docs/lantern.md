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

Each game has its sheet from AeroFoil's titles database: name, description, categories as tags, and its age rating
(read as the same age by Lantern, so a cap hides exactly what AeroFoil's would). Icons and banners AeroFoil has
cached are served from here; the others point at the titles database's own addresses and are marked external,
and screenshots are always external. One shelf, *Recently added*, is offered.

## Accounts and pairing

A Lantern account is an AeroFoil user. A device shows a short code; the user opens `/lantern/pair` on AeroFoil,
signs in as usual, compares the key fingerprint when one is set, and approves. The device gets what the account
may do: the library and requests with shop access, saves with backup access, cheats with cheat access,
administration with admin access. Freezing a user freezes their devices and deleting a user deletes them (within
a few seconds). Users list and revoke their devices on **Profile**.

Behind a reverse proxy that ends TLS, tick **Behind an HTTPS reverse proxy** in the settings (devices sign the
`https` address they use) and, with a certificate from a private authority, give the key's fingerprint.

## Saves

Lantern save backups live in AeroFoil's own saves folder (`data/saves/<user>/<title id>/<kind>/`), so a backup
made from a console shows up on the **Saves** page and the other way round, and nothing is copied. The five kinds
of Lantern (account, device, bcat, cache, other) are now save types of AeroFoil too; what was saved before without
a type is offered to Lantern as `other`. Erasing a Lantern account removes only what Lantern wrote there. Backups
that end-to-end encryption protects are a separate change.

## Cheats and requests

Cheats are AeroFoil's cheat files, one per game build (`data/cheats/<title id>/<build id>.txt`): a Lantern set is
named `cs_<title id>-<build id>`, its note is the file's note, and a set written from a console is the same file
the Cheats page shows (a set with no build id is refused). Requests for games are AeroFoil's title requests: the
same request shared by the users who asked, visible on the Requests page; closing it there makes it `done`
for Lantern and denying it makes it `declined`.

## Administration

An administrator's device (admin access) can use Lantern's administration API: accounts, devices, invitations,
pairings and request decisions. Lantern accounts follow AeroFoil's users: scopes and frozen state are brought
back in line with the user's access within a few seconds, so change them on the **Users** page. An account made
by hand through the API is left alone. A user's **maximum rating** is a cap on all their devices, set before or
after pairing (the stricter of it and an invitation's wins). **Manage** shows the fingerprint progress while the
library is being read for the first time.

## Events and the LAN

Devices that keep a connection open hear about changes at once: games added or removed, a sheet changed, a cheat
changed (on the Cheats page too), plus the backups, requests and pairings the protocol itself tells. Run one
AeroFoil process: the streams are held in it. When **LAN discovery** is also on in the settings, AeroFoil
announces itself with DNS-SD (`_lantern._tcp`, the advertised web port of the same setting) so Lantern clients
find it without an address.

## Encrypted backups, sync and peers

- **End-to-end encrypted backups** (the console encrypts, AeroFoil never holds a key) are kept as sent in `.enc`
  files next to the other backups. AeroFoil's Saves page does not list them: it cannot read them, and Lantern
  clients get them back as they were sent.
- **Sync** keeps each user's wishes and installed lists (`config/lantern-sync.db`), merged item by item and kept
  across restarts.
- **Peers**: the server signs short-lived grants for its files with a key kept in `config/lantern-secrets.json`,
  the same after a restart, so other Lantern servers and devices can fetch from it. Declaring mirrors of a file
  is not offered yet.

## Decompressed variants and differential updates

A compressed package (`.nsz`, `.xcz`) is listed with `requires: ["fs-compression"]`. Beside it, a second app of the
same game is offered as the plain `.nsp`/`.xci`, streamed decompressed on the fly with ranges, for consoles that
cannot read compressed packages. To list a file with its SHA-256 before it is served, each compressed file is
decompressed once in the background after the library is listed, and the result is kept in
`config/lantern-variants.db` for the size and time of the file; a changed file loses its variant until it is
hashed again. This costs CPU time on a large library: turn it off with **Offer decompressed NSP/XCI** in the
settings.

Files between 128 KiB and 64 MiB (updates, small packages) carry a chunk index, kept in `config/lantern-chunks/`,
so a device that has an older version fetches only the pieces it lacks. Larger files have none: cutting them in
Python is too slow.
