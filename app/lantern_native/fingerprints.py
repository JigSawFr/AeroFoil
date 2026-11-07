"""SHA-256 of every file in the library, kept in the `files` table.

A Lantern library lists a file with its SHA-256 (SPEC.md § 6.1), and reading a whole library to find out takes
hours: the hash is computed once, in the background, and kept with the validators it was computed under (the size
and the modification time, `files.size` and `files.mtime_ns`). A file whose validators change is hashed again;
a file is not listed by Lantern until it is hashed.

The work is the SDK's (`lantern.server.file_index`); `FilesStore` is its storage on AeroFoil's own table, so the
hashes sit beside the rest of what AeroFoil knows of a file and nothing is kept twice.
"""

import logging
import os
import threading
import time
from pathlib import Path

from sqlalchemy import func, update

from app.constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from app.db import Files, app_files, Apps, db

logger = logging.getLogger(__name__)

#: The one source of the SDK's index: AeroFoil's libraries are all in the `files` table.
SOURCE = 'aerofoil'
#: A file's `filepath` is absolute: joined to this root it stays as it is.
ROOTS = {SOURCE: Path('')}
#: Bases first, then updates, then DLC, then what no app claims.
PRIORITY = {APP_TYPE_BASE: 0, APP_TYPE_UPD: 1, APP_TYPE_DLC: 2}
UNCLAIMED = 3
#: Seconds between two runs when nothing wakes the worker.
INTERVAL_S = 15 * 60
#: Seconds before trying again after a pass failed (the database not ready, a disk gone for a moment).
RETRY_S = 30
BATCH = 500
#: Seconds at most between two refreshes of the Lantern listing while hashing.
REFRESH_EVERY_S = 60


def _entry(path, size, mtime_ns, priority):
    from lantern.server.file_index import Entry

    return Entry(path, size, mtime_ns, priority)


class FilesStore:
    """`lantern.server.file_index.IndexStore` on the `files` table. Call it inside an app context.

    AeroFoil's own scan adds and removes the rows; this only records what the files look like now (`sync`) and
    their hash, so `sync` never adds or removes a file."""

    def sync(self, source, entries):
        from lantern.server.file_index import SyncResult

        entries = list(entries)
        added = changed = 0
        for start in range(0, len(entries), BATCH):
            batch = {e.path: e for e in entries[start:start + BATCH]}
            rows = Files.query.filter(Files.filepath.in_(list(batch))).all()
            for row in rows:
                entry = batch[row.filepath]
                if row.mtime_ns is None:
                    added += 1
                elif (row.mtime_ns, row.size) != (entry.mtime_ns, entry.size):
                    changed += 1
                    row.sha256 = None
                elif row.sha256 is not None and row.hash_priority == entry.priority:
                    continue
                row.mtime_ns, row.size = entry.mtime_ns, entry.size
                row.hash_priority = entry.priority
            db.session.commit()
        return SyncResult(added=added, changed=changed, removed=0)

    @staticmethod
    def _row(file):
        from lantern.server.file_index import Row

        return Row(SOURCE, file.filepath, file.size or 0, file.mtime_ns or 0, file.sha256)

    def pending(self, limit=100, sources=None):
        if sources is not None and SOURCE not in sources:
            return []
        files = (
            Files.query.filter(Files.sha256.is_(None), Files.mtime_ns.isnot(None))
            .order_by(Files.hash_priority, Files.size, Files.filepath)
            .limit(limit)
            .all()
        )
        return [self._row(f) for f in files]

    def set_hash(self, row, sha256):
        result = db.session.execute(
            update(Files)
            .where(Files.filepath == row.path, Files.size == row.size, Files.mtime_ns == row.mtime_ns)
            .values(sha256=sha256)
        )
        db.session.commit()
        return result.rowcount == 1

    def hashed(self, source=None):
        if source not in (None, SOURCE):
            return []
        files = Files.query.filter(Files.sha256.isnot(None)).order_by(Files.filepath).all()
        return [self._row(f) for f in files]

    def rows(self, source=None):
        if source not in (None, SOURCE):
            return []
        return [self._row(f) for f in Files.query.order_by(Files.filepath).all()]

    def counts(self):
        done = db.session.query(func.count(Files.sha256)).scalar() or 0
        total = db.session.query(func.count(Files.id)).scalar() or 0
        return {'hashed': int(done), 'pending': int(total - done)}

    def pending_bytes(self):
        total = db.session.query(func.coalesce(func.sum(Files.size), 0)).filter(Files.sha256.is_(None)).scalar()
        return int(total or 0)

    def rehash_files(self, files):
        paths = [path for source, path in files if source == SOURCE]
        count = 0
        for start in range(0, len(paths), BATCH):
            result = db.session.execute(
                update(Files)
                .where(Files.filepath.in_(paths[start:start + BATCH]))
                .values(sha256=None, hash_priority=-1)
            )
            count += result.rowcount
        db.session.commit()
        return count


