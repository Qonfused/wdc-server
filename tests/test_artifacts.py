"""Exercise release integrity at the boundary between locks and stored artifacts."""

import hashlib
import functools
import http.server
import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent


def module(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


inputs = module('inputs')
image = module('image')
prepare = module('prepare-packages')


class InputReleases(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        for name in ('locks', '.work/base', '.work/packages', '.work/collection-archives'):
            (self.project / name).mkdir(parents=True)
        base = b'fixture root filesystem'
        archive = b'fixture package'
        (self.project / '.work/base/rootfs.tar.xz').write_bytes(base)
        (self.project / '.work/packages/example-1_1.x86_64.xbps').write_bytes(archive)
        (self.project / 'locks/base.json').write_text(json.dumps({'sha256': hashlib.sha256(base).hexdigest()}))
        (self.project / 'locks/collections.json').write_text('{"archives": []}')
        (self.project / 'locks/builds.yml').write_text('schema: 1\n')
        self.lock = self.project / 'locks/packages.json'
        self.lock.write_text(json.dumps({'packages': {'example': {
            'version': 'example-1_1', 'architecture': 'x86_64',
            'sha256': hashlib.sha256(archive).hexdigest()}}}))
        self.release = self.root / 'release'

    def test_bad_input_does_not_publish_a_partial_release(self):
        (self.project / '.work/packages/example-1_1.x86_64.xbps').write_bytes(b'damaged')
        with self.assertRaisesRegex(ValueError, 'Missing or changed'):
            inputs.create(self.project, self.lock, None, self.release)
        self.assertFalse(self.release.exists())

    def test_release_has_no_dependency_on_the_source_cache(self):
        inputs.create(self.project, self.lock, None, self.release)
        shutil.rmtree(self.project)
        inputs.verify(self.release)
        with self.assertRaisesRegex(ValueError, 'cannot be overwritten'):
            inputs.create(self.project, self.lock, None, self.release)

    def test_transport_manifest_cannot_override_embedded_package_pins(self):
        inputs.create(self.project, self.lock, None, self.release)
        name = 'packages/example-1_1.x86_64.xbps'
        (self.release / name).write_bytes(b'other package')
        manifest = json.loads((self.release / 'manifest.json').read_text())
        manifest['files'][name] = inputs.checksum(self.release / name)
        (self.release / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'disagrees with its lock'):
            inputs.verify(self.release)

    def test_manifest_cannot_read_outside_its_release(self):
        self.release.mkdir()
        (self.root / 'outside').write_text('outside')
        (self.release / 'manifest.json').write_text(json.dumps({
            'schema': 1, 'files': {'../outside': 'unused'}}))
        with self.assertRaisesRegex(ValueError, 'escapes'):
            inputs.verify(self.release)

    def test_unlisted_files_cannot_be_published_as_part_of_a_verified_bundle(self):
        inputs.create(self.project, self.lock, None, self.release)
        (self.release / '.env').write_text('PRIVATE_PLACEHOLDER=not-a-secret\n')
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            inputs.verify(self.release)

    def test_bundle_must_match_the_checkout_even_when_its_own_locks_are_consistent(self):
        inputs.create(self.project, self.lock, None, self.release)
        (self.project / 'locks/builds.yml').write_text('schema: 2\n')
        inputs.verify(self.release)
        with self.assertRaisesRegex(ValueError, 'disagrees with the checkout'):
            inputs.verify(self.release, self.project)

    def test_release_parts_restore_without_the_original_bundle(self):
        inputs.create(self.project, self.lock, None, self.release)
        parts = self.root / 'parts'
        inputs.pack(self.release, parts, parts.as_uri(), self.project)
        shutil.rmtree(self.release)
        restored = self.root / 'restored'
        inputs.restore(parts / 'input-release.json', restored, self.project)
        inputs.verify(restored, self.project)

    def test_damaged_release_part_is_rejected(self):
        inputs.create(self.project, self.lock, None, self.release)
        parts = self.root / 'parts'
        inputs.pack(self.release, parts, parts.as_uri(), self.project)
        (parts / 'inputs-001.tar').write_bytes(b'damaged')
        with self.assertRaisesRegex(ValueError, 'differs from the lock'):
            inputs.restore(parts / 'input-release.json', self.root / 'restored', self.project)

    def test_release_lock_rejects_a_changed_package_lock(self):
        inputs.create(self.project, self.lock, None, self.release)
        parts = self.root / 'parts'
        inputs.pack(self.release, parts, parts.as_uri(), self.project)
        self.lock.write_text(self.lock.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'does not match the checkout locks'):
            inputs.check_release_lock(parts / 'input-release.json', self.project)

    def test_release_lock_rejects_changed_nonpackage_inputs(self):
        inputs.create(self.project, self.lock, None, self.release)
        parts = self.root / 'parts'
        inputs.pack(self.release, parts, parts.as_uri(), self.project)
        tag = inputs.release_tag(self.project)
        (self.project / 'locks/base.json').write_text('{}')
        self.assertNotEqual(tag, inputs.release_tag(self.project))
        with self.assertRaisesRegex(ValueError, 'does not match the checkout locks'):
            inputs.check_release_lock(parts / 'input-release.json', self.project)

    def test_empty_cache_uses_explicit_repository_without_changing_pins(self):
        repository = self.project / '.work/packages'
        with tempfile.TemporaryDirectory() as temporary:
            paths = prepare.select_archives(json.loads(self.lock.read_text()), Path(temporary) / 'unused',
                                            [], Path(temporary), repository.as_uri())
            self.assertEqual(set(paths), {'example-1_1.x86_64.xbps'})
            (repository / 'example-1_1.x86_64.xbps').write_bytes(b'wrong download')
            with self.assertRaisesRegex(SystemExit, 'differs from the lock'):
                prepare.select_archives(json.loads(self.lock.read_text()), Path(temporary) / 'unused',
                                        [], Path(temporary), repository.as_uri())


class PackageRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / 'upstream'
        self.repository.mkdir()
        self.filename = 'example-1_1.x86_64.xbps'
        self.contents = b'original locked package'
        (self.repository / self.filename).write_bytes(self.contents)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.root))
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.lock = {'packages': {'example': {
            'version': 'example-1_1', 'architecture': 'x86_64',
            'sha256': hashlib.sha256(self.contents).hexdigest(),
            'url': self.url + '/upstream/' + self.filename}}}
        self.destination = self.root / 'destination'
        self.destination.mkdir()
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()

    def select(self, mirror=None):
        return prepare.select_archives(self.lock, self.destination, [], self.downloads, mirror)

    def test_deleted_cache_can_be_recovered_from_the_locked_upstream(self):
        original = json.dumps(self.lock)
        result = self.select()
        self.assertEqual(result[self.filename].read_bytes(), self.contents)
        self.assertEqual(json.dumps(self.lock), original)

    def test_damaged_destination_is_replaced_only_after_selection_succeeds(self):
        damaged = self.destination / self.filename
        damaged.write_bytes(b'damaged cache')
        result = self.select()
        self.assertEqual(result[self.filename].read_bytes(), self.contents)
        self.assertEqual(damaged.read_bytes(), b'damaged cache')

    def test_changed_upstream_leaves_existing_files_untouched(self):
        (self.repository / self.filename).write_bytes(b'unexpected upstream change')
        existing = self.destination / self.filename
        existing.write_bytes(b'existing damaged file')
        with self.assertRaisesRegex(SystemExit, 'differs from the lock'):
            self.select()
        self.assertEqual(existing.read_bytes(), b'existing damaged file')
        self.assertFalse((self.downloads / self.filename).exists())

    def test_missing_or_corrupt_mirror_falls_back_to_the_same_upstream_pin(self):
        mirror = self.root / 'mirror'
        mirror.mkdir()
        for contents in (None, b'corrupt mirror'):
            with self.subTest(contents=contents):
                if contents:
                    (mirror / self.filename).write_bytes(contents)
                result = self.select(self.url + '/mirror')
                self.assertEqual(result[self.filename].read_bytes(), self.contents)

    def test_retained_mirror_recovers_an_archive_deleted_upstream(self):
        mirror = self.root / 'mirror'
        mirror.mkdir()
        (mirror / self.filename).write_bytes(self.contents)
        (self.repository / self.filename).unlink()
        result = self.select(self.url + '/mirror')
        self.assertEqual(result[self.filename].read_bytes(), self.contents)


