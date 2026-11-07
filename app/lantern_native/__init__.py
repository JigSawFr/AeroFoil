"""Lantern, served by AeroFoil itself (https://github.com/JigSawFr/lantern).

The protocol is the SDK's (`lantern-protocol`); this package only joins it to AeroFoil: the stable uid, the
accounts and secrets kept in the config directory, and the settings that turn it on. It is off by default and
never stops AeroFoil from starting: if it cannot start, it logs why and the web UI, the shop and everything
else carry on.

The package is named `lantern_native` and not `lantern` because `python app/app.py` puts `app/` first on the
import path, where a package called `lantern` would hide the SDK's.
"""

import logging
import os

from app.constants import CONFIG_DIR

logger = logging.getLogger(__name__)

#: Where the protocol is served (SPEC.md § 2); /.well-known/lantern answers for discovery.
BASE = '/api/lantern'
#: Where a signed-in user approves a pairing (SPEC.md § 5.2).
PAIR_PATH = '/lantern/pair'

ACCOUNTS_FILE = 'lantern-accounts.db'
SECRETS_FILE = 'lantern-secrets.json'
JOURNAL_FILE = 'lantern-journal.db'
ANNOUNCED_FILE = 'lantern-announced.db'

_instance = None
_fingerprints = None
_dnssd = None
_refresh_latest_save = None


def set_save_refresher(function):
    """app.py gives the function that rebuilds a title's "latest" copy of a save (called after a Lantern change)."""
    global _refresh_latest_save
    _refresh_latest_save = function


def _refresh_latest(user, title_id, save_type):
    if _refresh_latest_save is not None:
        _refresh_latest_save(user, title_id, save_type=save_type)



def is_enabled(settings):
    """Whether Lantern is turned on in the settings (shop.lantern_enabled, off by default)."""
    shop = (settings or {}).get('shop') or {}
    return shop.get('lantern_enabled') is True


def _own_page(request):
    from lantern.server.http import not_found

    raise not_found('The approval page is served by AeroFoil.')


def build(settings, uid, config_dir=CONFIG_DIR, library=None):
    """The LanternApp for these settings, its accounts and secrets kept in `config_dir`."""
    from lantern.server import SecretsFile, SqliteJournal, SqliteSync
    from lantern.server.app import LanternApp

    shop = settings.get('shop') or {}
    os.makedirs(config_dir, exist_ok=True)
    vault = SecretsFile(os.path.join(config_dir, SECRETS_FILE))
    if library is None:
        from app.lantern_native.library import Announced, AeroFoilLibrary

        from app.lantern_native.variants import VirtualStore

        library = AeroFoilLibrary(
            SqliteJournal(os.path.join(config_dir, JOURNAL_FILE)),
            Announced(os.path.join(config_dir, ANNOUNCED_FILE)),
            BASE,
            # The decompressed form of .nsz/.xcz, hashed in the background (the setting can turn it off)
            virtual=VirtualStore(os.path.join(config_dir, 'lantern-variants.db'))
            if shop.get('lantern_variants', True)
            else None,
            chunk_dir=os.path.join(config_dir, 'lantern-chunks'),
        )
    from app.lantern_native.accounts import make_accounts
    from app.lantern_native.requests import AeroFoilRequests
    from app.lantern_native.saves import AeroFoilSaveStore

    return LanternApp(
        library,
        saves=AeroFoilSaveStore(refresh=_refresh_latest),
        requests=AeroFoilRequests(os.path.join(config_dir, 'lantern-request-ids.db')),
        # End-to-end encrypted backups are kept as sent; AeroFoil never holds the key (SPEC.md § 9.4)
        encrypted_saves=True,
        # What each user keeps in sync (wishes, installed), surviving a restart
        sync=SqliteSync(os.path.join(config_dir, 'lantern-sync.db')),
        uid=uid,
        name=str(shop.get('discovery_name') or 'AeroFoil').strip()[:100] or 'AeroFoil',
        base=BASE,
        accounts=make_accounts(os.path.join(config_dir, ACCOUNTS_FILE)),
        # A private shop shows nothing to a client that has not paired
        public=bool(shop.get('public')),
        # Cursors and since tokens, and the grant key of peers, must survive a restart
        secret=vault.get('cursor', 32),
        grant_seed=vault.get('grant', 32),
        software=('AeroFoil', 'native'),
        # The approval page is AeroFoil's own (app/lantern_native/web.py), where the user is signed in
        verify=PAIR_PATH,
        # Given, so that the SDK leaves the route to AeroFoil's own page (web.py)
        pair_page=_own_page,
        # Behind a reverse proxy that ends TLS, the scheme clients sign is not the one this app sees
        scheme=shop.get('lantern_scheme') or None,
        spki=shop.get('lantern_spki') or None,
        hsts=bool(shop.get('lantern_hsts')),
    )


