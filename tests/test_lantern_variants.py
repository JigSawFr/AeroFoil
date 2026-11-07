import hashlib
import os
import random
import unittest
from unittest.mock import patch

try:
    from test_compressed_stream import _Container, _Entry, _nsz_components, _ncz
    from test_lantern_library import BASE, TITLE, LibraryTests
except ImportError:  # run as tests.test_lantern_variants
    from tests.test_compressed_stream import _Container, _Entry, _nsz_components, _ncz
    from tests.test_lantern_library import BASE, TITLE, LibraryTests

from app import compressed_stream
from app.db import Files, db
from app.lantern_native import variants
from app.lantern_native.fingerprints import Fingerprinter
from app.settings import _normalize_shop_settings


def container():
    return _Container([_Entry('ticket.tik', b'ticket'), _Entry('content.ncz', _ncz(b'virtual-nca-data' * 50))])


class DeterminismTests(unittest.TestCase):
    """The hash of a virtual file is only true if the same bytes come out every time, whole or by range."""

    def patched(self):
        return patch('app.compressed_stream._open', side_effect=lambda path: container()), patch(
            'app.compressed_stream._load_nsz', side_effect=_nsz_components
        )

    def test_the_virtual_nsp_is_the_same_every_time(self):
        a, b = self.patched()
        with a, b:
            first = variants.virtual_hash('Example.nsz')
            second = variants.virtual_hash('Example.nsz')
            whole = b''.join(compressed_stream.virtual_stream('Example.nsz')[0])
        self.assertEqual(first, second)
        self.assertEqual(first, (hashlib.sha256(whole).hexdigest(), len(whole)))

    def test_any_range_of_the_virtual_nsp_is_a_slice_of_the_whole(self):
        a, b = self.patched()
        with a, b:
            whole = b''.join(compressed_stream.virtual_stream('Example.nsz')[0])
            pick = random.Random(7)
            for first in [0, 1, 0xEFFF, 0xF000, 0xF005, len(whole) - 1] + [pick.randrange(len(whole)) for _ in range(20)]:
                length = pick.randint(1, 5000)
                got = b''.join(variants.reader('Example.nsz')(first, min(length, len(whole) - first)))
                self.assertEqual(got, whole[first:first + length], (first, length))

    def test_a_stream_that_is_not_its_announced_size_is_refused(self):
        with patch('app.compressed_stream.virtual_stream', return_value=(iter([b'abc']), 4)):
            with self.assertRaises(ValueError):
                variants.virtual_hash('Example.nsz')

    def test_the_setting_is_on_by_default(self):
        self.assertTrue(_normalize_shop_settings({})['lantern_variants'])
        self.assertFalse(_normalize_shop_settings({'lantern_variants': 'false'})['lantern_variants'])


class VariantLibraryTests(LibraryTests):
    PLAIN = b'PFS0' + bytes(range(256)) * 20

    def fake_streams(self):
        plain = self.PLAIN

        def stream(path):
            return iter([plain[:100], plain[100:]]), len(plain)

        def ranged(path, first, last):
            return iter([plain[first:last + 1]])

        return patch('app.compressed_stream.virtual_stream', side_effect=stream), patch(
            'app.compressed_stream.virtual_range', side_effect=ranged
        )

    def build(self):
        path = self.add('game.nsz', b'compressed bytes')
        builder = variants.VariantBuilder(self.library.virtual)
        a, b = self.fake_streams()
        with a, b:
            self.assertEqual(builder.run(), 1)
        return path

    def test_a_compressed_package_gets_a_plain_variant(self):
        self.build()
        self.refresh()
        apps = self.get('/v1/apps').get_json()['items']
        by_format = {a['format']: a for a in apps}
        self.assertEqual(set(by_format), {'nsz', 'nsp'})
        nsz, nsp = by_format['nsz'], by_format['nsp']
        self.assertEqual(nsz['requires'], ['fs-compression'])
        self.assertNotIn('requires', nsp)
        self.assertEqual((nsp['app_id'], nsp['version_code'], nsp['title_id']), (nsz['app_id'], nsz['version_code'], nsz['title_id']))
        self.assertEqual((nsp['size'], nsp['sha256']), (len(self.PLAIN), hashlib.sha256(self.PLAIN).hexdigest()))
        self.assertNotEqual(nsp['id'], nsz['id'])

    def test_the_variant_is_served_whole_and_by_range(self):
        self.build()
        self.refresh()
        nsp = next(a for a in self.get('/v1/apps').get_json()['items'] if a['format'] == 'nsp')
        a, b = self.fake_streams()
        with a, b:
            whole = self.client.get(nsp['file'])
            part = self.client.get(nsp['file'], headers={'Range': 'bytes=10-29'})
        self.assertEqual((whole.status_code, whole.data), (200, self.PLAIN))
        self.assertEqual((part.status_code, part.data), (206, self.PLAIN[10:30]))
        import base64

        digest = whole.headers['Repr-Digest'].split(':')[1]
        self.assertEqual(base64.b64decode(digest).hex(), hashlib.sha256(self.PLAIN).hexdigest())

    def test_a_changed_file_loses_its_variant_until_it_is_hashed_again(self):
        path = self.build()
        self.refresh()
        with open(path, 'wb') as handle:
            handle.write(b'compressed bytes, changed')
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
        Fingerprinter(self.app).run_once()
        self.refresh()
        self.assertEqual({a['format'] for a in self.get('/v1/apps').get_json()['items']}, {'nsz'})
        a, b = self.fake_streams()
        with a, b:
            self.assertEqual(variants.VariantBuilder(self.library.virtual).run(), 1)
        self.refresh()
        self.assertEqual({a['format'] for a in self.get('/v1/apps').get_json()['items']}, {'nsz', 'nsp'})

    def test_the_worker_makes_the_variants_after_listing(self):
        self.add('game.nsz', b'compressed bytes', hashed=False)
        a, b = self.fake_streams()
        worker = Fingerprinter(self.app, on_pass=lambda done: self.library.refresh(), variants=variants.VariantBuilder(self.library.virtual))
        with a, b:
            worker.run_once()
        self.assertEqual({x['format'] for x in self.get('/v1/apps').get_json()['items']}, {'nsz', 'nsp'})
        self.assertEqual(worker.status()['variants'], {'done': 1, 'pending': 0})

    def test_small_files_have_a_chunk_index_that_is_served(self):
        data = random.Random(3).randbytes(300_000)
        self.add('small.nsp', data)
        self.refresh()
        [app] = self.get('/v1/apps').get_json()['items']
        self.assertIn('chunks', app)
        self.assertEqual(self.get('/v1').get_json()['features']['deltas'], 1)
        index = self.client.get(app['chunks']['url'])
        self.assertEqual(index.status_code, 200)
        self.assertEqual(hashlib.sha256(index.data).hexdigest(), app['chunks']['sha256'])
        self.assertTrue(os.path.isfile(os.path.join(self.config, 'lantern-chunks', app['sha256'] + '.idx')))

    def test_big_files_have_no_chunk_index(self):
        self.add('big.nsp', b'x' * 100)  # under the minimum
        self.refresh()
        [app] = self.get('/v1/apps').get_json()['items']
        self.assertNotIn('chunks', app)


# The inherited tests run again with the variants on: they must still pass
if __name__ == '__main__':
    unittest.main()
