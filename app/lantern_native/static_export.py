"""A static copy of the library (SPEC.md § 21): plain files and pages that any web host, NAS or USB key can serve.

The SDK writes it (`lantern.static.export`) from the very library the server serves: the same apps, titles and
cheats, each file copied and checked by its SHA-256. It goes to `data/lantern-export/<name>/` and takes as long as
copying the library takes, so it runs in the background and reports its progress. Decompressed variants are left
out (they exist only as a stream); the compressed originals are in.
"""

import logging
import os
import re
import threading
import time

from app.constants import DATA_DIR

logger = logging.getLogger(__name__)

EXPORT_DIR = os.path.join(DATA_DIR, 'lantern-export')
NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


class OnDisk:
    """The library as the export reads it: only what is a file on this disk."""

    def __init__(self, library):
        self._library = library
        self.families = frozenset(library.families & {'library', 'titles', 'cheats'})

    def apps(self):
        for app in self._library.apps():
            ref = self._library.file(app.file.rsplit('/', 1)[-1])
            if ref is not None and ref.read is None and ref.path:
                yield app

    def titles(self, languages):
        return self._library.titles(languages)

    def homebrew(self, languages):
        return []

    def cheats(self):
        return self._library.cheats()

    def file(self, file_id):
        return self._library.file(file_id)

    def cheat_file(self, cheat_id):
        return self._library.cheat_file(cheat_id)

    def chunk_index(self, sha256):
        return self._library.chunk_index(sha256)


class Exporter:
    def __init__(self, lantern, root=None):
        self.lantern = lantern
        self.root = root or EXPORT_DIR
        self._lock = threading.Lock()
        self._status = {'state': 'idle'}

    def status(self):
        with self._lock:
            return dict(self._status)

    def start(self, name='library'):
        """Starts an export in the background; False when one is running. ValueError for a name that is not a
        plain folder name."""
        if not NAME.match(name or ''):
            raise ValueError('The name of an export is 1 to 64 letters, digits, ".", "_" and "-".')
        with self._lock:
            if self._status['state'] == 'running':
                return False
            self._status = {'state': 'running', 'name': name, 'started': time.time()}
        threading.Thread(target=self._run, args=(name,), name='lantern-export', daemon=True).start()
        return True

    def _run(self, name):
        from lantern.static import export

        destination = os.path.join(self.root, name)
        try:
            os.makedirs(destination, exist_ok=True)
            summary = export(
                OnDisk(self.lantern.library), destination, uid=self.lantern.uid, name=self.lantern.name,
                software=('AeroFoil', 'native'),
            )
            result = {'state': 'done', 'path': destination, 'documents': summary['documents'], 'files': summary['files']}
        except Exception as error:
            logger.exception('Lantern: the static export failed.')
            result = {'state': 'failed', 'error': str(error)[:300]}
        with self._lock:
            self._status = {**result, 'name': name, 'finished': time.time()}
