import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile

from flask import Blueprint, Flask
from flask_login import LoginManager

from app import lantern_native
from app.db import User, db
from app.lantern_native.saves import AeroFoilSaveStore

BASE = lantern_native.BASE
TITLE = '0100ABCD00010000'


def archive(text='save data'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('save.bin', text)
    return out.getvalue()


class SaveStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.dir.name, 'app.db'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='t',
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)
        db.session.add(User(user='lea', password='x', admin_access=False, shop_access=True, backup_access=True, cheat_access=True, frozen=False))
        db.session.commit()
        self.folder = os.path.join(self.dir.name, 'saves')
        self.refreshed = []
        self.store = AeroFoilSaveStore(self.folder, refresh=lambda *a: self.refreshed.append(a))

    def save(self, **fields):
        from lantern._generated.models import Save

        data = fields.pop('data', archive())
        values = dict(
            platform='switch', title_id=TITLE, save_id='s_one', created='2026-11-07T10:00:00Z',
            size=len(data), sha256=hashlib.sha256(data).hexdigest(), kind='account', slot='main',
        )
        values.update(fields)
        return Save(**values), data

    def test_a_backup_lands_where_aerofoil_keeps_its_own(self):
        save, data = self.save()
        self.store.keep('lea', save, data)
        path = os.path.join(self.folder, 'lea', TITLE, 'account', 's_one.zip')
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), data)
        with open(os.path.join(self.folder, 'lea', TITLE, 'account', 's_one.json'), encoding='utf-8') as handle:
            meta = json.load(handle)
        self.assertEqual((meta['created_at'], meta['lantern']['slot']), ('2026-11-07T10:00:00Z', 'main'))
        [listed] = self.store.saves('lea')
        self.assertEqual((listed.save_id, listed.kind, listed.slot, listed.sha256), ('s_one', 'account', 'main', save.sha256))
        self.assertEqual(self.refreshed, [(db.session.query(User).first(), TITLE, 'account')])

    def test_every_kind_has_its_folder(self):
        for n, kind in enumerate(('account', 'device', 'bcat', 'cache', 'other')):
            save, data = self.save(save_id=f's_{kind}', kind=kind, created=f'2026-11-07T10:00:0{n}Z')
            self.store.keep('lea', save, data)
            self.assertTrue(os.path.isfile(os.path.join(self.folder, 'lea', TITLE, kind, f's_{kind}.zip')))
        self.assertEqual([s.kind for s in self.store.saves('lea')], ['account', 'device', 'bcat', 'cache', 'other'])

    def test_aerofoils_own_saves_are_listed_with_stable_ids(self):
        folder = os.path.join(self.folder, 'lea', TITLE)
        os.makedirs(folder)
        data = archive('old')
        with open(os.path.join(folder, 'before chapter 4.zip'), 'wb') as handle:
            handle.write(data)
        with open(os.path.join(folder, 'before chapter 4.json'), 'w') as handle:
            json.dump({'note': 'before chapter 4', 'created_ts': 1_700_000_000}, handle)
        with open(os.path.join(self.folder, 'lea', TITLE + '.zip'), 'wb') as handle:
            handle.write(archive('legacy'))
        listed = self.store.saves('lea')
        self.assertEqual(len(listed), 2)
        noted = next(s for s in listed if s.note)
        self.assertRegex(noted.save_id, r'^s_[0-9a-f]{16}$')
        self.assertEqual((noted.kind, noted.slot, noted.created), ('other', 'default', '2023-11-14T22:13:20Z'))
        again = {s.save_id for s in self.store.saves('lea')}
        self.assertEqual(again, {s.save_id for s in listed})
        self.assertEqual(self.store.archive('lea', noted.save_id).sha256, hashlib.sha256(data).hexdigest())

    def test_an_encrypted_backup_is_kept_apart_as_sent(self):
        from lantern._generated.models import Encryption

        save, data = self.save(
            save_id='s_enc', data=b'ciphertext', encryption=Encryption.from_json({
                'scheme': 'xchacha20poly1305-v1', 'key_id': 'k1',
            }),
        )
        self.store.keep('lea', save, data)
        self.assertTrue(os.path.isfile(os.path.join(self.folder, 'lea', TITLE, 'account', 's_enc.enc')))
        [listed] = self.store.saves('lea')
        self.assertIsNotNone(listed.encryption)
        self.assertEqual(self.store.archive('lea', 's_enc').content_type, 'application/octet-stream')

    def test_drop_and_unknown_ids(self):
        save, data = self.save()
        self.store.keep('lea', save, data)
        self.assertFalse(self.store.drop('lea', 's_nope'))
        self.assertTrue(self.store.drop('lea', 's_one'))
        self.assertEqual(self.store.saves('lea'), [])
        self.assertIsNone(self.store.archive('lea', 's_one'))

    def test_an_unknown_account_has_nothing(self):
        self.assertEqual(self.store.saves('ghost'), [])
        self.assertFalse(self.store.drop('ghost', 's_one'))

    def test_uploads_resume_and_become_a_backup(self):
        from lantern.server.saves import UploadSession

        save, data = self.save(save_id='s_up')
        session = UploadSession('u_1', 'lea', save, '2026-11-08T10:00:00Z')
        self.store.open_upload(session)
        self.assertEqual(self.store.append('u_1', data[:10]), 10)
        reopened = AeroFoilSaveStore(self.folder).upload('u_1')  # as after a restart
        self.assertEqual((reopened.offset, reopened.user), (10, 'lea'))
        self.assertEqual(self.store.append('u_1', data[10:]), len(data))
        digest, head = self.store.upload_digest('u_1')
        self.assertEqual((digest, head), (hashlib.sha256(data).hexdigest(), data[:16]))
        self.store.keep_upload('u_1', save)
        self.assertIsNone(self.store.upload('u_1'))
        self.assertEqual(self.store.archive('lea', 's_up').sha256, digest)
        self.assertEqual(self.store.uploads(), [])

    def test_an_abandoned_upload_is_gone(self):
        from lantern.server.saves import UploadSession

        save, _ = self.save()
        self.store.open_upload(UploadSession('u_2', 'lea', save, '2026-11-08T10:00:00Z'))
        self.store.abandon('u_2')
        self.assertEqual(self.store.uploads(), [])

    def test_erasing_the_account_keeps_what_aerofoil_wrote(self):
        theirs = os.path.join(self.folder, 'lea', TITLE)
        os.makedirs(theirs)
        with open(os.path.join(theirs, 'mine.zip'), 'wb') as handle:
            handle.write(archive('aerofoil'))
        save, data = self.save()
        self.store.keep('lea', save, data)
        self.store.forget('lea')
        self.assertEqual([s.kind for s in self.store.saves('lea')], ['other'])
        self.assertTrue(os.path.isfile(os.path.join(theirs, 'mine.zip')))


