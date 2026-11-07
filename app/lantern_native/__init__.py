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

ACCOUNTS_FILE = 'lantern-accounts.db'
SECRETS_FILE = 'lantern-secrets.json'

_instance = None
_fingerprints = None


def is_enabled(settings):
    """Whether Lantern is turned on in the settings (shop.lantern_enabled, off by default)."""
    shop = (settings or {}).get('shop') or {}
    return shop.get('lantern_enabled') is True


def build(settings, uid, config_dir=CONFIG_DIR, library=None):
    """The LanternApp for these settings, its accounts and secrets kept in `config_dir`."""
    from lantern.server import MemoryLibrary, SecretsFile, SqliteAccounts
    from lantern.server.app import LanternApp

    shop = settings.get('shop') or {}
    os.makedirs(config_dir, exist_ok=True)
    vault = SecretsFile(os.path.join(config_dir, SECRETS_FILE))
    return LanternApp(
        library if library is not None else MemoryLibrary(base=BASE),
        uid=uid,
        name=str(shop.get('discovery_name') or 'AeroFoil').strip()[:100] or 'AeroFoil',
        base=BASE,
        accounts=SqliteAccounts(os.path.join(config_dir, ACCOUNTS_FILE)),
        # A private shop shows nothing to a client that has not paired
        public=bool(shop.get('public')),
        # Cursors and since tokens, and the grant key of peers, must survive a restart
        secret=vault.get('cursor', 32),
        grant_seed=vault.get('grant', 32),
        software=('AeroFoil', 'native'),
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
        if start_worker:
            from app.lantern_native.fingerprints import Fingerprinter

            _fingerprints = Fingerprinter(app)
            _fingerprints.start()
        logger.info('Lantern is served at %s (uid %s).', BASE, _instance.uid)
        return _instance
    except Exception:
        logger.exception('Lantern could not start; AeroFoil carries on without it.')
        return None


def instance():
    """The running LanternApp, or None when it is off."""
    return _instance


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
