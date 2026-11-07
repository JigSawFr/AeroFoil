import os
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from flask import Flask

from app import cheats as aerofoil_cheats
from app import lantern_native
from app.db import db

BASE = lantern_native.BASE
TITLE = '0100ABCD00010000'
BUILD = '0123456789ABCDEF'


class EventTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.dir.name, 'app.db'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db.create_all()
        self.addCleanup(db.session.remove)
        original = aerofoil_cheats.CHEATS_DIR
        aerofoil_cheats.CHEATS_DIR = os.path.join(self.dir.name, 'cheats')
        self.addCleanup(setattr, aerofoil_cheats, 'CHEATS_DIR', original)
        self.addCleanup(setattr, aerofoil_cheats, 'listeners', list(aerofoil_cheats.listeners))
        self.lantern = lantern_native.init_app(
            self.app, {'shop': {'lantern_enabled': True, 'public': True, 'discovery_enabled': True}},
            os.path.join(self.dir.name, 'config'), str(uuid.uuid4()), start_worker=False,
        )
        self.addCleanup(setattr, lantern_native, '_instance', None)
        self.seen = []
        self.lantern.library.on_changes = self.seen.extend

    def test_a_cheat_changed_on_aerofoils_page_is_a_library_event(self):
        aerofoil_cheats.save_cheat(TITLE, BUILD, b'[Moon jump]\n04000000 00ABC010 0000FFFF\n')
        aerofoil_cheats.set_cheat_note(TITLE, BUILD, 'jump')
        aerofoil_cheats.delete_cheat(TITLE, BUILD)
        self.assertEqual([(c.family, c.platform, c.title_id) for c in self.seen], [('cheats', 'switch', TITLE)] * 3)

    def test_a_refresh_tells_what_changed(self):
        from lantern._generated.models import App

        app = App(id='a_1', platform='switch', title_id=TITLE, app_id=TITLE, type='base', version_code=0, size=1,
                  format='nsp', sha256='0' * 64, file='/f')
        self.lantern.library.refresh({'a_1': (app, '/x')})
        self.assertIn(('apps', TITLE), [(c.family, c.title_id) for c in self.seen])
        self.seen.clear()
        self.lantern.library.refresh({'a_1': (app, '/x')})
        self.assertEqual(self.seen, [])
        self.lantern.library.refresh({})
        self.assertEqual({c.family for c in self.seen}, {'apps', 'titles'})

    def test_the_server_offers_events_and_a_stream_opens(self):
        features = self.app.test_client().get(BASE + '/v1').get_json()['features']
        self.assertEqual(features['events'], 1)

    def test_it_is_announced_on_the_lan_when_discovery_is_on(self):
        with patch('lantern.server.discovery.advertise_dnssd', return_value=threading.Event()) as announce, \
                patch.object(lantern_native, '_lan_address', return_value='192.168.1.20'), \
                patch.object(threading.Event, 'close', create=True):
            self.assertTrue(lantern_native.advertise(8465, {'shop': {'discovery_enabled': True, 'discovery_http_port': 9000}}))
            announce.assert_called_once()
            self.assertEqual(announce.call_args.args[1:], (9000, '192.168.1.20'))
            self.assertFalse(lantern_native.advertise(8465, {'shop': {'discovery_enabled': True}}))  # already
            lantern_native.stop()
            self.assertIsNone(lantern_native._dnssd)

    def test_it_is_not_announced_when_discovery_is_off_or_lantern_is_off(self):
        self.assertFalse(lantern_native.advertise(8465, {'shop': {'discovery_enabled': False}}))
        lantern_native._instance = None
        self.assertFalse(lantern_native.advertise(8465, {'shop': {'discovery_enabled': True}}))

    def test_a_failing_announcement_does_not_raise(self):
        with patch('lantern.server.discovery.advertise_dnssd', side_effect=OSError('no network')), \
                patch.object(lantern_native, '_lan_address', return_value='192.168.1.20'):
            self.assertFalse(lantern_native.advertise(8465, {'shop': {'discovery_enabled': True}}))


if __name__ == '__main__':
    unittest.main()
