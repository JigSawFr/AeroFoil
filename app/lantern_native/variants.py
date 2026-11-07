"""Decompressed variants of compressed packages (SPEC.md § 6.1, Annex B.15).

A console that cannot read `.nsz`/`.xcz` can still install the same game as a plain `.nsp`/`.xci`: AeroFoil
already streams those decompressed on the fly (`compressed_stream.py`). Lantern lists a file with its SHA-256
before it is served, so each such file is decompressed once, in the background, only to hash the bytes it would
send, and the result is kept (`config/lantern-variants.db`) with the size and modification time it holds for.

The bytes sent are the same every time (the virtual container is built from the file alone), which is what makes
that hash true: a test decompresses twice and compares. The variant is a second app of the same game, `nsp` with
nothing required, beside the `nsz` one that `requires` `fs-compression`.
"""

import hashlib
import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

#: Compressed extension -> the plain format its virtual stream is.
PLAIN = {'nsz': 'nsp', 'xcz': 'xci'}


def virtual_hash(path):
    """(SHA-256, size) of the bytes AeroFoil sends for the decompressed form of this file."""
    from app import compressed_stream

    stream, size = compressed_stream.virtual_stream(path)
    digest, count = hashlib.sha256(), 0
    for piece in stream:
        digest.update(piece)
        count += len(piece)
    if count != size:
        raise ValueError(f'{path}: the virtual stream is {count} bytes, not the {size} announced')
    return digest.hexdigest(), count


def reader(path):
    """The `read(first, length)` of a FileRef for the virtual file: the pieces of that range."""
    from app import compressed_stream

    def read(first, length):
        return compressed_stream.virtual_range(path, first, first + length - 1)

    return read


class VirtualStore:
    """What was hashed of each virtual file, for the size and time of the compressed file it comes from."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute(
            'CREATE TABLE IF NOT EXISTS virtual (path TEXT PRIMARY KEY, size INTEGER NOT NULL, '
            'mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL, vsize INTEGER NOT NULL)'
        )

    def close(self):
        with self._lock:
            self._db.close()

    def all(self):
        """{path: (size, mtime_ns, sha256, virtual size)}"""
        with self._lock:
            rows = self._db.execute('SELECT path, size, mtime_ns, sha256, vsize FROM virtual').fetchall()
        return {path: (size, mtime, sha, vsize) for path, size, mtime, sha, vsize in rows}

    def put(self, path, size, mtime_ns, sha256, vsize):
        with self._lock:
            self._db.execute(
                'INSERT OR REPLACE INTO virtual (path, size, mtime_ns, sha256, vsize) VALUES (?, ?, ?, ?, ?)',
                (path, size, mtime_ns, sha256, vsize),
            )

    def keep_only(self, paths):
        """Forgets the files that are no longer in the library."""
        with self._lock:
            known = [row[0] for row in self._db.execute('SELECT path FROM virtual')]
            for path in set(known) - set(paths):
                self._db.execute('DELETE FROM virtual WHERE path = ?', (path,))


class VariantBuilder:
    """Hashes the decompressed form of the hashed `.nsz`/`.xcz` files that have no current result."""

    def __init__(self, store):
        self.store = store

    def wanted(self):
        """[(path, size, mtime_ns)] of the compressed files that are hashed, with no valid result yet."""
        from app.db import Files

        have = self.store.all()
        out = []
        query = Files.query.filter(Files.sha256.isnot(None), Files.mtime_ns.isnot(None)).order_by(Files.size)
        for file in query.all():
            if os.path.splitext(file.filepath)[1].lstrip('.').lower() not in PLAIN:
                continue
            known = have.get(file.filepath)
            if known is None or known[:2] != (file.size, file.mtime_ns):
                out.append((file.filepath, file.size, file.mtime_ns))
        self.store.keep_only(
            [f.filepath for f in Files.query.filter(Files.mtime_ns.isnot(None)).all() if f.filepath in have]
        )
        return out

    def counts(self):
        todo = len(self.wanted())
        return {'done': len(self.store.all()), 'pending': todo}

    def run(self, should_stop=lambda: False, limit=None):
        """Hashes what is wanted, smallest first; how many were done. A file that cannot be read is skipped."""
        done = 0
        for path, size, mtime_ns in self.wanted():
            if should_stop() or (limit is not None and done >= limit):
                break
            try:
                before = os.stat(path)
                if (before.st_size, before.st_mtime_ns) != (size, mtime_ns):
                    continue
                sha256, vsize = virtual_hash(path)
                after = os.stat(path)
            except Exception as error:
                logger.warning('Lantern: no decompressed variant for %s (%s).', path, error)
                continue
            if (after.st_size, after.st_mtime_ns) != (size, mtime_ns):
                continue
            self.store.put(path, size, mtime_ns, sha256, vsize)
            done += 1
        return done
