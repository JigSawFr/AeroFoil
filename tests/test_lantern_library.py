import hashlib
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

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

    def test_an_app_the_titles_database_does_not_know_is_named_after_its_file(self):
        self.add('Frost Garden [0100ABCD00010000][v0].nsp', b'game')
        self.refresh()
        [app] = self.get('/v1/apps').get_json()['items']
        self.assertEqual(app['name'], 'Frost Garden')

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
        self.assertEqual((doc['features']['titles'], doc['features']['sections']), (1, 1))

    def info(self, **values):
        base = {'name': 'Frost Valley', 'description': 'A cold adventure. ' * 30, 'category': 'Adventure, Puzzle',
                'rating': 12, 'iconUrl': 'https://cdn.example.org/icon.jpg', 'bannerUrl': '',
                'screenshots': ['https://cdn.example.org/1.jpg', 'http://insecure.example.org/2.jpg']}
        base.update(values)
        return patch('app.titles.get_game_info', return_value=base)

    def test_a_title_has_its_sheet(self):
        self.add('game.nsp', b'game')
        with self.info():
            self.refresh()
        [title] = self.get('/v1/titles', query_string={'ids': TITLE}).get_json()['titles']
        self.assertEqual((title['title_id'], title['name'], title['tags']), (TITLE, 'Frost Valley', ['Adventure', 'Puzzle']))
        self.assertEqual(title['ratings'], [{'system': 'PEGI', 'rating': '12'}])
        self.assertEqual(title['icon'], {'url': 'https://cdn.example.org/icon.jpg', 'external': True})
        self.assertEqual([s['url'] for s in title['screenshots']], ['https://cdn.example.org/1.jpg'])
        self.assertLessEqual(len(title['summary']), 300)

    def test_a_rating_cap_hides_what_is_above_it(self):
        self.add('game.nsp', b'game')
        with self.info(rating=16):
            self.refresh()
        self.assertEqual(len(self.get('/v1/titles', query_string={'ids': TITLE}).get_json()['titles']), 1)
        from lantern._generated.models import RatingCap

        self.lantern.accounts.create_user('kid', ['library'])
        token, _ = self.lantern.accounts.issue('kid', 'tablet', ['library'], rating_cap=RatingCap(age=12))
        shown = self.get('/v1/titles', query_string={'ids': TITLE}, headers={'Authorization': f'Bearer {token}'}).get_json()['titles']
        self.assertEqual(shown, [])

    def test_a_cached_image_is_served_from_here(self):
        self.add('game.nsp', b'game')
        icons = os.path.join(self.dir.name, 'cache', 'icons')
        os.makedirs(icons)
        picture = b'\x89PNG\r\n\x1a\nnot really a picture'
        with open(os.path.join(icons, TITLE + '.png'), 'wb') as handle:
            handle.write(picture)
        with self.info(), patch('app.lantern_native.library.CACHE_DIR', os.path.join(self.dir.name, 'cache')):
            self.refresh()
        [title] = self.get('/v1/titles', query_string={'ids': TITLE}).get_json()['titles']
        sha = hashlib.sha256(picture).hexdigest()
        self.assertEqual(title['icon']['sha256'], sha)
        self.assertEqual(title['icon']['url'], f'{BASE}/v1/media/{sha}.png')
        served = self.client.get(title['icon']['url'])
        self.assertEqual((served.status_code, served.data, served.headers['Content-Type']), (200, picture, 'image/png'))

    def test_title_changes_follow_the_sheet(self):
        self.add('game.nsp', b'game')
        with self.info():
            self.refresh()
        token = self.get('/v1/titles/changes').get_json()['since']
        with self.info(name='Frost Valley II'):
            self.refresh()
        later = self.get('/v1/titles/changes', query_string={'since': token}).get_json()
        self.assertEqual([t['name'] for t in later['titles']], ['Frost Valley II'])
        with self.info():
            self.refresh()
            self.assertEqual(self.refresh(), (0, 0))

    def test_recently_added_is_a_shelf(self):
        self.add('a.nsp', b'aaa')
        self.refresh()
        [shelf] = self.get('/v1/sections').get_json()['sections']
        self.assertEqual((shelf['id'], len(shelf['apps'])), ('recent', 1))


if __name__ == '__main__':
    unittest.main()