def file_entries():
    """What the files in the table look like on disk now. A file that is gone is left out: AeroFoil's own scan
    removes its row."""
    last = 0
    while True:
        batch = (
            db.session.query(Files.id, Files.filepath)
            .filter(Files.id > last)
            .order_by(Files.id)
            .limit(BATCH)
            .all()
        )
        if not batch:
            return
        last = batch[-1][0]
        types = {}
        for file_id, app_type in (
            db.session.query(app_files.c.file_id, Apps.app_type)
            .join(Apps, Apps.id == app_files.c.app_id)
            .filter(app_files.c.file_id.in_([b[0] for b in batch]))
        ):
            types[file_id] = min(types.get(file_id, UNCLAIMED), PRIORITY.get(app_type, UNCLAIMED))
        for file_id, path in batch:
            try:
                info = os.stat(path)
            except OSError:
                continue
            yield _entry(path, info.st_size, info.st_mtime_ns, types.get(file_id, UNCLAIMED))


class Fingerprinter:
    """The background worker: refreshes the validators, hashes what is pending, checks a sample. Woken by
    `kick()` (the library changed) and by a timer."""

    def __init__(self, app, store=None, on_hashed=None, on_pass=None, variants=None):
        self.app = app
        self.variants = variants
        self.on_pass = on_pass
        self.store = store or FilesStore()
        self.on_hashed = on_hashed
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._failed = False
        self._status = {'state': 'idle', 'hashed': 0, 'pending': 0, 'pending_bytes': 0, 'updated_at': None}
        self._lock = threading.Lock()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name='lantern-fingerprints', daemon=True)
            self._thread.start()
            self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def kick(self):
        self._wake.set()

    def status(self):
        with self._lock:
            return dict(self._status)

    def _set(self, **values):
        with self._lock:
            self._status.update(values, updated_at=time.time())

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(RETRY_S if self._failed else INTERVAL_S)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                with self.app.app_context():
                    self.run_once()
                self._failed = False
            except Exception:
                logger.exception('Lantern fingerprinting failed; it tries again in %d seconds.', RETRY_S)
                self._failed = True
                self._set(state='error')

    def run_once(self):
        """One pass, inside an app context: the validators, then the hashes, then a sample."""
        from lantern.server.file_index import hash_pending, verify_sample

        self._set(state='scanning')
        self.store.sync(SOURCE, file_entries())
        # A sample first, so a file found changed is hashed again in this very pass
        verify_sample(self.store, ROOTS, 3)
        done = 0
        last = 0.0
        while not self._stop.is_set():
            # What is hashed so far is listed without waiting for the whole library (at most once a minute)
            if time.monotonic() - last >= REFRESH_EVERY_S:
                self._pass(done)
                last = time.monotonic()
            self._set(state='hashing', **self._counts())
            count = hash_pending(self.store, ROOTS, limit=25, should_stop=self._stop.is_set)
            if count == 0:
                break
            done += count
        self._set(state='idle', **self._counts())
        self._pass(done)
        if self.variants is not None and not self._stop.is_set():
            # After everything is listed: decompress the .nsz/.xcz once each, to hash their plain form
            self._set(state='variants', **self._counts())
            made = self.variants.run(self._stop.is_set)
            if made:
                self._pass(made)
                done += made
        self._set(state='idle', **self._counts())
        if done:
            logger.info('Lantern: hashed %d file(s).', done)
            if self.on_hashed is not None:
                self.on_hashed(done)
        return done

    def _pass(self, done):
        if self.on_pass is not None:
            self.on_pass(done)

    def _counts(self):
        counts = self.store.counts()
        extra = {'variants': self.variants.counts()} if self.variants is not None else {}
        return {
            **extra,
            'hashed': counts['hashed'],
            'pending': counts['pending'],
            'pending_bytes': self.store.pending_bytes(),
        }
