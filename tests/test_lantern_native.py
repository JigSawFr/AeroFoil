import json
import os
import stat
import sys
import tempfile
import unittest
import uuid

from flask import Flask

from app import lantern_native
from app.settings import _normalize_shop_settings, verify_settings

UID = str(uuid.uuid4())


def on(**shop):
    return {'shop': {'lantern_enabled': True, 'public': True, **shop}}


class LanternNativeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(setattr, lantern_native, '_instance', None)

    def serve(self, settings):
        app = Flask(__name__)
        found = lantern_native.init_app(app, settings, self.dir.name, UID)
        return app, found

    def test_setting_is_off_by_default_and_coerces(self):
        self.assertFalse(_normalize_shop_settings({})['lantern_enabled'])
        self.assertFalse(_normalize_shop_settings({'lantern_enabled': 'false'})['lantern_enabled'])
        self.assertTrue(_normalize_shop_settings({'lantern_enabled': 'true'})['lantern_enabled'])
        self.assertTrue(verify_settings('shop', {'lantern_enabled': True, 'discovery_http_port': 0})[0])

    def test_off_serves_nothing_and_writes_nothing(self):
        app, found = self.serve({'shop': {}})
        self.assertIsNone(found)
        self.assertEqual(app.test_client().get('/.well-known/lantern').status_code, 404)
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_on_serves_the_protocol_with_the_servers_uid_and_name(self):
        app, found = self.serve(on(discovery_name='Living room'))
        self.assertIsNotNone(found)
        response = app.test_client().get('/.well-known/lantern')
        self.assertEqual(response.status_code, 200)
        doc = response.get_json()
        self.assertEqual(doc['uid'], UID)
        self.assertEqual(doc['name'], 'Living room')

    def test_accounts_and_secrets_are_kept_in_the_config_directory(self):
        self.serve(on())
        kept = os.listdir(self.dir.name)
        self.assertIn(lantern_native.ACCOUNTS_FILE, kept)
        self.assertIn(lantern_native.SECRETS_FILE, kept)
        if sys.platform != 'win32':
            for name in (lantern_native.ACCOUNTS_FILE, lantern_native.SECRETS_FILE):
                mode = stat.S_IMODE(os.stat(os.path.join(self.dir.name, name)).st_mode)
                self.assertEqual(mode, 0o600, name)

    def test_secrets_survive_a_restart(self):
        self.serve(on())
        with open(os.path.join(self.dir.name, lantern_native.SECRETS_FILE), encoding='utf-8') as handle:
            first = json.load(handle)
        lantern_native._instance = None
        self.serve(on())
        with open(os.path.join(self.dir.name, lantern_native.SECRETS_FILE), encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), first)
        self.assertEqual(sorted(first['secrets']), ['cursor', 'grant'])

    def test_a_private_shop_shows_nothing_to_an_anonymous_client(self):
        app, _ = self.serve(on(public=False))
        response = app.test_client().get(lantern_native.BASE + '/v1/apps')
        self.assertEqual(response.status_code, 401)

    def test_failing_to_start_does_not_raise(self):
        blocker = os.path.join(self.dir.name, 'file')
        with open(blocker, 'w', encoding='utf-8') as handle:
            handle.write('x')
        app = Flask(__name__)
        self.assertIsNone(lantern_native.init_app(app, on(), os.path.join(blocker, 'config'), UID))


if __name__ == '__main__':
    unittest.main()
