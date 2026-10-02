"""Ready images must not be accepted after corruption, mode changes or failed builds."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import recovery_core as core


class ReusableImagesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('reusable_images'),
                             'Ready-image verification module is missing')
        import reusable_images
        self.reuse = reusable_images
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'bootloader').mkdir()
        (self.root / '.geacx1-prepared.json').write_text('{"verified":"fixture"}')
        (self.root / 'bootloader/system.img').write_bytes(b'known image contents')
        (self.root / 'bootloader/system.img.raw').write_bytes(b'raw filesystem image')
        (self.root / 'bootloader/l4t-rootfs-uuid.txt_ext').write_text('c9395d78-d4d3-44c9-bb0e-35d2e2b78c20\n')
        self.validate = patch.object(core, 'validate_prepared', return_value={})
        self.validate.start()
        self.addCleanup(self.validate.stop)

    def test_record_then_recheck_image_but_reject_another_mode(self):
        self.reuse.record_success(self.root, 'emmc')
        result = self.reuse.validate(self.root, 'emmc')
        self.assertEqual(result['mode'], 'emmc')
        self.assertEqual(set(result['files']), {'bootloader/system.img', 'bootloader/l4t-rootfs-uuid.txt_ext'})
        with self.assertRaises(core.RecoveryError):
            self.reuse.validate(self.root, 'nvme')

    def test_unregistered_legacy_image_is_not_adopted(self):
        with self.assertRaises(core.RecoveryError):
            self.reuse.validate(self.root, 'emmc')

    def test_modified_image_uuid_marker_and_missing_nvme_raw_block_reuse(self):
        for name in ('bootloader/system.img', 'bootloader/system.img.raw',
                     'bootloader/l4t-rootfs-uuid.txt_ext', '.geacx1-prepared.json'):
            with self.subTest(name=name):
                before = (self.root / name).read_bytes()
                self.reuse.record_success(self.root, 'nvme')
                (self.root / name).write_bytes(b'changed')
                with self.assertRaises(core.RecoveryError):
                    self.reuse.validate(self.root, 'nvme')
                (self.root / name).write_bytes(before)
        self.reuse.record_success(self.root, 'nvme')
        (self.root / 'bootloader/system.img.raw').unlink()
        with self.assertRaises(core.RecoveryError):
            self.reuse.validate(self.root, 'nvme')

    def test_manifest_cannot_omit_required_image_or_inject_paths(self):
        self.reuse.record_success(self.root, 'emmc')
        receipt = self.root / '.geacx1-reuse.json'
        good = json.loads(receipt.read_text())
        for files in ({}, {'../outside': {'sha256': '0'*64, 'size': 1}}):
            bad = dict(good, files=files)
            receipt.write_text(json.dumps(bad))
            with self.assertRaises(core.RecoveryError):
                self.reuse.validate(self.root, 'emmc')

    def test_symlink_image_or_parent_is_rejected_before_registration(self):
        image = self.root / 'bootloader/system.img'
        image.unlink()
        image.symlink_to(self.root / 'bootloader/system.img.raw')
        with self.assertRaises(core.RecoveryError):
            self.reuse.record_success(self.root, 'emmc')
        self.assertFalse((self.root / '.geacx1-reuse.json').exists())

    def test_begin_new_generation_invalidates_old_receipt(self):
        self.reuse.record_success(self.root, 'emmc')
        self.reuse.invalidate(self.root)
        with self.assertRaises(core.RecoveryError):
            self.reuse.validate(self.root, 'emmc')

    def test_successful_nvme_repeat_can_refresh_fsck_metadata_but_not_replace_image(self):
        self.reuse.record_success(self.root, 'nvme')
        (self.root / 'bootloader/system.img.raw').write_bytes(b'filesystem with updated fsck metadata')
        self.reuse.refresh_after_repeat(self.root, 'nvme')
        self.reuse.validate(self.root, 'nvme')
        (self.root / 'bootloader/system.img').write_bytes(b'wrong image')
        with self.assertRaises(core.RecoveryError):
            self.reuse.refresh_after_repeat(self.root, 'nvme')

    def test_repeat_argv_uses_normal_eeprom_path_and_reuses_system_image(self):
        self.assertEqual(self.reuse.build_command('emmc'),
                         ['./flash.sh', '-r', 'geacx1-32gb-jp72', 'mmcblk0p1'])
        command = self.reuse.build_command('nvme')
        self.assertEqual(command[:2], ['./tools/kernel_flash/l4t_initrd_flash.sh', '-r'])
        self.assertIn('--external-device', command)
        self.assertNotIn('--flash-only', command)
        self.assertNotIn('--reuse', command)

    def test_stale_board_spec_is_isolated_not_executed(self):
        spec = self.root / 'bootloader/board.spec'
        spec.write_text('BOARDID=9999\ntouch /never-execute-this\n')
        self.reuse.isolate_board_spec(self.root)
        self.assertFalse(spec.exists())
        self.assertEqual((self.root / 'bootloader/board.spec.previous').read_text(),
                         'BOARDID=9999\ntouch /never-execute-this\n')

    def test_space_estimate_uses_allocated_raw_bytes_not_sparse_apparent_size(self):
        raw = self.root / 'bootloader/system.img.raw'
        with raw.open('wb') as stream:
            stream.truncate(64 * 1024**3)
        self.assertEqual(self.reuse.required_free_bytes(self.root, 'emmc'), 8 * 1024**3)
        self.assertLess(self.reuse.required_free_bytes(self.root, 'nvme'), 20 * 1024**3)
        self.assertEqual(self.reuse.protected_cleanup_paths('nvme'), {'bootloader/system.img.raw'})


if __name__ == '__main__':
    unittest.main()
