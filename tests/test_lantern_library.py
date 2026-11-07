import hashlib
import os
import tempfile
import unittest
import uuid

from flask import Flask

from app import lantern_native
from app.constants import APP_TYPE_BASE, APP_TYPE_DLC, APP_TYPE_UPD
from app.db import Apps, Files, Libraries, Titles, db
from app.lantern_native.fingerprints import FilesStore, Fingerprinter

BASE = lantern_native.BASE
TITLE = '0100ABCD00010000'
BASE_ID, UPDATE_ID, DLC_ID = TITLE, '0100ABCD00010800', '0100ABCD00011001'


class LibraryTests(unittest.TestCase):
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
        self.config = os.path.join(self.dir.name, 'config')
        self.boot()

    def boot(self):
        """Starts Lantern as AeroFoil does, without its worker thread."""
        self.web = Flask('lantern')
        found = lantern_native.init_app(
            self.web, {'shop': {'lantern_enabled': True, 'public': True}}, self.config, str(uuid.uuid4()),
            start_worker=False,
        )
        self.lantern = found
        self.library = found.library
        self.client = self.web.test_client()
        self.addCleanup(setattr, lantern_native, '_instance', None)

    def add(self, name, data, apps=((BASE_ID, '0', APP_TYPE_BASE),), hashed=True):
        path = os.path.join(self.dir.name, name)
        with open(path, 'wb') as handle:
            handle.write(data)
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        file = Files(
            library_id=self.library_id, filepath=path, filename=name, extension=name.rsplit('.', 1)[-1],
            size=len(data), identified=True,
        )
        db.session.add(file)
        title = Titles.query.filter_by(title_id=TITLE).first()
        if title is None:
            title = Titles(title_id=TITLE)
            db.session.add(title)
            db.session.flush()
        for app_id, version, app_type in apps:
            row = Apps.query.filter_by(app_id=app_id, app_version=version).first()
            if row is None:
                row = Apps(title_id=title.id, app_id=app_id, app_version=version, app_type=app_type)
                db.session.add(row)
            row.files.append(file)
        db.session.commit()
        if hashed:
            Fingerprinter(self.app, FilesStore()).run_once()
        return path

    def refresh(self):
        return self.library.refresh()

    def get(self, path, **kw):
        return self.client.get(BASE + path, **kw)

    def test_nothing_is_listed_until_a_file_is_hashed(self):
        self.add('game.nsp', b'game', hashed=False)
        self.refresh()
        self.assertEqual(self.get('/v1/apps').get_json()['items'], [])

    def test_a_hashed_file_is_listed_and_served(self):
        self.add('game.nsp', b'game bytes')
        self.assertEqual(self.refresh(), (1, 0))
        [app] = self.get('/v1/apps').get_json()['items']
        sha = hashlib.sha256(b'game bytes').hexdigest()
        self.assertEqual(
            (app['id'], app['platform'], app['title_id'], app['app_id'], app['type'], app['version_code']),
            ('a_' + sha[:12], 'switch', TITLE, BASE_ID, 'base', 0),
        )
        self.assertEqual((app['sha256'], app['size'], app['format']), (sha, 10, 'nsp'))
        self.assertTrue(app['added'])
        download = self.client.get(app['file'])
        self.assertEqual((download.status_code, download.data), (200, b'game bytes'))
        ranged = self.client.get(app['file'], headers={'Range': 'bytes=2-5'})
        self.assertEqual((ranged.status_code, ranged.data), (206, b'me b'))
        self.assertEqual(self.client.get(BASE + '/v1/files/f_nope').status_code, 404)

    def test_a_bundle_is_one_app_that_includes_the_others(self):
        self.add(
            'bundle.nsp', b'bundle',
            apps=((DLC_ID, '0', APP_TYPE_DLC), (UPDATE_ID, '65536', APP_TYPE_UPD), (BASE_ID, '0', APP_TYPE_BASE)),
        )
        self.refresh()
        [app] = self.get('/v1/apps').get_json()['items']
        self.assertEqual((app['app_id'], app['type']), (BASE_ID, 'base'))
        self.assertEqual(
            [(i['app_id'], i['version_code']) for i in app['includes']], [(UPDATE_ID, 65536), (DLC_ID, 0)]
        )

    def test_the_same_bytes_in_two_places_are_one_app(self):
        self.add('one.nsp', b'same')
        self.add('copy.nsp', b'same')
        self.refresh()
        self.assertEqual(len(self.get('/v1/apps').get_json()['items']), 1)

    def test_what_is_missing(self):
        self.add('base.nsp', b'base')
        self.add('update.nsp', b'update', apps=((UPDATE_ID, '65536', APP_TYPE_UPD),))
        self.refresh()
        answer = self.client.post(
            BASE + '/v1/apps/missing',
            json={'schema': 1, 'installed': [{'app_id': BASE_ID, 'version_code': 0}], 'want': ['update']},
            headers={'Lantern-Platform': 'switch'},
        )
        self.assertEqual([a['app_id'] for a in answer.get_json()['items']], [UPDATE_ID])

    def test_changes_say_what_was_added_and_removed(self):
        first = self.add('a.nsp', b'aaa')
        self.refresh()
        token = self.get('/v1/changes').get_json()['since']
        self.add('b.nsp', b'bbb')
        os.remove(first)
        Files.query.filter_by(filepath=first).delete()
        db.session.commit()
        self.assertEqual(self.refresh(), (1, 1))
        later = self.get('/v1/changes', query_string={'since': token}).get_json()
        self.assertEqual([a['sha256'] for a in later['added']], [hashlib.sha256(b'bbb').hexdigest()])
        self.assertEqual(later['removed'], ['a_' + hashlib.sha256(b'aaa').hexdigest()[:12]])

    def test_a_restart_changes_nothing(self):
        self.add('a.nsp', b'aaa')
        self.refresh()
        token = self.get('/v1/changes').get_json()['since']
        added_at = self.get('/v1/apps').get_json()['items'][0]['added']
        lantern_native._instance = None
        self.boot()
        self.assertEqual(self.refresh(), (0, 0))
        later = self.get('/v1/changes', query_string={'since': token}).get_json()
        self.assertEqual((later['added'], later['removed']), ([], []))
        self.assertEqual(self.get('/v1/apps').get_json()['items'][0]['added'], added_at)

    def test_the_handshake_offers_the_library_and_changes(self):
        doc = self.get('/v1').get_json()
        self.assertEqual((doc['features']['library'], doc['features']['changes']), (1, 1))
        self.assertEqual(doc['features']['titles'], 0)


if __name__ == '__main__':
    unittest.main()
