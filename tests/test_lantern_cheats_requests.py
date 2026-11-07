import hashlib
import os
import tempfile
import unittest
import uuid

from flask import Flask

from app import cheats as aerofoil_cheats
from app import lantern_native
from app.db import TitleRequestUsers, TitleRequests, Titles, User, db

BASE = lantern_native.BASE
TITLE = '0100ABCD00010000'
BUILD = '0123456789ABCDEF'
TEXT = '[Moon jump]\n04000000 00ABC010 0000FFFF\n'


class Harness(unittest.TestCase):
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
        self.lea = self.user('lea', admin_access=True)
        self.noor = self.user('noor')
        cheats_dir = os.path.join(self.dir.name, 'cheats')
        self.original = aerofoil_cheats.CHEATS_DIR
        aerofoil_cheats.CHEATS_DIR = cheats_dir
        self.addCleanup(setattr, aerofoil_cheats, 'CHEATS_DIR', self.original)
        self.lantern = lantern_native.init_app(
            self.app, {'shop': {'lantern_enabled': True, 'public': False}},
            os.path.join(self.dir.name, 'config'), str(uuid.uuid4()), start_worker=False,
        )
        self.addCleanup(setattr, lantern_native, '_instance', None)
        self.client = self.app.test_client()
        self.leas = self.device('lea', ['library', 'requests', 'cheats', 'cheats.write', 'admin'])
        self.noors = self.device('noor', ['library', 'requests', 'cheats'])

    def user(self, name, **flags):
        values = dict(admin_access=False, shop_access=True, backup_access=True, cheat_access=True, frozen=False)
        values.update(flags)
        user = User(user=name, password='x', **values)
        db.session.add(user)
        db.session.commit()
        return user

    def device(self, name, scopes):
        self.lantern.accounts.create_user(name, scopes)
        token, _ = self.lantern.accounts.issue(name, f'{name} console', scopes)
        return {'Authorization': f'Bearer {token}', 'Lantern-Platform': 'switch'}


class CheatTests(Harness):
    body = {'schema': 1, 'platform': 'switch', 'title_id': TITLE, 'build_id': BUILD, 'source': 'Lea', 'note': 'Moon jump', 'text': TEXT}

    def test_a_cheat_written_through_lantern_is_aerofoils_file(self):
        made = self.client.post(BASE + '/v1/cheats', json=self.body, headers=self.leas)
        self.assertEqual(made.status_code, 201, made.data)
        cheat = made.get_json()
        self.assertEqual((cheat['id'], cheat['build_id'], cheat['source'], cheat['note']), (f'cs_{TITLE}-{BUILD}', BUILD, 'Lea', 'Moon jump'))
        with open(os.path.join(aerofoil_cheats.CHEATS_DIR, TITLE, f'{BUILD}.txt'), encoding='utf-8') as handle:
            self.assertEqual(handle.read(), TEXT)
        self.assertEqual(aerofoil_cheats.list_cheats()[0]['note'], 'Moon jump')
        text = self.client.get(f"{BASE}/v1/cheats/{cheat['id']}", headers=self.noors)
        self.assertEqual((text.status_code, text.data), (200, TEXT.encode()))

    def test_a_cheat_aerofoil_holds_is_listed(self):
        aerofoil_cheats.save_cheat(TITLE, BUILD, TEXT.encode())
        [cheat] = self.client.get(BASE + '/v1/cheats', query_string={'title_id': TITLE}, headers=self.noors).get_json()['cheats']
        self.assertEqual((cheat['source'], cheat['sha256']), ('aerofoil', hashlib.sha256(TEXT.encode()).hexdigest()))

    def test_replace_conflict_and_delete(self):
        path = BASE + f'/v1/cheats/cs_{TITLE}-{BUILD}'
        self.client.post(BASE + '/v1/cheats', json=self.body, headers=self.leas)
        self.assertEqual(self.client.post(BASE + '/v1/cheats', json=self.body, headers=self.leas).status_code, 409)
        new = TEXT + '[Slow fall]\n04000000 00ABC020 00000001\n'
        self.assertEqual(self.client.put(path, json={**self.body, 'text': new}, headers=self.leas).status_code, 204)
        self.assertEqual(self.client.get(path, headers=self.leas).data, new.encode())
        self.assertEqual(self.client.put(path, json={**self.body, 'source': 'Noor'}, headers=self.leas).status_code, 400)
        self.assertEqual(self.client.delete(path, headers=self.leas).status_code, 204)
        self.assertFalse(os.path.exists(os.path.join(aerofoil_cheats.CHEATS_DIR, TITLE, f'{BUILD}.txt')))
        self.assertEqual(self.client.delete(path, headers=self.leas).status_code, 404)

    def test_a_set_with_no_build_is_refused_and_a_reader_cannot_write(self):
        self.assertEqual(self.client.post(BASE + '/v1/cheats', json={**self.body, 'build_id': None}, headers=self.leas).status_code, 400)
        self.assertEqual(self.client.post(BASE + '/v1/cheats', json=self.body, headers=self.noors).status_code, 403)


