"""The Lantern library of AeroFoil: the hashed, identified files of its database, as Lantern apps.

`AeroFoilLibrary` is the SDK's `Library` (and its `Journal`). It serves a listing kept in memory and rebuilt
by `refresh()` after each pass of the fingerprint worker, so a request never queries the database; and it
remembers what it has announced (`announced.py`), so that `/v1/changes` says what really changed, also across a
restart, and a restart alone changes nothing.

A file is listed once it has a SHA-256 and is claimed by an app. An app's id is made from the file's bytes
(`a_` and 12 hex digits of its SHA-256): the same bytes are the same app, wherever they are. A file that holds
several apps (a bundle) is one app, the base before an update before a DLC, and `includes` the others.
"""

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone

from app.constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD, CACHE_DIR
from app.db import Apps, Files, Titles, app_files, db

logger = logging.getLogger(__name__)

PLATFORM = 'switch'
TYPES = {APP_TYPE_BASE: 'base', APP_TYPE_UPD: 'update', APP_TYPE_DLC: 'dlc'}
ORDER = {'base': 0, 'update': 1, 'dlc': 2}
FORMATS = {'nsp', 'nsz', 'xci', 'xcz'}


#: The titles database gives an age; Lantern reads a rating of a system (SPEC.md § 5.6). Each age is
#: written in a system whose table reads it as that very age, so a cap hides exactly what AeroFoil's would.
RATING_BY_AGE = {
    0: ('USK', '0'), 3: ('PEGI', '3'), 6: ('USK', '6'), 7: ('PEGI', '7'), 10: ('ESRB', 'E10+'), 12: ('PEGI', '12'),
    13: ('ESRB', 'T'), 14: ('CLASSIND', '14'), 16: ('PEGI', '16'), 17: ('ESRB', 'M'), 18: ('PEGI', '18'),
}
IMAGE_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp', 'gif': 'image/gif'}
RECENT = 40


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


def _game_info(title_id):
    try:
        from app import titles

        info = titles.get_game_info(title_id) or {}
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _external(url):
    url = str(url or '').strip()
    if url.startswith('//'):
        url = 'https:' + url
    return url if url.startswith('https://') and len(url) <= 2048 else None


