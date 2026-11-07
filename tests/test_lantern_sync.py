import hashlib
import os
import tempfile
import unittest
import uuid

from flask import Flask

from app import lantern_native
from app.db import User, db

BASE = lantern_native.BASE
TITLE = '0100ABCD00010000'
NOW = '2026-11-07T10:00:00.000Z'


class SyncAndPeersTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = os.path.join(self.dir.name, 'config')
        db_path = 'sqlite:///' + os.path.join(self.dir.name, 'app.db')
        self.saves = os.path.join(self.dir.name, 'saves')
        from app.lantern_native import saves as saves_module

        original = saves_module.AeroFoilSaveStore.__init__.__defaults__
        saves_module.AeroFoilSaveStore.__init__.__defaults__ = (self.saves, None)
        self.addCleanup(setattr, saves_module.AeroFoilSaveStore.__init__, '__defaults__', original)
        self.app = self.boot(db_path, first=True)
        db.session.add(User(user='lea', password='x', admin_access=False, shop_access=True, backup_access=True, cheat_access=True, frozen=False))
        db.session.commit()
        self.lantern.accounts.create_user('lea', ['library', 'saves', 'sync.read', 'sync.write'])
        self.token, _ = self.lantern.accounts.issue('lea', 'console', ['library', 'saves', 'sync.read', 'sync.write'])
        self.headers = {'Authorization': f'Bearer {self.token}', 'Lantern-Platform': 'switch'}

    def boot(self, db_path, first=False):
        app = Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI=db_path, SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        if not first:
            self.ctx.pop()
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.lantern = lantern_native.init_app(
            app, {'shop': {'lantern_enabled': True, 'public': False}}, self.config, str(uuid.uuid4()),
            start_worker=False,
        )
        self.addCleanup(setattr, lantern_native, '_instance', None)
        self.client = app.test_client()
        return app

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_the_handshake_offers_sync_peers_and_encrypted_saves(self):
        features = self.client.get(BASE + '/v1', headers=self.headers).get_json()['features']
        self.assertEqual((features['sync'], features['peers'], features['encrypted_saves']), (1, 1, 1))

    def test_sync_keeps_wishes_across_a_restart(self):
        generation = self.client.get(BASE + '/v1/sync', headers=self.headers).get_json()['generation']
        item = {'key': f'switch/{TITLE}', 'value': {'platform': 'switch', 'title_id': TITLE}, 'updated': NOW}
        done = self.client.patch(
            BASE + '/v1/sync/wishes', json={'schema': 1, 'generation': generation, 'items': [item]}, headers=self.headers
        )
        self.assertEqual(done.status_code, 200, done.data)
        lantern_native._instance = None
        self.boot('sqlite:///' + os.path.join(self.dir.name, 'app.db'))
        again = self.client.get(BASE + '/v1/sync/wishes', headers=self.headers).get_json()
        self.assertEqual(again['generation'], generation)
        self.assertEqual([i['key'] for i in again['items']], [f'switch/{TITLE}'])

    def test_the_grant_key_survives_a_restart(self):
        before = self.client.get(BASE + '/v1').get_json()['peers']['key']
        lantern_native._instance = None
        self.boot('sqlite:///' + os.path.join(self.dir.name, 'app.db'))
        self.assertEqual(self.client.get(BASE + '/v1').get_json()['peers']['key'], before)

    def test_an_encrypted_backup_is_kept_as_sent_and_not_shown_as_a_zip(self):
        from lantern import e2e

        key = e2e.SaveKey(bytes(range(32)))
        sealed = e2e.encrypt(key, b'PK\x03\x04' + b'save' * 100)
        from lantern.digest import format_digest

        made = self.client.post(
            f'{BASE}/v1/saves/{TITLE}', data=sealed,
            headers={
                **self.headers, 'Content-Type': 'application/octet-stream',
                'Content-Digest': format_digest(hashlib.sha256(sealed).hexdigest()),
                'Lantern-Save-Encryption': e2e.encryption_header(key.key_id), 'Lantern-Save-Kind': 'account',
            },
        )
        self.assertEqual(made.status_code, 201, made.data)
        entry = made.get_json()
        self.assertEqual(entry['encryption'], {'scheme': e2e.SCHEME, 'key_id': key.key_id})
        stored = os.path.join(self.saves, 'lea', TITLE, 'account', entry['save_id'] + '.enc')
        with open(stored, 'rb') as handle:
            self.assertEqual(handle.read(), sealed)
        back = self.client.get(f"{BASE}/v1/saves/{TITLE}/{entry['save_id']}", headers=self.headers)
        self.assertEqual((back.data, back.headers['Content-Type']), (sealed, 'application/octet-stream'))
        self.assertEqual(e2e.decrypt(key, back.data), b'PK\x03\x04' + b'save' * 100)


if __name__ == '__main__':
    unittest.main()
