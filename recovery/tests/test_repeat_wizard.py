import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('repeat_wizard', Path(__file__).parents[1] / 'wizard.py')
wizard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wizard)


class RepeatWizardTests(unittest.TestCase):
    def test_ready_mode_uses_r_and_rechecks_before_vendor_but_never_prepares_rootfs(self):
        self.assertTrue(hasattr(wizard, 'reusable'), 'Ready-image wizard is not integrated')
        for mode in ('emmc', 'nvme'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                events = []
                runner = Mock(log=Path(directory) / 'run.log')
                runner.run.side_effect = lambda argv, cwd: events.append(('command', argv))
                device = {'sysfs': '/sys/usb/3-1', 'product_id': '7223'}
                def validate(*_args):
                    events.append(('verify', None))
                    return {'created_at': 'test'}
                with contextlib.ExitStack() as stack:
                    for name, value in [('require_root', None), ('print_checks', True),
                                        ('show_recovery', device), ('usb_identity', ('3-1','3','4','')),
                                        ('ask_erase', True), ('confirm_nvme_requirements', True), ('pause', None)]:
                        stack.enter_context(patch.object(wizard, name, return_value=value))
                    stack.enter_context(patch.object(wizard.core, 'recovery_devices', return_value=[device]))
                    stack.enter_context(patch.object(wizard.core, 'check_runtime', return_value=[]))
                    stack.enter_context(patch.object(wizard.core, 'validate_prepared'))
                    stack.enter_context(patch.object(wizard.core, 'require_initrd_network'))
                    stack.enter_context(patch.object(wizard.core, 'prepare', side_effect=AssertionError('Rebuilt rootfs')))
                    stack.enter_context(patch.object(wizard.core, 'preserve_prepared_initrd', return_value=contextlib.nullcontext()))
                    stack.enter_context(patch.object(wizard, 'flash_host_state', return_value=contextlib.nullcontext()))
                    stack.enter_context(patch.object(wizard, 'usb_preflight', side_effect=lambda *_: events.append(('usb', None))))
                    stack.enter_context(patch.object(wizard.reusable, 'validate', side_effect=validate))
                    stack.enter_context(patch.object(wizard.reusable, 'required_free_bytes', return_value=8*1024**3))
                    stack.enter_context(patch.object(wizard.reusable, 'record_success', side_effect=AssertionError('Cannot bless repeat output')))
                    stack.enter_context(patch.object(wizard.reusable, 'refresh_after_repeat', return_value=None))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    self.assertTrue(wizard.flash_wizard(Path(directory), runner, fixed_mode=mode, reuse_images=True))
                command = [argv for name, argv in events if name == 'command'][0]
                self.assertIn('-r', command)
                self.assertNotIn('--flash-only', command)
                self.assertGreaterEqual(sum(name == 'verify' for name, _ in events), 2)
                self.assertEqual(events[-2][0], 'usb')
                self.assertFalse(runner.destructive)

    def test_corrupt_cache_stops_before_any_vendor_command(self):
        self.assertTrue(hasattr(wizard, 'reusable'), 'Ready-image wizard is not integrated')
        runner = Mock()
        with patch.object(wizard, 'require_root'), \
             patch.object(wizard.reusable, 'validate', side_effect=wizard.core.RecoveryError('corrupt image')):
            with self.assertRaises(wizard.core.RecoveryError):
                wizard.flash_wizard(Path('/prepared'), runner, fixed_mode='emmc', reuse_images=True)
        runner.run.assert_not_called()

    def test_cleanup_needs_confirmation_and_keeps_nvme_input(self):
        for mode, answer, removed in [('emmc', '', False), ('emmc', 'CLEAN GENERATED', True),
                                      ('nvme', 'CLEAN GENERATED', False)]:
            with self.subTest(mode=mode, answer=answer), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / 'bootloader').mkdir()
                (root / '.geacx1-prepared.json').write_text('{}')
                (root / '.geacx1-reuse.json').write_text('{"mode":"' + mode + '"}')
                raw = root / 'bootloader/system.img.raw'; raw.write_bytes(b'raw')
                image = root / 'bootloader/system.img'; image.write_bytes(b'keep')
                with patch.object(wizard, 'require_root'), \
                     patch.object(wizard, 'saved_work_path', return_value=root), \
                     patch.object(wizard, 'ask', return_value=answer), patch.object(wizard, 'pause'), \
                     contextlib.redirect_stdout(io.StringIO()):
                    wizard.cleanup_generated_images(Mock())
                self.assertEqual(raw.exists(), not removed)
                self.assertEqual(image.read_bytes(), b'keep')

    def test_repeat_space_check_does_not_require_fresh_build_reserve(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(wizard.core.shutil, 'disk_usage', return_value=Mock(free=9*1024**3)):
            fresh = next(x for x in wizard.core.host_checks(Path(directory)) if x['name'] == 'Свободное место')
            repeat = next(x for x in wizard.core.host_checks(Path(directory), 8*1024**3) if x['name'] == 'Свободное место')
            self.assertFalse(fresh['ok'])
            self.assertTrue(repeat['ok'])


if __name__ == '__main__':
    unittest.main()
