"""Release packaging safety checks; no real Stash or external services."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build
import installer


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='oneclick-package-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        for name in installer.PAYLOAD_FILES:
            (self.payload / name).write_text('fixture ' + name, encoding='utf-8')

    def test_zip_excludes_unlisted_files_and_subdirectories(self):
        (self.payload / 'config.yml').write_text('do not ship')
        (self.payload / 'data').mkdir()
        (self.payload / 'data' / 'private.json').write_text('do not ship')
        target = self.root / 'release.zip'
        build.package_zip(self.payload, target)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {'JAVFanzaOneClick/' + n for n in installer.PAYLOAD_FILES})
            self.assertIsNone(archive.testzip())

    def test_zip_does_not_overwrite_existing_output(self):
        target = self.root / 'release.zip'
        target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            build.package_zip(self.payload, target)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_missing_notice_prevents_archive_creation(self):
        (self.payload / 'THIRD_PARTY_NOTICES.txt').unlink()
        target = self.root / 'release.zip'
        with self.assertRaises(RuntimeError):
            build.package_zip(self.payload, target)
        self.assertFalse(target.exists())

    def test_installer_arguments_are_individual_allowlisted_files(self):
        (self.payload / 'private.log').write_text('do not ship')
        args = build.installer_data_args(self.payload)
        self.assertEqual(args[::2], ['--add-data'] * len(installer.PAYLOAD_FILES))
        self.assertEqual({Path(a.split(';')[0]).name for a in args[1::2]}, set(installer.PAYLOAD_FILES))
        self.assertNotIn(str(self.payload) + ';payload/JAVFanzaOneClick', args)

    def test_linked_file_is_rejected(self):
        with patch.object(build, 'linked', side_effect=lambda p: p.name == 'README.md'):
            with self.assertRaises(RuntimeError):
                build.manifest_paths(self.payload)

    def test_path_traversal_is_rejected(self):
        with patch.object(build, 'PAYLOAD_FILES', ('../private.json',)):
            with self.assertRaises(RuntimeError):
                build.manifest_paths(self.payload)

    def test_fresh_directory_never_reuses_existing_directory(self):
        with patch.object(build, 'ROOT', self.root):
            with self.assertRaises(FileExistsError):
                build.directory(self.payload, fresh=True)
            with self.assertRaises(RuntimeError):
                build.directory(self.root.parent / 'outside')

    def test_installer_preserves_data_and_installs_notices(self):
        target = self.root / 'installed'
        target.mkdir()
        (target / 'config.yml').write_text('keep me')
        with patch.object(installer, 'bundled_plugin_dir', return_value=self.payload), patch.object(installer, 'install_target', return_value=target):
            installer.install()
            installer.install()
        for name in installer.PAYLOAD_FILES:
            self.assertEqual((target / name).read_bytes(), (self.payload / name).read_bytes())
        self.assertEqual((target / 'config.yml').read_text(), 'keep me')


if __name__ == '__main__':
    unittest.main()
