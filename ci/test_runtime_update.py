import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('UPDATE_RECOVERY'), 'Offline update is missing')
        import UPDATE_RECOVERY
        self.update = UPDATE_RECOVERY

    def fixture(self, root, original_pid=False):
        old = json.loads(gzip.decompress((ROOT / 'ci/fixtures/update-v1.4-originals.json.gz').read_bytes()))
        if original_pid:
            pid = json.loads(gzip.decompress((ROOT / 'ci/fixtures/pid-hotfix-originals.json.gz').read_bytes()))['v1.4']
            old.update(pid)
        (root / 'docs').mkdir()
        for name, data in old.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        (root / 'SHA256SUMS').write_text(''.join(hashlib.sha256(data.encode()).hexdigest() + '  ' + name + '\n' for name, data in old.items()))
        (root / 'last-work.json').write_text('{"l4t":"/prepared/keep-this"}\n')
        return old

    def test_updates_installed_v14_without_touching_saved_work_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = self.fixture(root)
            backup = self.update.apply_update(root)
            self.assertTrue(backup.is_dir())
            self.assertEqual((root / 'last-work.json').read_text(), '{"l4t":"/prepared/keep-this"}\n')
            for name in self.update.records():
                self.assertEqual((root / name).read_bytes(), (ROOT / 'recovery' / name).read_bytes())
                self.assertIn(hashlib.sha256((root / name).read_bytes()).hexdigest() + '  ' + name,
                              (root / 'SHA256SUMS').read_text())
            self.assertIsNone(self.update.apply_update(root))
            self.assertEqual((backup / 'wizard.py').read_text(), old['wizard.py'])

    def test_unknown_file_stops_before_any_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            (root / 'wizard.py').write_text('custom change')
            before = (root / 'lib/recovery_core.py').read_bytes()
            with self.assertRaises(ValueError):
                self.update.apply_update(root)
            self.assertEqual((root / 'lib/recovery_core.py').read_bytes(), before)
            self.assertFalse((root / 'lib/reusable_images.py').exists())

    def test_original_v14_archive_gets_pid_and_runtime_update_without_separate_hotfix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root, original_pid=True)
            self.update.apply_update(root, keep_backup=False)
            self.assertEqual((root / 'wizard.py').read_bytes(), (ROOT / 'recovery/wizard.py').read_bytes())
            self.assertEqual((root / 'lib/recovery_core.py').read_bytes(), (ROOT / 'recovery/lib/recovery_core.py').read_bytes())

    def test_small_original_archive_is_updated_by_real_assembler(self):
        import assemble
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original'; original.mkdir()
            self.fixture(original, original_pid=True)
            (original / 'docs/SYMLINKS.json').write_text('{}\n')
            with (original / 'SHA256SUMS').open('a') as manifest:
                manifest.write(hashlib.sha256(b'{}\n').hexdigest() + '  docs/SYMLINKS.json\n')
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                archive.add(original, arcname='geacx1-recovery')
            data = buffer.getvalue()
            (root / 'payload').mkdir()
            (root / 'payload/fixture.part').write_bytes(data)
            manifest = {'archive': assemble.ARCHIVE, 'sha256': hashlib.sha256(data).hexdigest(),
                        'size': len(data), 'directory': 'geacx1-recovery', 'parts': [
                            {'name': 'fixture.part', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]}
            kit = assemble.unpack(root, manifest, root / 'delivered')
            self.assertEqual((kit / 'wizard.py').read_bytes(), (ROOT / 'recovery/wizard.py').read_bytes())
            self.assertTrue((kit / 'lib/reusable_images.py').is_file())

    def test_write_failure_rolls_back_existing_and_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); old = self.fixture(root)
            original_manifest = (root / 'SHA256SUMS').read_bytes()
            replace = self.update.replace_file
            count = 0
            def failing(*args):
                nonlocal count
                count += 1
                if count == 4:
                    raise OSError('simulated storage failure')
                return replace(*args)
            with patch.object(self.update, 'replace_file', side_effect=failing):
                with self.assertRaises(OSError):
                    self.update.apply_update(root)
            for name, text in old.items():
                self.assertEqual((root / name).read_text(), text)
            self.assertEqual((root / 'SHA256SUMS').read_bytes(), original_manifest)
            self.assertFalse((root / 'lib/reusable_images.py').exists())

    def test_symlink_parent_refused_without_modifying_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            outside = root / 'outside'
            (root / 'lib').rename(outside)
            (root / 'lib').symlink_to(outside, target_is_directory=True)
            before = (outside / 'recovery_core.py').read_bytes()
            with self.assertRaises(ValueError):
                self.update.apply_update(root)
            self.assertEqual((outside / 'recovery_core.py').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
