import hashlib
import os
import tempfile
import unittest

from flask import Flask

from app.constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from app.db import Apps, Files, Libraries, Titles, db
from app.lantern_native import fingerprints
from app.lantern_native.fingerprints import Fingerprinter, FilesStore, SOURCE, file_entries


def write(path, data, mtime_ns=None):
    with open(path, 'wb') as handle:
        handle.write(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(self.dir.name, 'app.db')
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)
        db.session.add(Libraries(path=self.dir.name))
        db.session.commit()
        self.library_id = Libraries.query.first().id
        self.store = FilesStore()

    def add(self, name, data, mtime_ns=1_000_000_000, app_type=None):
        path = os.path.join(self.dir.name, name)
        write(path, data, mtime_ns)
        file = Files(library_id=self.library_id, filepath=path, filename=name, size=len(data))
        db.session.add(file)
        if app_type is not None:
            title = Titles.query.first() or Titles(title_id='0100ABCD00010000')
            db.session.add(title)
            db.session.flush()
            app = Apps(title_id=title.id, app_id=name, app_version='0', app_type=app_type)
            app.files.append(file)
            db.session.add(app)
        db.session.commit()
        return path

    def run_once(self):
        return Fingerprinter(self.app, self.store).run_once()

    def sha(self, path):
        return Files.query.filter_by(filepath=path).one().sha256

    def test_files_are_hashed_in_place(self):
        a = self.add('a.nsp', b'alpha')
        b = self.add('b.nsp', b'beta')
        self.assertEqual(self.run_once(), 2)
        self.assertEqual(self.sha(a), hashlib.sha256(b'alpha').hexdigest())
        self.assertEqual(self.sha(b), hashlib.sha256(b'beta').hexdigest())
        self.assertEqual(self.store.counts(), {'hashed': 2, 'pending': 0})
        self.assertEqual(self.run_once(), 0)

    def test_a_changed_file_is_hashed_again(self):
        path = self.add('a.nsp', b'alpha')
        self.run_once()
        write(path, b'alphabet', 2_000_000_000)
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.sha(path), hashlib.sha256(b'alphabet').hexdigest())

    def test_the_queue_is_bases_then_updates_then_dlc_then_the_rest(self):
        self.add('dlc.nsp', b'1', app_type=APP_TYPE_DLC)
        self.add('other.nsp', b'1')
        self.add('base.nsp', b'111111', app_type=APP_TYPE_BASE)
        self.add('update.nsp', b'11', app_type=APP_TYPE_UPD)
        self.store.sync(SOURCE, file_entries())
        order = [os.path.basename(row.path) for row in self.store.pending(10)]
        self.assertEqual(order, ['base.nsp', 'update.nsp', 'dlc.nsp', 'other.nsp'])

    def test_a_file_never_seen_is_not_pending_until_synced(self):
        self.add('a.nsp', b'alpha')
        Files.query.update({Files.mtime_ns: None})
        db.session.commit()
        self.assertEqual(self.store.pending(), [])
        self.store.sync(SOURCE, file_entries())
        self.assertEqual(len(self.store.pending()), 1)

    def test_a_hash_is_refused_when_the_file_changed_meanwhile(self):
        path = self.add('a.nsp', b'alpha')
        self.store.sync(SOURCE, file_entries())
        [row] = self.store.pending()
        write(path, b'alphabet', 2_000_000_000)
        self.store.sync(SOURCE, file_entries())
        self.assertFalse(self.store.set_hash(row, 'x' * 64))
        self.assertIsNone(self.sha(path))

    def test_a_vanished_file_stays_pending_and_is_left_to_the_scan(self):
        path = self.add('a.nsp', b'alpha')
        os.remove(path)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.store.counts(), {'hashed': 0, 'pending': 1})

    def test_the_sample_finds_a_file_changed_under_the_same_validators(self):
        path = self.add('a.nsp', b'alpha')
        self.run_once()
        write(path, b'ALPHA', 1_000_000_000)
        Fingerprinter(self.app, self.store).run_once()
        self.assertEqual(self.sha(path), hashlib.sha256(b'ALPHA').hexdigest())

    def test_status_counts_what_is_left(self):
        self.add('a.nsp', b'alpha')
        worker = Fingerprinter(self.app, self.store)
        worker.run_once()
        status = worker.status()
        self.assertEqual((status['state'], status['hashed'], status['pending']), ('idle', 1, 0))

    def test_the_hashed_callback_runs_when_something_was_hashed(self):
        self.add('a.nsp', b'alpha')
        seen = []
        Fingerprinter(self.app, self.store, on_hashed=seen.append).run_once()
        self.assertEqual(seen, [1])


if __name__ == '__main__':
    unittest.main()
