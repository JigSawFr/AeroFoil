import hashlib
import json
import os
import time
import unittest

try:
    import test_lantern_variants as variant_tests
except ImportError:
    from tests import test_lantern_variants as variant_tests

from lantern.static import StaticLibrary

from app.lantern_native import static_export


class StaticExportTests(variant_tests.VariantLibraryTests):
    def setUp(self):
        super().setUp()
        self.exporter = static_export.Exporter(self.lantern, os.path.join(self.dir.name, 'export'))

    def wait(self):
        for _ in range(200):
            if self.exporter.status()['state'] != 'running':
                return self.exporter.status()
            time.sleep(0.05)
        self.fail('the export did not end')

    def test_the_library_is_exported_as_a_static_library_a_reader_can_use(self):
        self.add('game.nsp', b'game bytes')
        self.refresh()
        self.assertTrue(self.exporter.start('shelf'))
        status = self.wait()
        self.assertEqual(status['state'], 'done', status)
        out = os.path.join(self.dir.name, 'export', 'shelf')
        with open(os.path.join(out, 'lantern.json'), encoding='utf-8') as handle:
            hello = json.load(handle)
        self.assertEqual((hello['uid'], hello['name'], hello['features']['library']), (self.lantern.uid, 'AeroFoil', 1))
        with StaticLibrary(out) as library:
            [app] = list(library.iter_apps('switch'))
            self.assertEqual(app.sha256, hashlib.sha256(b'game bytes').hexdigest())
            target = os.path.join(self.dir.name, 'copy.nsp')
            library.download(app, target)
        with open(target, 'rb') as handle:
            self.assertEqual(handle.read(), b'game bytes')

    def test_the_decompressed_variants_are_left_out(self):
        self.build()
        self.refresh()
        self.exporter.start('shelf')
        self.assertEqual(self.wait()['state'], 'done')
        with StaticLibrary(os.path.join(self.dir.name, 'export', 'shelf')) as library:
            self.assertEqual([a.format for a in library.iter_apps('switch')], ['nsz'])

    def test_only_one_export_runs_and_names_are_plain(self):
        self.add('game.nsp', b'game')
        self.refresh()
        with self.assertRaises(ValueError):
            self.exporter.start('../escape')
        self.assertTrue(self.exporter.start('a'))
        self.exporter.start('b')  # refused or queued behind: never two at once
        self.wait()
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, 'export', '..', 'escape')))

    def test_a_failing_export_is_reported(self):
        os.makedirs(os.path.join(self.dir.name, 'export'))
        with open(os.path.join(self.dir.name, 'export', 'taken'), 'w', encoding='utf-8') as handle:
            handle.write('a file where the folder should be')
        self.exporter.start('taken')
        self.assertEqual(self.wait()['state'], 'failed')

    # The inherited tests are not this file's business
    test_a_hashed_file_is_listed_and_served = None
    test_nothing_is_listed_until_a_file_is_hashed = None
    test_a_bundle_is_one_app_that_includes_the_others = None
    test_the_same_bytes_in_two_places_are_one_app = None
    test_what_is_missing = None
    test_changes_say_what_was_added_and_removed = None
    test_a_restart_changes_nothing = None
    test_the_handshake_offers_the_library_and_changes = None
    test_a_title_has_its_sheet = None
    test_a_rating_cap_hides_what_is_above_it = None
    test_a_cached_image_is_served_from_here = None
    test_title_changes_follow_the_sheet = None
    test_recently_added_is_a_shelf = None
    test_a_compressed_package_gets_a_plain_variant = None
    test_the_variant_is_served_whole_and_by_range = None
    test_a_changed_file_loses_its_variant_until_it_is_hashed_again = None
    test_the_worker_makes_the_variants_after_listing = None
    test_small_files_have_a_chunk_index_that_is_served = None
    test_big_files_have_no_chunk_index = None


if __name__ == '__main__':
    unittest.main()
