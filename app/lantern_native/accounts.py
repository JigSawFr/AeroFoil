"""Lantern accounts are AeroFoil's users.

A device is paired by a signed-in AeroFoil user, who approves it on AeroFoil's own page; the Lantern account it
belongs to carries that user's name and what they may do (scopes), so a change of access, a frozen account or a
deleted user reaches the devices already paired.
"""

import dataclasses
import hashlib
import json
import logging
import os
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

    def __init__(self, accounts, clock=time.monotonic, names_file=None):
        self.accounts = accounts
        self.clock = clock
        self._last = None
        self._lock = threading.Lock()
        #: The accounts this mirror made, kept in a file: an account an administrator made by hand is not one
        #: of AeroFoil's users, and is left alone.
        self._names_file = names_file
        self._names = self._read_names()

    def _read_names(self):
        if not self._names_file:
            return None
        try:
            with open(self._names_file, encoding='utf-8') as handle:
                return set(json.load(handle))
        except (OSError, ValueError):
            return set()

    def _write_names(self):
        if self._names_file:
            with open(self._names_file, 'w', encoding='utf-8') as handle:
                json.dump(sorted(self._names), handle)

    def sync(self, force=False):
        from app.db import User

        with self._lock:
            now = self.clock()
            if not force and self._last is not None and now - self._last < SYNC_EVERY_S:
                return
            self._last = now
            users = {lantern_name(u.user): u for u in User.query.all()}
            caps = getattr(self.accounts, 'caps', None)
            if caps is not None:
                caps.clear()
                caps.update({n: u.max_rating for n, u in users.items() if isinstance(u.max_rating, int)})
            for account in list(self.accounts.users()):
                user = users.get(account.name)
                if user is None:
                    if self._names is None or account.name in self._names:
                        self.accounts.delete_user(account.name)
                        if self._names is not None:
                            self._names.discard(account.name)
                            self._write_names()
                        logger.info('Lantern: the account %s went with its AeroFoil user.', account.name)
                else:
                    ensure_account(self.accounts, user)
                    self.remember(account.name)

    def remember(self, name):
        """Notes that an account is made from an AeroFoil user (it is deleted with them)."""
        if self._names is not None and name not in self._names:
            self._names.add(name)
            self._write_names()

    def owner(self, device):
        """The AeroFoil user a Lantern device belongs to, or None."""
        from app.db import User

        for user in User.query.all():
            if lantern_name(user.user) == device.user:
                return user
        return None


def make_accounts(path):
    """The SDK's SQLite accounts, with AeroFoil's age cap of each user applied to their devices."""
    from lantern.server import SqliteAccounts

    class AeroFoilAccounts(SqliteAccounts):
        """A user's `max_rating` is a cap on every one of their devices, whenever it was set: the stricter of it
        and the device's own cap (an invitation's). The caps come from `Mirror.sync`."""

        caps = {}

        def _capped(self, device):
            from lantern._generated.models import RatingCap
            from lantern.ratings import cap_age

            age = None if device is None else self.caps.get(device.user)
            if device is None or age is None:
                return device
            own = cap_age(device.rating_cap) if device.rating_cap is not None else None
            if own is not None and own <= age:
                return device
            return dataclasses.replace(device, rating_cap=RatingCap(age=age))

        def device_of(self, token):
            device = super().device_of(token)
            return self._capped(device) if device is not None else None

        def devices(self, user=None):
            return [self._capped(d) for d in super().devices(user)]

    return AeroFoilAccounts(path)
