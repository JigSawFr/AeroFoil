import os
import re
import tempfile
import unittest
import uuid

from flask import Blueprint, Flask, g
from flask_login import LoginManager

from app import lantern_native
from app.db import User, db
from app.lantern_native import accounts as mirror_accounts

BASE = lantern_native.BASE


def make_user(name, **flags):
    values = dict(admin_access=False, shop_access=True, backup_access=False, cheat_access=True, frozen=False)
    values.update(flags)
    user = User(user=name, password='x', **values)
    db.session.add(user)
    db.session.commit()
    return user


class ScopeTests(unittest.TestCase):
    def user(self, **flags):
        values = dict(admin_access=False, shop_access=True, backup_access=False, cheat_access=True, frozen=False)
        values.update(flags)
        return User(user='u', **values)

    def test_scopes_follow_the_access_flags(self):
        self.assertEqual(
            mirror_accounts.scopes_for(self.user()), ['library', 'requests', 'sync.read', 'sync.write', 'cheats']
        )
        self.assertEqual(mirror_accounts.scopes_for(self.user(cheat_access=False))[-1], 'sync.write')
        self.assertIn('saves', mirror_accounts.scopes_for(self.user(backup_access=True)))
        admin = mirror_accounts.scopes_for(self.user(admin_access=True))
        self.assertTrue({'admin', 'cheats.write'} <= set(admin))
        self.assertEqual(mirror_accounts.scopes_for(self.user(shop_access=False)), [])

    def test_a_frozen_user_has_nothing_to_do(self):
        self.assertEqual(
            mirror_accounts.scopes_for(self.user(frozen=True, admin_access=True, backup_access=True)), []
        )

    def test_names_fit_lantern_and_never_collide(self):
        self.assertEqual(mirror_accounts.lantern_name('lea.b-2'), 'lea.b-2')
        odd, other = mirror_accounts.lantern_name('léa b'), mirror_accounts.lantern_name('lèa b')
        self.assertNotEqual(odd, other)
        for name in (odd, other, mirror_accounts.lantern_name('x' * 200), mirror_accounts.lantern_name('')):
            self.assertRegex(name, r'^[A-Za-z0-9._-]{1,64}$')


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.dir.name, 'app.db'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test',
        )
        db.init_app(self.app)
        login = LoginManager(self.app)
        login.login_view = 'auth.login'
        login.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        auth = Blueprint('auth', __name__)
        auth.add_url_rule('/login', 'login', lambda: 'login page')
        self.app.register_blueprint(auth)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)
        self.lantern = lantern_native.init_app(
            self.app, {'shop': {'lantern_enabled': True, 'public': False, 'lantern_scheme': 'https'}},
            os.path.join(self.dir.name, 'config'), str(uuid.uuid4()), start_worker=False,
        )
        self.addCleanup(setattr, lantern_native, '_instance', None)
        self.lea = make_user('lea', backup_access=True)
        self.client = self.app.test_client()

    def login(self, user):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def start(self, scopes=('library', 'saves', 'admin')):
        answer = self.client.post(
            BASE + '/v1/pairing', json={'schema': 1, 'device': 'Lea console', 'scopes': list(scopes)},
            base_url='https://shelf.example.org',
        )
        self.assertEqual(answer.status_code, 200, answer.data)
        return answer.get_json()

    def csrf(self, page):
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def test_the_pairing_points_at_aerofoils_page(self):
        doc = self.start()
        self.assertEqual(doc['verify'], 'https://shelf.example.org' + lantern_native.PAIR_PATH)

    def test_a_signed_out_user_is_sent_to_log_in(self):
        answer = self.client.get(lantern_native.PAIR_PATH + '?code=ABCD-EFGH')
        self.assertEqual(answer.status_code, 302)
        self.assertIn('/login?next=', answer.headers['Location'])

    def test_a_user_approves_a_device_with_what_their_account_holds(self):
        doc = self.start()
        self.login(self.lea)
        page = self.client.get(lantern_native.PAIR_PATH, query_string={'code': doc['code']})
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn('Lea console', text)
        done = self.client.post(
            lantern_native.PAIR_PATH,
            data={'code': doc['code'], 'csrf': self.csrf(text), 'decision': 'approve'},
        )
        self.assertIn('Approved', done.get_data(as_text=True))
        token = self.client.get(doc['poll'], base_url='https://shelf.example.org').get_json()
        self.assertEqual(token['scopes'], ['library', 'saves'])  # not admin: Lea is no administrator
        account = self.lantern.accounts.user('lea')
        self.assertEqual(account.state, 'active')

    def test_the_form_without_its_token_approves_nothing(self):
        doc = self.start()
        self.login(self.lea)
        self.client.get(lantern_native.PAIR_PATH, query_string={'code': doc['code']})
        refused = self.client.post(
            lantern_native.PAIR_PATH, data={'code': doc['code'], 'csrf': 'wrong', 'decision': 'approve'}
        )
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.lantern.accounts.devices(), [])

    def test_a_device_can_be_refused(self):
        doc = self.start()
        self.login(self.lea)
        text = self.client.get(lantern_native.PAIR_PATH, query_string={'code': doc['code']}).get_data(as_text=True)
        self.client.post(
            lantern_native.PAIR_PATH, data={'code': doc['code'], 'csrf': self.csrf(text), 'decision': 'refuse'}
        )
        self.assertEqual(self.lantern.accounts.devices(), [])

    def test_a_user_without_shop_access_cannot_approve(self):
        self.login(make_user('guest', shop_access=False))
        self.assertEqual(self.client.get(lantern_native.PAIR_PATH).status_code, 403)

    def test_guessing_codes_is_slowed_down(self):
        self.login(self.lea)
        answers = [
            self.client.get(lantern_native.PAIR_PATH, query_string={'code': f'GUESS{n:03d}'}).status_code
            for n in range(12)
        ]
        self.assertEqual(answers[-1], 429)

    def test_devices_are_listed_and_revoked_by_their_owner_only(self):
        doc = self.start()
        self.login(self.lea)
        text = self.client.get(lantern_native.PAIR_PATH, query_string={'code': doc['code']}).get_data(as_text=True)
        self.client.post(
            lantern_native.PAIR_PATH, data={'code': doc['code'], 'csrf': self.csrf(text), 'decision': 'approve'}
        )
        [device] = self.client.get('/api/lantern-native/devices').get_json()['devices']
        self.assertEqual(device['device'], 'Lea console')
        other = self.app.test_client()
        g.pop('_login_user', None)  # the test keeps one app context open, where flask-login caches the user
        noor = make_user('noor')
        with other.session_transaction() as session:
            session['_user_id'] = str(noor.id)
            session['_fresh'] = True
        self.assertEqual(other.get('/api/lantern-native/devices').get_json()['devices'], [])
        self.assertEqual(other.delete(f"/api/lantern-native/devices/{device['id']}").status_code, 404)
        g.pop('_login_user', None)
        self.assertEqual(self.client.delete(f"/api/lantern-native/devices/{device['id']}").status_code, 200)
        self.assertEqual(self.lantern.accounts.devices(), [])

    def test_a_frozen_or_deleted_user_reaches_their_devices(self):
        doc = self.start()
        self.login(self.lea)
        text = self.client.get(lantern_native.PAIR_PATH, query_string={'code': doc['code']}).get_data(as_text=True)
        self.client.post(
            lantern_native.PAIR_PATH, data={'code': doc['code'], 'csrf': self.csrf(text), 'decision': 'approve'}
        )
        token = self.client.get(doc['poll'], base_url='https://shelf.example.org').get_json()['token']
        headers = {'Authorization': f'Bearer {token}'}
        url = BASE + '/v1/apps'
        self.assertEqual(self.client.get(url, headers=headers, base_url='https://shelf.example.org').status_code, 200)
        mirror = mirror_accounts.Mirror(self.lantern.accounts)
        self.lea.frozen = True
        db.session.commit()
        mirror.sync(force=True)
        self.assertEqual(self.lantern.accounts.user('lea').state, 'frozen')
        self.assertEqual(self.client.get(url, headers=headers, base_url='https://shelf.example.org').status_code, 403)
        db.session.delete(self.lea)
        db.session.commit()
        mirror.sync(force=True)
        self.assertIsNone(self.lantern.accounts.user('lea'))
        self.assertEqual(self.lantern.accounts.devices(), [])
        self.assertEqual(self.client.get(url, headers=headers, base_url='https://shelf.example.org').status_code, 401)


if __name__ == '__main__':
    unittest.main()