def _cached_names(kind):
    """{TITLE ID: file name} of the images AeroFoil has cached (icons, banners)."""
    folder = os.path.join(CACHE_DIR, kind)
    try:
        return {name.split('.', 1)[0].upper(): name for name in os.listdir(folder) if '.' in name}
    except OSError:
        return {}


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

    families = frozenset({'library', 'titles', 'sections'})

    def __init__(self, journal, announced, base='/api/lantern', on_change=None):
        self.base = base.rstrip('/')
        self._log = journal
        self._announced = announced
        self._on_change = on_change
        self._lock = threading.RLock()
        self._apps = {}
        self._files = {}
        self._titles = {}
        self._sections = []
        self._media = {}
        self._image_memo = {}

    # Refreshing.

    def _local_image(self, kind, title_id, names, media):
        """An image AeroFoil cached for a title, served on this origin; None when there is none."""
        from lantern._generated.models import Image
        from lantern.server.files import FileRef

        name = names.get(title_id.upper())
        if not name:
            return None
        path = os.path.join(CACHE_DIR, kind, name)
        extension = name.rsplit('.', 1)[-1].lower()
        content_type = IMAGE_TYPES.get(extension)
        try:
            info = os.stat(path)
        except OSError:
            return None
        if content_type is None or info.st_size == 0:
            return None
        key = (path, info.st_mtime_ns, info.st_size)
        known = self._image_memo.get(key)
        if known is None:
            digest = hashlib.sha256()
            with open(path, 'rb') as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            width = height = None
            try:
                from PIL import Image as PilImage

                with PilImage.open(path) as picture:
                    width, height = picture.size
            except Exception:
                pass
            known = (digest.hexdigest(), width, height)
            self._image_memo[key] = known
        sha256, width, height = known
        served = f'{sha256}.{"jpg" if extension == "jpeg" else extension}'
        media[served] = FileRef(path, info.st_size, sha256, content_type)
        return Image(url=f'{self.base}/v1/media/{served}', width=width, height=height, sha256=sha256)

    def _title(self, title_id, icons, banners, media):
        from lantern._generated.models import Image, Rating, Title
        from lantern import schema

        info = _game_info(title_id)
        name = str(info.get('name') or '').strip()
        if not name or name == 'Unrecognized':
            name = title_id
        description = str(info.get('description') or '').strip()
        category = str(info.get('category') or '').strip()
        rating = RATING_BY_AGE.get(info.get('rating')) if isinstance(info.get('rating'), int) else None
        icon = self._local_image('icons', title_id, icons, media)
        banner = self._local_image('banners', title_id, banners, media)
        if icon is None and _external(info.get('iconUrl')):
            icon = Image(url=_external(info.get('iconUrl')), external=True)
        if banner is None and _external(info.get('bannerUrl')):
            banner = Image(url=_external(info.get('bannerUrl')), external=True)
        shots = [Image(url=u, external=True) for u in map(_external, info.get('screenshots') or []) if u][:12]
        title = Title(
            platform=PLATFORM,
            title_id=title_id,
            name=name[:200],
            summary=description[:300] or None,
            description=description[:4000] or None,
            tags=[t.strip()[:64] for t in category.split(',') if t.strip()][:32] or None,
            ratings=[Rating(system=rating[0], rating=rating[1])] if rating else None,
            icon=icon,
            banner=banner,
            screenshots=shots or None,
        )
        if schema.definition('title', title.to_json()):
            title = Title(platform=PLATFORM, title_id=title_id, name=name[:200])
            if schema.definition('title', title.to_json()):
                return None
        return title

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
        titles, media, changed = self._refresh_titles(apps)
        sections = self._recent(apps)
        with self._lock:
            self._apps, self._files = apps, files
            self._titles, self._media, self._sections = titles, media, sections
        if (added or removed or any(changed)) and self._on_change is not None:
            try:
                self._on_change(added + changed[0], removed + changed[1])
            except Exception:
                logger.exception('Lantern: a change listener failed.')
        return added, removed

    def _refresh_titles(self, apps):
        """A sheet for each title an app belongs to, the changes recorded; returns (titles, media, (changed, gone))."""
        wanted = sorted({app.title_id for app in apps.values()})
        icons, banners = _cached_names('icons'), _cached_names('banners')
        media, titles, keep = {}, {}, {}
        before = self._announced.get('title')
        changed = 0
        for title_id in wanted:
            title = self._title(title_id, icons, banners, media)
            if title is None:
                continue
            fingerprint = json.dumps(title.to_json(), sort_keys=True)
            key = f'{PLATFORM}/{title_id}'
            titles[title_id] = title
            keep[key] = (fingerprint, (before.get(key) or ('', _now()))[1])
            if key not in before or before[key][0] != fingerprint:
                self._log.record('title', key, PLATFORM)
                changed += 1
        gone = 0
        for key in before.keys() - keep.keys():
            self._log.record('title', key, PLATFORM)
            gone += 1
        self._announced.put('title', keep)
        return titles, media, (changed, gone)

    @staticmethod
    def _recent(apps):
        from lantern._generated.models import Section

        newest = sorted(apps.values(), key=lambda a: (a.added or '', a.id), reverse=True)[:RECENT]
        return [Section(id='recent', title='Recently added', apps=[a.id for a in newest])] if newest else []

    # Library.

    def apps(self):
        with self._lock:
            return list(self._apps.values())

    def titles(self, languages):
        with self._lock:
            return list(self._titles.values())

    def sections(self, languages):
        with self._lock:
            return list(self._sections)

    def media(self, name):
        with self._lock:
            return self._media.get(name)

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
        from lantern._generated.models import RemovedTitle

        with self._lock:
            keys = sorted({key for _, key in self._log.since(after, ('title',), platform)})
            changed, removed = [], []
            for key in keys:
                title_id = key.partition('/')[2]
                if title_id in self._titles:
                    changed.append(self._titles[title_id])
                else:
                    removed.append(RemovedTitle(platform=PLATFORM, title_id=title_id))
            return changed, removed