def init_app(app, settings=None, config_dir=CONFIG_DIR, uid=None, start_worker=True):
    """Serves Lantern from `app` when it is turned on; returns the LanternApp or None.

    Reading the settings once, at start: turning it on or off takes a restart."""
    global _instance, _fingerprints
    try:
        if settings is None:
            from app.settings import load_settings

            settings = load_settings()
        if not is_enabled(settings):
            return None
        from lantern.server.flask import init_app as serve

        if uid is None:
            from app import discovery

            uid = discovery._server_uid()
        _instance = build(settings, uid, config_dir)
        serve(app, _instance)
        # What changed in the library reaches the devices that are listening (SPEC.md § 18)
        _instance.library.on_changes = _instance.library_changed
        from app import cheats

        cheats.listeners.append(_instance.library.cheats_changed)
        from app.lantern_native import accounts as mirror_accounts
        from app.lantern_native import web

        mirror = mirror_accounts.Mirror(
            _instance.accounts, names_file=os.path.join(config_dir, 'lantern-mirrored.json')
        )
        app.register_blueprint(web.blueprint_for(_instance, mirror))

        @app.before_request
        def _lantern_accounts():
            # Access, frozen and deleted users reach their paired devices before a Lantern request is handled
            from flask import request

            if request.path.startswith(BASE + '/'):
                try:
                    mirror.sync()
                except Exception:
                    logger.exception('Lantern: the accounts could not be brought up to date.')
        if start_worker:
            from app.lantern_native.fingerprints import Fingerprinter

            from app.lantern_native.variants import VariantBuilder

            library = _instance.library
            # Created now, started by start() once the database exists (see app.py)
            _fingerprints = Fingerprinter(
                app,
                on_pass=lambda done: library.refresh(),
                variants=VariantBuilder(library.virtual) if library.virtual is not None else None,
            )
        logger.info('Lantern is served at %s (uid %s).', BASE, _instance.uid)
        return _instance
    except Exception:
        logger.exception('Lantern could not start; AeroFoil carries on without it.')
        return None


def instance():
    """The running LanternApp, or None when it is off."""
    return _instance


def _lan_address():
    """The IPv4 address this machine has on its LAN (the route to a private address; nothing is sent)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(('192.0.2.1', 9))
            return probe.getsockname()[0]
        except OSError:
            return None


def advertise(port, settings=None):
    """Announces the server on the LAN with DNS-SD (`_lantern._tcp`) when Lantern and LAN discovery are both on.

    `port` is the web port; the shop setting for the advertised port wins (Docker, a proxy). Returns True when
    it announced."""
    global _dnssd
    if _instance is None:
        return False
    try:
        if settings is None:
            from app.settings import load_settings

            settings = load_settings()
        shop = (settings or {}).get('shop') or {}
        if shop.get('discovery_enabled') is not True or _dnssd is not None:
            return False
        address = _lan_address()
        if address is None:
            logger.warning('Lantern: no LAN address to announce.')
            return False
        from lantern.server.discovery import advertise_dnssd

        _dnssd = advertise_dnssd(_instance, int(shop.get('discovery_http_port') or port), address)
        logger.info('Lantern is announced on the LAN at %s (DNS-SD).', address)
        return True
    except Exception:
        logger.exception('Lantern: the LAN announcement failed; AeroFoil carries on.')
        return False


def stop():
    """Ends the announcement and the fingerprint worker (at shutdown)."""
    global _dnssd
    if _dnssd is not None:
        try:
            _dnssd.close()
        except Exception:
            pass
        _dnssd = None
    if _fingerprints is not None:
        _fingerprints.stop()


def start():
    """Starts the fingerprint worker. app.py calls it once the database is ready: at import it does not exist yet."""
    if _fingerprints is not None:
        _fingerprints.start()


def kick():
    """Tells the fingerprint worker that the library changed (a no-op when Lantern is off)."""
    if _fingerprints is not None:
        _fingerprints.kick()


def status():
    """What the fingerprint worker is doing, for the admin pages; {'enabled': False} when Lantern is off."""
    if _instance is None:
        return {'enabled': False}
    out = {'enabled': True, 'uid': _instance.uid}
    if _fingerprints is not None:
        out['fingerprints'] = _fingerprints.status()
    return out