class ImageReleases(unittest.TestCase):
    def test_editing_configuration_during_assembly_prevents_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / 'project'
            original = image.source_inputs(PROJECT, PROJECT / 'locks/packages.json', '')
            for name in original['configuration_sha256']:
                target = project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PROJECT / name, target)
            lock = project / 'locks/packages.json'
            shutil.copyfile(PROJECT / 'locks/packages.json', lock)
            (directory / 'source-inputs.json').write_text(json.dumps(image.source_inputs(project, lock, '')))
            with (project / 'config/docker.yml').open('a') as output:
                output.write('\n# Changed while building\n')
            with self.assertRaisesRegex(ValueError, 'changed during image assembly'):
                image.record(project, directory, lock, 'release', 'user', ['base'], '')
            self.assertFalse((directory / 'manifest.json').exists())

    def test_damaged_image_and_test_release_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in image.OUTPUTS:
                (directory / name).write_bytes(b'image fixture')
            lock = directory / 'packages.json'
            lock.write_text('{}')
            manifest = {'schema': 1, 'kind': 'test', 'package_lock_sha256': image.checksum(lock),
                        'files': {name: {'sha256': image.checksum(directory / name),
                                         'size': (directory / name).stat().st_size}
                                  for name in (*image.OUTPUTS, 'packages.json')}}
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            image.verify(directory)
            with self.assertRaisesRegex(ValueError, 'Expected an image'):
                image.verify(directory, 'release')
            (directory / 'root.img').write_bytes(b'changed bytes')
            with self.assertRaisesRegex(ValueError, 'Missing or changed'):
                image.verify(directory)


if __name__ == '__main__':
    unittest.main()