class RequestTests(Harness):
    def make(self, headers=None):
        return self.client.post(BASE + '/v1/requests', json={'schema': 1, 'platform': 'switch', 'title_id': TITLE}, headers=headers or self.noors)

    def test_a_request_is_aerofoils_request(self):
        made = self.make()
        self.assertEqual(made.status_code, 201, made.data)
        request = made.get_json()
        row = TitleRequests.query.one()
        self.assertEqual((row.title_id, row.status, row.user_id), (TITLE, 'open', self.noor.id))
        listed = self.client.get(BASE + '/v1/requests', headers=self.noors).get_json()['requests']
        self.assertEqual([r['id'] for r in listed], [request['id']])
        again = self.make()
        self.assertEqual((again.status_code, again.get_json()['id']), (200, request['id']))

    def test_two_users_share_one_aerofoil_request(self):
        mine, hers = self.make().get_json(), self.make(self.leas).get_json()
        self.assertNotEqual(mine['id'], hers['id'])
        self.assertEqual((TitleRequests.query.count(), TitleRequestUsers.query.count()), (1, 2))
        self.assertEqual(len(self.client.get(BASE + '/v1/requests', headers=self.noors).get_json()['requests']), 1)

    def test_a_request_made_on_aerofoils_page_is_visible(self):
        from app.title_requests import create_title_request

        create_title_request(self.noor.id, TITLE, 'Frost Valley')
        [request] = self.client.get(BASE + '/v1/requests', headers=self.noors).get_json()['requests']
        self.assertEqual((request['title_id'], request['status'], request['name']), (TITLE, 'open', 'Frost Valley'))
        self.assertRegex(request['id'], r'^r_\d+-\d+$')

    def test_answering_maps_to_aerofoils_statuses(self):
        request = self.make().get_json()
        self.assertEqual(self.lantern.answer_request(request['id'], 'declined', 'Not available.').status, 'declined')
        self.assertEqual(TitleRequests.query.one().status, 'denied')
        shown = self.client.get(BASE + '/v1/requests', headers=self.noors).get_json()['requests'][0]
        self.assertEqual((shown['status'], shown['reason']), ('declined', 'Not available.'))

    def test_aerofoil_closing_a_request_makes_it_done(self):
        request = self.make().get_json()
        row = TitleRequests.query.one()
        row.status = 'closed'
        db.session.commit()
        shown = self.client.get(BASE + '/v1/requests', headers=self.noors).get_json()['requests'][0]
        self.assertEqual((shown['id'], shown['status']), (request['id'], 'done'))

    def test_a_title_already_in_the_library_cannot_be_requested(self):
        db.session.add(Titles(title_id=TITLE))
        db.session.commit()
        self.assertEqual(self.make().status_code, 409)

    def test_withdrawing_removes_only_your_part(self):
        mine, hers = self.make().get_json(), self.make(self.leas).get_json()
        self.assertEqual(self.client.delete(f"{BASE}/v1/requests/{mine['id']}", headers=self.leas).status_code, 404)
        self.assertEqual(self.client.delete(f"{BASE}/v1/requests/{mine['id']}", headers=self.noors).status_code, 204)
        self.assertEqual((TitleRequests.query.count(), TitleRequestUsers.query.count()), (1, 1))
        self.assertEqual(self.client.delete(f"{BASE}/v1/requests/{hers['id']}", headers=self.leas).status_code, 204)
        self.assertEqual(TitleRequests.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