class ProtocolTests(unittest.TestCase):
    """The SDK's server, over AeroFoil's folder, as a Lantern client uses it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.dir.name, 'app.db'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='t',
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)
        db.session.add(User(user='lea', password='x', admin_access=False, shop_access=True, backup_access=True, cheat_access=True, frozen=False))
        db.session.commit()
        self.folder = os.path.join(self.dir.name, 'saves')
        from app.lantern_native import saves as saves_module

        original = saves_module.SAVE_SYNC_DIR
        saves_module.SAVE_SYNC_DIR = self.folder
        self.addCleanup(setattr, saves_module, 'SAVE_SYNC_DIR', original)
        original_init = saves_module.AeroFoilSaveStore.__init__.__defaults__
        saves_module.AeroFoilSaveStore.__init__.__defaults__ = (self.folder, None)
        self.addCleanup(setattr, saves_module.AeroFoilSaveStore.__init__, '__defaults__', original_init)
        self.lantern = lantern_native.init_app(
            self.app, {'shop': {'lantern_enabled': True, 'public': False}},
            os.path.join(self.dir.name, 'config'), str(uuid.uuid4()), start_worker=False,
        )
        self.addCleanup(setattr, lantern_native, '_instance', None)
        self.lantern.accounts.create_user('lea', ['library', 'saves'])
        self.token, _ = self.lantern.accounts.issue('lea', 'Lea console', ['library', 'saves'])
        self.client = self.app.test_client()
        self.headers = {'Authorization': f'Bearer {self.token}', 'Lantern-Platform': 'switch'}

    def post(self, data, **headers):
        from lantern.digest import format_digest

        base = {
            'Content-Type': 'application/zip', 'Content-Digest': format_digest(hashlib.sha256(data).hexdigest()),
            **self.headers, **headers,
        }
        return self.client.post(f'{BASE}/v1/saves/{TITLE}', data=data, headers=base)

    def test_a_backup_made_through_lantern_is_in_aerofoils_folder(self):
        data = archive('chapter 4')
        made = self.post(data, **{'Lantern-Save-Kind': 'bcat', 'Lantern-Save-Slot': 'main'})
        self.assertEqual(made.status_code, 201, made.data)
        entry = made.get_json()
        self.assertEqual((entry['kind'], entry['slot']), ('bcat', 'main'))
        archive_path = os.path.join(self.folder, 'lea', TITLE, 'bcat', entry['save_id'] + '.zip')
        with open(archive_path, 'rb') as handle:
            self.assertEqual(handle.read(), data)
        listed = self.client.get(f'{BASE}/v1/saves', headers=self.headers).get_json()['saves']
        self.assertEqual([s['save_id'] for s in listed], [entry['save_id']])
        whole = self.client.get(f"{BASE}/v1/saves/{TITLE}/{entry['save_id']}", headers=self.headers)
        self.assertEqual((whole.status_code, whole.data), (200, data))
        gone = self.client.delete(f"{BASE}/v1/saves/{TITLE}/{entry['save_id']}", headers=self.headers)
        self.assertEqual(gone.status_code, 204)
        self.assertFalse(os.path.exists(archive_path))

    def test_a_save_aerofoil_holds_is_offered_to_the_device(self):
        folder = os.path.join(self.folder, 'lea', TITLE)
        os.makedirs(folder)
        with open(os.path.join(folder, 'old.zip'), 'wb') as handle:
            handle.write(archive('old'))
        [entry] = self.client.get(f'{BASE}/v1/saves', headers=self.headers).get_json()['saves']
        self.assertEqual(entry['kind'], 'other')
        got = self.client.get(f"{BASE}/v1/saves/{TITLE}/{entry['save_id']}", headers=self.headers)
        self.assertEqual(got.status_code, 200)

    def test_the_old_backups_are_pruned_beyond_the_limit(self):
        for n in range(12):
            self.assertEqual(self.post(archive(f'v{n}'), **{'Lantern-Save-Kind': 'account', 'Lantern-Save-Slot': 'a'}).status_code, 201)
        listed = self.client.get(f'{BASE}/v1/saves', headers=self.headers).get_json()['saves']
        self.assertEqual(len(listed), 10)


class SaveSyncRoutesTests(unittest.TestCase):
    def test_aerofoil_accepts_the_new_types(self):
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / 'app/app.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        types = next(
            ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'SAVE_TYPES'
        )
        self.assertEqual(types, ('unknown', 'account', 'device', 'bcat', 'cache', 'other'))


if __name__ == '__main__':
    unittest.main()
