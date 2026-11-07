"""Lantern accounts are AeroFoil's users.

A device is paired by a signed-in AeroFoil user, who approves it on AeroFoil's own page; the Lantern account it
belongs to carries that user's name and what they may do (scopes), so a change of access, a frozen account or a
deleted user reaches the devices already paired.
"""

import hashlib
import logging
import re
import threading
import time

logger = logging.getLogger(__name__)

#: AeroFoil access flag -> the Lantern scopes it gives (SPEC.md § 5.7).
#: The library and requests belong to the shop, saves to backup access, cheats to cheat access, and writing a
#: cheat set or administering the server to an administrator.
SYNC_EVERY_S = 5.0
_BAD = re.compile(r'[^A-Za-z0-9._-]')


def scopes_for(user):
    scopes = []
    if user.has_shop_access():
        scopes += ['library', 'requests', 'sync.read', 'sync.write']
    if user.has_backup_access() and not user.frozen:
        scopes.append('saves')
    if user.has_cheat_access():
        scopes.append('cheats')
    if user.has_admin_access() and not user.frozen:
        scopes += ['cheats.write', 'admin']
    return scopes


def lantern_name(username):
    """The Lantern account name of an AeroFoil user: 1 to 64 letters, digits, '.', '_' and '-'. A name that has
    to change keeps a short hash of the original so that two users never share one."""
    username = str(username or '')
    clean = _BAD.sub('_', username)[:64]
    if clean == username and clean:
        return clean
    digest = hashlib.sha256(username.encode('utf-8')).hexdigest()[:6]
    return (clean[:57] or 'user') + '-' + digest


def ensure_account(accounts, user):
    """The Lantern account of an AeroFoil user, created or brought up to date; returns its name."""
    from lantern.server.accounts import ordered_scopes

    name = lantern_name(user.user)
    state = 'frozen' if user.frozen else 'active'
    scopes = ordered_scopes(scopes_for(user))
    have = accounts.user(name)
    if have is None:
        accounts.create_user(name, scopes, state)
    elif (have.scopes, have.state) != (scopes, state):
        accounts.update_user(name, scopes=scopes, state=state)
    return name


class Mirror:
    """Keeps the Lantern accounts equal to the AeroFoil users they come from, at most every few seconds."""

    def __init__(self, accounts, clock=time.monotonic):
        self.accounts = accounts
        self.clock = clock
        self._last = None
        self._lock = threading.Lock()

    def sync(self, force=False):
        from app.db import User

        with self._lock:
            now = self.clock()
            if not force and self._last is not None and now - self._last < SYNC_EVERY_S:
                return
            self._last = now
            users = {lantern_name(u.user): u for u in User.query.all()}
            for account in list(self.accounts.users()):
                user = users.get(account.name)
                if user is None:
                    self.accounts.delete_user(account.name)
                    logger.info('Lantern: the account %s went with its AeroFoil user.', account.name)
                else:
                    ensure_account(self.accounts, user)

    def owner(self, device):
        """The AeroFoil user a Lantern device belongs to, or None."""
        from app.db import User

        for user in User.query.all():
            if lantern_name(user.user) == device.user:
                return user
        return None
