"""The Lantern library of AeroFoil: the hashed, identified files of its database, as Lantern apps.

`AeroFoilLibrary` is the SDK's `Library` (and its `Journal`). It serves a listing kept in memory and rebuilt
by `refresh()` after each pass of the fingerprint worker, so a request never queries the database; and it
remembers what it has announced (`announced.py`), so that `/v1/changes` says what really changed, also across a
restart, and a restart alone changes nothing.

A file is listed once it has a SHA-256 and is claimed by an app. An app's id is made from the file's bytes
(`a_` and 12 hex digits of its SHA-256): the same bytes are the same app, wherever they are. A file that holds
several apps (a bundle) is one app, the base before an update before a DLC, and `includes` the others.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from app.constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from app.db import Apps, Files, Titles, app_files, db

logger = logging.getLogger(__name__)

PLATFORM = 'switch'
TYPES = {APP_TYPE_BASE: 'base', APP_TYPE_UPD: 'update', APP_TYPE_DLC: 'dlc'}
ORDER = {'base': 0, 'update': 1, 'dlc': 2}
FORMATS = {'nsp', 'nsz', 'xci', 'xcz'}


def _version(value):
    try:
        return max(0, int(str(value or '0').strip() or '0'))
    except ValueError:
        return 0


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _name_of(title_id):
    """The name AeroFoil knows for a title, or None (the titles database may not be loaded)."""
    try:
        from app import titles

        info = titles.get_game_info(title_id) or {}
    except Exception:
        return None
    name = str(info.get('name') or '').strip()
    return name[:200] if name and name != 'Unrecognized' else None


def listing(base):
    """{app id: (App without `added`, filepath)} for what is listable now: read from the database."""
    from lantern._generated.models import App, AppIncludes

    rows = (
        db.session.query(Files, Apps, Titles)
        .join(app_files, Files.id == app_files.c.file_id)
        .join(Apps, Apps.id == app_files.c.app_id)
        .join(Titles, Titles.id == Apps.title_id)
        .filter(Files.sha256.isnot(None))
        .order_by(Files.id, Apps.id)
        .all()
    )
    by_file = {}
    for file, app_row, title in rows:
        by_file.setdefault(file.id, (file, []))[1].append((app_row, title))
    out = {}
    for file, contents in by_file.values():
        extension = str(file.extension or '').lstrip('.').lower()
        if extension not in FORMATS or not file.size or not file.sha256:
            continue
        contents = [c for c in contents if TYPES.get(c[0].app_type) and c[0].app_id and c[1].title_id]
        if not contents:
            continue
        contents.sort(key=lambda c: (ORDER[TYPES[c[0].app_type]], c[0].app_id))
        main, title = contents[0]
        app_id = 'a_' + file.sha256[:12]
        if app_id in out:  # the same bytes in two places: the first one serves
            continue
        includes = [
            AppIncludes(app_id=a.app_id, version_code=_version(a.app_version)) for a, _ in contents[1:16]
        ]
        out[app_id] = (
            App(
                id=app_id,
                platform=PLATFORM,
                title_id=title.title_id,
                app_id=main.app_id,
                type=TYPES[main.app_type],
                version_code=_version(main.app_version),
                name=_name_of(title.title_id),
                size=int(file.size),
                format=extension,
                includes=includes or None,
                sha256=file.sha256,
                file=f'{base}/v1/files/f_{file.sha256[:16]}',
            ),
            file.filepath,
        )
    return out


class Announced:
    """What was told to clients, kept in a SQLite file: (kind, key) -> fingerprint and when it first appeared."""

    def __init__(self, path):
        import sqlite3

        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                'CREATE TABLE IF NOT EXISTS announced ('
                'kind TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, added TEXT NOT NULL, '
                'PRIMARY KEY (kind, key))'
            )

    def get(self, kind):
        with self._lock:
            rows = self._db.execute('SELECT key, fingerprint, added FROM announced WHERE kind = ?', (kind,))
            return {key: (fingerprint, added) for key, fingerprint, added in rows}

    def put(self, kind, items):
        """Makes the kind equal to `items` ({key: (fingerprint, added)})."""
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._db.execute('DELETE FROM announced WHERE kind = ?', (kind,))
                self._db.executemany(
                    'INSERT INTO announced (kind, key, fingerprint, added) VALUES (?, ?, ?, ?)',
                    [(kind, key, fp, added) for key, (fp, added) in items.items()],
                )
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
            self._db.execute('COMMIT')

    def close(self):
        with self._lock:
            self._db.close()


class AeroFoilLibrary:
    """`lantern.server.Library` and `Journal` over AeroFoil's files. Core only for now: apps and their files."""

    families = frozenset({'library'})

    def __init__(self, journal, announced, base='/api/lantern', on_change=None):
        self.base = base.rstrip('/')
        self._log = journal
        self._announced = announced
        self._on_change = on_change
        self._lock = threading.RLock()
        self._apps = {}
        self._files = {}

    # Refreshing.

    def refresh(self, rows=None):
        """Rebuilds the listing from the database (`rows`: for a test), records what changed in the journal and
        remembers it. Call inside an app context. Returns (added, removed)."""
        from lantern.server.files import FileRef

        current = listing(self.base) if rows is None else rows
        before = self._announced.get('app')
        stamp = _now()
        keep, apps, files = {}, {}, {}
        added = removed = 0
        for app_id, (app, path) in current.items():
            fingerprint = json.dumps(app.to_json(), sort_keys=True)
            known = before.get(app_id)
            when = known[1] if known else stamp
            keep[app_id] = (fingerprint, when)
            app.added = when
            apps[app_id] = app
            files['f_' + app.sha256[:16]] = FileRef(path, app.size, app.sha256)
            if known is None or known[0] != fingerprint:
                self._log.record('added', app_id, PLATFORM)
                added += 1
        for app_id in before.keys() - current.keys():
            self._log.record('removed', app_id, PLATFORM)
            removed += 1
        self._announced.put('app', keep)
        with self._lock:
            self._apps, self._files = apps, files
        if (added or removed) and self._on_change is not None:
            try:
                self._on_change(added, removed)
            except Exception:
                logger.exception('Lantern: a change listener failed.')
        return added, removed

    # Library.

    def apps(self):
        with self._lock:
            return list(self._apps.values())

    def titles(self, languages):
        return []

    def homebrew(self, languages):
        return []

    def cheats(self):
        return []

    def file(self, file_id):
        with self._lock:
            return self._files.get(file_id)

    def cheat_file(self, cheat_id):
        return None

    def journal(self):
        return self

    # Journal.

    @property
    def epoch(self):
        return self._log.epoch

    def position(self):
        return self._log.seq

    def oldest(self):
        return self._log.floor

    def app_changes(self, after, platform):
        with self._lock:
            events = self._log.since(after, ('added', 'removed'), platform)
            added = list(dict.fromkeys(key for kind, key in events if kind == 'added' and key in self._apps))
            removed = sorted({key for kind, key in events if kind == 'removed' and key not in self._apps})
            return [self._apps[key] for key in added], removed

    def title_changes(self, after, platform, languages):
        return [], []
